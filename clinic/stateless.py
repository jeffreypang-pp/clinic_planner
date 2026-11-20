"""Stateless mode, for hosts such as Vercel where the server keeps no memory between requests
and can't write files.

How it works: the *browser* holds the working data and sends it with every request.  The
server rebuilds a ``Clinic`` from that state, runs exactly the same request handlers as the
local server (``server.dispatch``), and for requests that may change something sends the new
state back.  Nothing is stored on the server; the files in ``clinic_data/`` are only the
read-only starting point.  "Save" therefore hands back the changed files to download (put them
in ``clinic_data/`` and commit), and saved treatment lists live in the browser.

State sent by the browser::

    {"version": 1,
     "groups":  {resources, planning, schedule},   # the working data
     "base":    {resources, planning, schedule},   # as of the last save/download (for "unsaved")
     "lists":   {name: {...}},                     # saved treatment lists
     "rawHash": {file name: sha256 or null}}       # the deployed files it started from

Endpoints (see ``serve``): GET /api/mode, GET /api/initial, POST /api/call.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .auth import Auth, denied, handle_auth_route, mode_info
from .lists import MemoryLists, TreatmentLists
from .model import Clinic, ClinicError, Conflict, GROUPS
from .sample import build_sample
from .server import App, dispatch

STATE_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent


def deployed_dir() -> str:
    """The read-only data shipped with the deployment."""
    return os.environ.get("CLINIC_DATA_DIR") or str(ROOT / "clinic_data")


def file_hashes(texts: dict[str, str | None]) -> dict[str, str | None]:
    return {n: hashlib.sha256(t.encode()).hexdigest() if t is not None else None for n, t in texts.items()}


def _deployed_clinic(directory: str) -> Clinic:
    return Clinic.load_dir(directory) if Clinic.dir_has_data(directory) else build_sample()


def initial_payload(directory: str) -> dict:
    """What a browser starts from: the deployed data, saved lists and file fingerprints."""
    clinic = _deployed_clinic(directory)
    path = os.path.join(directory, "treatment_lists.json")
    lists = TreatmentLists(directory)._read() if os.path.exists(path) else {}
    return {"groups": clinic.to_groups(), "lists": lists,
            "hashes": file_hashes(Clinic.read_files(directory))}


class StatelessApp(App):
    """The same handlers as ``App``, but backed by request-supplied state, not memory/disk."""

    def __init__(self, state: dict, directory: str) -> None:
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ClinicError("Missing or outdated working data; reload the page")
        for key in ("groups", "base"):
            if not isinstance(state.get(key), dict):
                raise ClinicError(f"Working data is missing {key!r}")
        self.directory = directory
        self.path = "clinic_data"                      # only used in messages
        self.lock = threading.RLock()
        self.deployed = Clinic.read_files(directory)
        self.raw_hash = state.get("rawHash") or {}
        self.clinic = Clinic.from_groups(state["groups"])
        self.base_groups = state["base"]
        self.canon = Clinic.from_groups(state["base"]).render_files()
        self.lists = MemoryLists(state.get("lists") or {})

    def state(self) -> dict:
        return {"version": STATE_VERSION, "groups": self.clinic.to_groups(), "base": self.base_groups,
                "lists": self.lists.data, "rawHash": self.raw_hash}

    def external(self) -> list[str]:
        """Deployed files that differ from the ones this working copy started from (a newer deploy)."""
        now = file_hashes(self.deployed)
        return [n for n in now if now[n] != self.raw_hash.get(n)]

    def post_save(self, q, body):
        """There is no disk to write to: return the changed files for the browser to download,
        and treat the current data as the new "saved" baseline."""
        names = {u["file"] for u in self.unsaved()}
        conflicts = sorted(n for n in names if n in self.external())
        if conflicts and not (body or {}).get("force"):
            raise Conflict(f"{', '.join(conflicts)} changed in the deployed data since you loaded it")
        files = {n: t for n, t in self.clinic.render_files().items() if n in names}
        self.base_groups = self.clinic.to_groups()
        self.canon = self.clinic.render_files()
        return {"saved": sorted(files), "files": files, **self.get_clinic(q, None)}

    def post_discard(self, q, body):
        """Drop unsaved changes: back to the last saved/downloaded state, or, with
        ``{"to": "deployed"}``, to the data currently deployed (picking up a newer deploy)."""
        if (body or {}).get("to") == "deployed":
            self.clinic = _deployed_clinic(self.directory)
            self.base_groups = self.clinic.to_groups()
            self.raw_hash = file_hashes(self.deployed)
        else:
            self.clinic = Clinic.from_groups(self.base_groups)
        self.canon = Clinic.from_groups(self.base_groups).render_files()
        return self.get_clinic(q, None)


def handle_call(payload: dict, directory: str) -> dict:
    """Run one API request carried by ``POST /api/call``.

    ``payload``: ``{method, path, query, body, state}``.  Returns ``{status, result}`` plus the
    new ``state`` for anything that isn't a plain GET."""
    if not isinstance(payload, dict):
        raise ClinicError("Expected a JSON object")
    method, path = str(payload.get("method", "GET")).upper(), payload.get("path")
    query = payload.get("query") or {}
    if not isinstance(path, str) or not path.startswith("/api/") or not isinstance(query, dict):
        raise ClinicError("Bad request: need 'path' (/api/...) and 'query' (object)")
    app = StatelessApp(payload.get("state"), directory)
    status, result = dispatch(app, method, path, query, payload.get("body"))
    out = {"status": status, "result": result}
    if method != "GET" and status < 400:
        out["state"] = app.state()
    return out


