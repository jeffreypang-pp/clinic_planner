"""Password protection: one shared password, a signed HttpOnly session cookie.

Set ``CLINIC_PASSWORD`` (environment variable) to turn it on.  Then every API call, and so all
clinic data, needs a valid session cookie; the static page itself contains no data and is served
as usual (it shows a login screen).

* ``POST /api/login {"password": ...}``  -> sets the session cookie
* ``POST /api/logout``                   -> clears it
* ``GET  /api/mode``                     -> reports whether login is needed / done (never data)

Environment:
  CLINIC_PASSWORD         the password (unset = no protection when running locally)
  CLINIC_REQUIRE_PASSWORD "1" to refuse to serve data until a password is configured.  This is
                          automatic on Vercel (``VERCEL`` is set there), so a deployment with no
                          password fails closed instead of exposing the data.
  CLINIC_SESSION_HOURS    how long a login lasts (default 12)
  CLINIC_SECRET           optional extra secret mixed into the cookie-signing key

The cookie is ``<expiry>.<HMAC-SHA256>``; the signing key is derived from the password (and
secret) with PBKDF2, so changing the password logs everyone out.  Wrong guesses are slowed down
and, after a few, refused for a while (per IP, per server process: best effort on serverless;
add a Vercel Firewall rate-limit rule for anything stronger).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from functools import lru_cache
from http.cookies import SimpleCookie

COOKIE = "clinic_session"
Response = tuple  # (http_status, json_payload, [(header, value), ...])


@lru_cache(maxsize=8)
def _signing_key(password: str, secret: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", (password + "\0" + secret).encode(),
                               b"clinic-planner-session-v1", 200_000)


class Auth:
    def __init__(self, password: str | None = None, require: bool = False, ttl_seconds: int = 12 * 3600,
                 secret: str = "", max_failures: int = 5, lockout_seconds: int = 600,
                 failure_delay: float = 0.4) -> None:
        self.password = password or None
        self.require = require
        self.ttl = ttl_seconds
        self.secret = secret
        self.max_failures, self.lockout, self.failure_delay = max_failures, lockout_seconds, failure_delay
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, env=None) -> "Auth":
        env = os.environ if env is None else env
        try:
            hours = float(env.get("CLINIC_SESSION_HOURS") or 12)
        except ValueError:
            hours = 12
        return cls(env.get("CLINIC_PASSWORD") or None,
                   require=bool(env.get("VERCEL")) or env.get("CLINIC_REQUIRE_PASSWORD") in ("1", "true", "yes"),
                   ttl_seconds=int(hours * 3600), secret=env.get("CLINIC_SECRET", ""))

    @property
    def enabled(self) -> bool:
        return self.password is not None

    @property
    def misconfigured(self) -> bool:
        """Protection is required but no password has been set: refuse to serve data."""
        return self.require and not self.enabled

    # ---- password / token ----------------------------------------------------
    def verify_password(self, candidate) -> bool:
        if not self.enabled or not isinstance(candidate, str):
            return False
        a = hashlib.sha256(candidate.encode()).digest()
        b = hashlib.sha256(self.password.encode()).digest()
        return hmac.compare_digest(a, b)

    def _sign(self, expiry: int) -> str:
        return hmac.new(_signing_key(self.password, self.secret), f"v1.{expiry}".encode(),
                        hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        expiry = int((time.time() if now is None else now) + self.ttl)
        return f"{expiry}.{self._sign(expiry)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not self.enabled or not token or "." not in token:
            return False
        exp, _, sig = token.partition(".")
        try:
            expiry = int(exp)
        except ValueError:
            return False
        if expiry <= (time.time() if now is None else now):
            return False
        return hmac.compare_digest(sig, self._sign(expiry))

    def authenticated(self, headers) -> bool:
        raw = headers.get("Cookie") if headers is not None else None
        if not raw:
            return False
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return False
        return COOKIE in jar and self.valid(jar[COOKIE].value)

    def set_cookie(self, token: str, secure: bool) -> str:
        return f"{COOKIE}={token}; Path=/; Max-Age={self.ttl}; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")

    @staticmethod
    def clear_cookie(secure: bool) -> str:
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")

    # ---- throttling wrong guesses (per client, per process) ---------------------
    def locked_for(self, ip: str, now: float | None = None) -> int:
        """Seconds until this client may try again (0 = may try now)."""
        now = time.time() if now is None else now
        recent = [t for t in self._failures.get(ip, []) if now - t < self.lockout]
        self._failures[ip] = recent
        return int(self.lockout - (now - recent[0])) + 1 if len(recent) >= self.max_failures else 0

    def record_failure(self, ip: str, now: float | None = None) -> None:
        self._failures.setdefault(ip, []).append(time.time() if now is None else now)
        if len(self._failures) > 5000:   # don't grow without bound
            self._failures.clear()

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)


# ---- request helpers shared by the local server and the Vercel functions ---------------------
def client_ip(headers, fallback: str = "?") -> str:
    fwd = (headers.get("X-Forwarded-For") if headers is not None else None) or ""
    return fwd.split(",")[0].strip() or fallback


def is_https(headers) -> bool:
    return (headers.get("X-Forwarded-Proto") if headers is not None else "") == "https"


def mode_info(auth: Auth, headers) -> dict:
    """Login state for the UI (``/api/mode``); contains no clinic data."""
    return {"auth": auth.enabled, "authenticated": auth.authenticated(headers) if auth.enabled else True,
            "misconfigured": auth.misconfigured}


def handle_auth_route(auth: Auth, method: str, route: str, raw_body: bytes, headers,
                      fallback_ip: str = "?") -> Response | None:
    """Answer /api/login and /api/logout; ``None`` for any other route."""
    secure = is_https(headers)
    if route == "/api/logout" and method == "POST":
        return 200, {"ok": True}, [("Set-Cookie", auth.clear_cookie(secure))]
    if route != "/api/login" or method != "POST":
        return None
    if auth.misconfigured:
        return 503, {"error": "No password is configured on this deployment (set CLINIC_PASSWORD)."}, []
    if not auth.enabled:
        return 200, {"ok": True, "auth": False}, []
    ip = client_ip(headers, fallback_ip)
    wait = auth.locked_for(ip)
    if wait:
        return 429, {"error": f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s)."}, \
            [("Retry-After", str(wait))]
    try:
        candidate = json.loads(raw_body or b"{}").get("password")
    except (json.JSONDecodeError, AttributeError):
        candidate = None
    if not auth.verify_password(candidate):
        auth.record_failure(ip)
        if auth.failure_delay:
            time.sleep(auth.failure_delay)
        return 401, {"error": "Wrong password"}, []
    auth.record_success(ip)
    return 200, {"ok": True}, [("Set-Cookie", auth.set_cookie(auth.issue(), secure))]


def denied(auth: Auth, headers) -> Response | None:
    """``None`` if the request may see data, otherwise the 401/503 to send instead."""
    if auth.misconfigured:
        return 503, {"error": "This deployment is not configured yet: set CLINIC_PASSWORD "
                              "(Vercel: Project Settings > Environment Variables) and redeploy."}, []
    if auth.enabled and not auth.authenticated(headers):
        return 401, {"error": "Login required", "login": True}, []
    return None