def serve(method: str, path: str, raw_body: bytes = b"", directory: str | None = None) -> tuple[int, dict]:
    """Route one stateless-API request; returns ``(http_status, json_payload)``."""
    directory = directory or deployed_dir()
    route = path.split("?")[0].rstrip("/")
    try:
        if method == "GET" and route == "/api/mode":
            return 200, {"mode": "stateless"}
        if method == "GET" and route == "/api/initial":
            return 200, initial_payload(directory)
        if method == "POST" and route == "/api/call":
            try:
                payload = json.loads(raw_body or b"{}")
            except json.JSONDecodeError as e:
                raise ClinicError(f"Invalid JSON: {e}") from None
            return 200, handle_call(payload, directory)
        return 404, {"error": f"No route {method} {route}"}
    except ClinicError as e:
        return 400, {"error": str(e)}
    except Exception as e:  # pragma: no cover
        return 500, {"error": f"{type(e).__name__}: {e}"}


def route(method: str, path: str, raw_body: bytes = b"", directory: str | None = None, headers=None,
          auth: Auth | None = None, fallback_ip: str = "?") -> tuple[int, dict, list]:
    """``serve`` behind the password (see ``clinic.auth``): handles /api/login and /api/logout,
    reports login state on /api/mode, and answers 401/503 to everything else until the request
    carries a valid session.  Returns ``(http_status, json_payload, extra_headers)``."""
    auth = auth or Auth()                       # no password configured: open
    route_ = path.split("?")[0].rstrip("/")
    response = handle_auth_route(auth, method, route_, raw_body, headers, fallback_ip)
    if response:
        return response
    if method == "GET" and route_ == "/api/mode":
        return 200, {"mode": "stateless", **mode_info(auth, headers)}, []
    blocked = denied(auth, headers)
    if blocked:
        return blocked
    status, payload = serve(method, path, raw_body, directory)
    return status, payload, []


_AUTH_CACHE: dict = {}


def get_auth() -> Auth:
    """The ``Auth`` for this process, built from the environment (kept across warm invocations so
    the wrong-password throttle persists; rebuilt if the settings change)."""
    env = os.environ
    key = tuple(env.get(k) for k in ("CLINIC_PASSWORD", "VERCEL", "CLINIC_REQUIRE_PASSWORD",
                                     "CLINIC_SESSION_HOURS", "CLINIC_SECRET"))
    if key not in _AUTH_CACHE:
        _AUTH_CACHE.clear()
        _AUTH_CACHE[key] = Auth.from_env()
    return _AUTH_CACHE[key]


def respond(handler, method: str, endpoint: str) -> None:
    """Answer one request on a ``BaseHTTPRequestHandler`` (what the files in ``api/`` call)."""
    raw = b""
    if method == "POST":
        raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
    status, payload, extra = route(method, f"/api/{endpoint}", raw, None, handler.headers, get_auth(),
                                   handler.client_address[0])
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in extra:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(data)
