"""Password protection: nothing is served without a login; tokens can't be forged; fails closed."""
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from clinic.auth import COOKIE, Auth, handle_auth_route, denied, mode_info
from clinic.sample import build_sample
from clinic.server import App, make_handler
from clinic.stateless import get_auth, handle_call, initial_payload, route

PW = "correct horse battery staple"


def auth(**kw):
    kw.setdefault("failure_delay", 0)
    return Auth(PW, **kw)


class FakeHeaders(dict):
    """case-insensitive-ish header bag good enough for .get()"""
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class AuthUnitTests(unittest.TestCase):
    def test_password_check(self):
        a = auth()
        self.assertTrue(a.verify_password(PW))
        for bad in ("", "wrong", PW + " ", PW.upper(), None, 5, b"x"):
            self.assertFalse(a.verify_password(bad), bad)
        self.assertFalse(Auth(None).verify_password(""))                  # no password set: nothing verifies

    def test_tokens_expire_and_cannot_be_forged_or_reused_after_a_password_change(self):
        a = auth(ttl_seconds=100)
        tok = a.issue(now=1000)
        self.assertTrue(a.valid(tok, now=1050))
        self.assertFalse(a.valid(tok, now=1100))                          # expired exactly at ttl
        exp, sig = tok.split(".")
        for forged in (f"{int(exp) + 9999}.{sig}",                       # extended expiry
                       f"{exp}.{'0' * len(sig)}", f"{exp}.", ".", "", "garbage", f"x.{sig}", None):
            self.assertFalse(a.valid(forged, now=1050), forged)
        other = Auth("different password", ttl_seconds=100, failure_delay=0)
        self.assertFalse(other.valid(tok, now=1050))                      # rotating the password logs everyone out
        self.assertFalse(Auth(PW, secret="s", ttl_seconds=100).valid(tok, now=1050))
        self.assertFalse(Auth(None).valid(tok, now=1050))

    def test_cookie_flags(self):
        a = auth()
        c = a.set_cookie("tok", secure=True)
        for part in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/", f"Max-Age={a.ttl}", f"{COOKIE}=tok"):
            self.assertIn(part, c)
        self.assertNotIn("Secure", a.set_cookie("tok", secure=False))
        self.assertIn("Max-Age=0", Auth.clear_cookie(True))

    def test_from_env(self):
        self.assertFalse(Auth.from_env({}).enabled)
        self.assertFalse(Auth.from_env({}).misconfigured)
        self.assertTrue(Auth.from_env({"VERCEL": "1"}).misconfigured)                 # Vercel without a password
        self.assertTrue(Auth.from_env({"CLINIC_REQUIRE_PASSWORD": "1"}).misconfigured)
        a = Auth.from_env({"VERCEL": "1", "CLINIC_PASSWORD": PW, "CLINIC_SESSION_HOURS": "2"})
        self.assertEqual((a.enabled, a.misconfigured, a.ttl), (True, False, 7200))
        self.assertEqual(Auth.from_env({"CLINIC_SESSION_HOURS": "nonsense"}).ttl, 12 * 3600)

    def test_throttle(self):
        a = auth(max_failures=3, lockout_seconds=60)
        for _ in range(3):
            self.assertEqual(handle_auth_route(a, "POST", "/api/login", b'{"password":"no"}', FakeHeaders(), "1.1.1.1")[0], 401)
        status, payload, hdrs = handle_auth_route(a, "POST", "/api/login", json.dumps({"password": PW}).encode(),
                                                  FakeHeaders(), "1.1.1.1")
        self.assertEqual(status, 429)                                     # locked, even with the right password
        self.assertIn("Retry-After", dict(hdrs))
        self.assertEqual(handle_auth_route(a, "POST", "/api/login", json.dumps({"password": PW}).encode(),
                                           FakeHeaders(), "2.2.2.2")[0], 200)      # other clients unaffected
        self.assertEqual(a.locked_for("1.1.1.1", now=10 ** 12), 0)       # lock expires

    def test_login_route_edge_cases(self):
        a = auth(max_failures=100)          # many bad attempts from one client here; the lockout has its own test
        for raw in (b"", b"{nope", b"[]", b'{"password": 5}', b'{"password": null}', b'{}'):
            self.assertEqual(handle_auth_route(a, "POST", "/api/login", raw, FakeHeaders(), "x")[0], 401, raw)
        self.assertIsNone(handle_auth_route(a, "GET", "/api/login", b"", FakeHeaders(), "x"))   # not a login
        self.assertIsNone(handle_auth_route(a, "POST", "/api/other", b"", FakeHeaders(), "x"))
        self.assertEqual(handle_auth_route(Auth(None, require=True), "POST", "/api/login", b"{}", FakeHeaders(), "x")[0], 503)
        # no password and not required: nothing to log in to, nothing blocked
        self.assertEqual(handle_auth_route(Auth(None), "POST", "/api/login", b"{}", FakeHeaders(), "x")[:2], (200, {"ok": True, "auth": False}))
        self.assertIsNone(denied(Auth(None), FakeHeaders()))
        self.assertEqual(mode_info(Auth(None), None), {"auth": False, "authenticated": True, "misconfigured": False})

    def test_secure_flag_follows_the_forwarded_protocol(self):
        a = auth()
        body = json.dumps({"password": PW}).encode()
        https = handle_auth_route(a, "POST", "/api/login", body, FakeHeaders({"X-Forwarded-Proto": "https"}), "x")
        http_ = handle_auth_route(a, "POST", "/api/login", body, FakeHeaders(), "x")
        self.assertIn("Secure", dict(https[2])["Set-Cookie"])
        self.assertNotIn("Secure", dict(http_[2])["Set-Cookie"])

    def test_forwarded_ip_is_what_gets_throttled(self):
        a = auth(max_failures=1)
        h = FakeHeaders({"X-Forwarded-For": "9.9.9.9, 10.0.0.1"})
        handle_auth_route(a, "POST", "/api/login", b"{}", h, "10.0.0.1")
        self.assertGreater(a.locked_for("9.9.9.9"), 0)
        self.assertEqual(a.locked_for("10.0.0.1"), 0)


class StatelessRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=7).save_dir(self.dir)
        init = initial_payload(self.dir)
        self.state = {"version": 1, "groups": init["groups"], "base": init["groups"], "lists": {}, "rawHash": init["hashes"]}
        self.a = auth()

    def tearDown(self):
        self.tmp.cleanup()

    def login_headers(self):
        status, _, hdrs = route("POST", "/api/login", json.dumps({"password": PW}).encode(), self.dir, FakeHeaders(), self.a)
        self.assertEqual(status, 200)
        cookie = dict(hdrs)["Set-Cookie"].split(";")[0]
        return FakeHeaders({"Cookie": cookie})

    def call(self, headers):
        raw = json.dumps({"method": "GET", "path": "/api/status", "state": self.state}).encode()
        return route("POST", "/api/call", raw, self.dir, headers, self.a)

    def test_no_data_without_a_login(self):
        for method, path in (("GET", "/api/initial"), ("POST", "/api/call"), ("GET", "/api/anything")):
            status, payload, _ = route(method, path, b"{}", self.dir, FakeHeaders(), self.a)
            self.assertEqual(status, 401, path)
            self.assertNotIn("groups", payload)
        self.assertEqual(self.call(FakeHeaders())[0], 401)
        self.assertEqual(self.call(FakeHeaders({"Cookie": f"{COOKIE}=1.abc"}))[0], 401)
        self.assertEqual(self.call(FakeHeaders({"Cookie": "other=1; ;; broken"}))[0], 401)

    def test_mode_is_public_and_reveals_no_data(self):
        status, payload, _ = route("GET", "/api/mode", b"", self.dir, FakeHeaders(), self.a)
        self.assertEqual((status, payload), (200, {"mode": "stateless", "auth": True, "authenticated": False,
                                                   "misconfigured": False}))
        self.assertEqual(route("GET", "/api/mode", b"", self.dir, self.login_headers(), self.a)[1]["authenticated"], True)

    def test_login_unlocks_everything_and_logout_clears(self):
        h = self.login_headers()
        self.assertEqual(route("GET", "/api/initial", b"", self.dir, h, self.a)[0], 200)
        status, payload, _ = self.call(h)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(json.dumps(payload))["status"], 200)
        status, _, hdrs = route("POST", "/api/logout", b"", self.dir, h, self.a)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", dict(hdrs)["Set-Cookie"])

    def test_wrong_password_gets_nothing(self):
        status, payload, hdrs = route("POST", "/api/login", b'{"password": "nope"}', self.dir, FakeHeaders(), self.a)
        self.assertEqual((status, hdrs), (401, []))
        self.assertNotIn("Set-Cookie", dict(hdrs))

    def test_fails_closed_when_a_deployment_has_no_password(self):
        closed = Auth(None, require=True, failure_delay=0)
        for method, path in (("GET", "/api/initial"), ("POST", "/api/call"), ("POST", "/api/login")):
            self.assertEqual(route(method, path, b"{}", self.dir, FakeHeaders(), closed)[0], 503, path)
        status, payload, _ = route("GET", "/api/mode", b"", self.dir, FakeHeaders(), closed)
        self.assertEqual((status, payload["misconfigured"]), (200, True))     # the page can explain what to do

    def test_open_when_no_password_and_not_required(self):
        self.assertEqual(route("GET", "/api/initial", b"", self.dir, FakeHeaders(), Auth())[0], 200)
        self.assertEqual(route("GET", "/api/initial", b"", self.dir, FakeHeaders(), None)[0], 200)

    def test_get_auth_follows_the_environment_and_keeps_its_throttle(self):
        saved = {k: os.environ.get(k) for k in ("CLINIC_PASSWORD", "VERCEL")}
        try:
            os.environ.pop("VERCEL", None)
            os.environ["CLINIC_PASSWORD"] = "one"
            first = get_auth()
            self.assertIs(get_auth(), first)                                  # same object while settings are the same
            self.assertTrue(first.verify_password("one"))
            os.environ["CLINIC_PASSWORD"] = "two"
            self.assertTrue(get_auth().verify_password("two"))
            os.environ.pop("CLINIC_PASSWORD")
            os.environ["VERCEL"] = "1"
            self.assertTrue(get_auth().misconfigured)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class HttpTests(unittest.TestCase):
    """Over real HTTP: the local server and the Vercel entry points, with cookies like a browser."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=7).save_dir(self.dir)
        self.servers = []
        self._env = {k: os.environ.get(k) for k in ("CLINIC_PASSWORD", "CLINIC_DATA_DIR", "VERCEL")}

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def serve(self, handler):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    @staticmethod
    def browser():
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar

    @staticmethod
    def req(opener, method, url, body=None):
        r = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
        try:
            with opener.open(r) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_local_server_requires_login_for_the_api_but_serves_the_page(self):
        base = self.serve(make_handler(App(self.dir), auth=auth()))
        anon, _ = self.browser()
        with anon.open(base + "/") as r:
            self.assertIn(b"Clinic Planner", r.read())                         # the page holds no data
        self.assertEqual(self.req(anon, "GET", base + "/api/mode")[1]["authenticated"], False)
        for path in ("/api/clinic", "/api/timetable?location=central&date=2026-11-23", "/api/lists", "/api/status"):
            self.assertEqual(self.req(anon, "GET", base + path)[0], 401, path)
        for method, path in (("PUT", "/api/clinic"), ("POST", "/api/save"), ("POST", "/api/book-list"), ("DELETE", "/api/lists?name=x")):
            self.assertEqual(self.req(anon, method, base + path, {})[0], 401, path)
        self.assertEqual(self.req(anon, "POST", base + "/api/login", {"password": "wrong"})[0], 401)
        self.assertEqual(self.req(anon, "GET", base + "/api/clinic")[0], 401)   # a failed login grants nothing
        status, _ = self.req(anon, "POST", base + "/api/login", {"password": PW})
        self.assertEqual(status, 200)
        self.assertEqual(self.req(anon, "GET", base + "/api/clinic")[0], 200)
        self.assertEqual(self.req(anon, "GET", base + "/api/mode")[1], {"mode": "server", "auth": True, "authenticated": True,
                                                                          "misconfigured": False})
        self.req(anon, "POST", base + "/api/logout", {})
        self.assertEqual(self.req(anon, "GET", base + "/api/clinic")[0], 401)

    def test_the_session_cookie_is_httponly_and_samesite_strict(self):
        base = self.serve(make_handler(App(self.dir), auth=auth()))
        r = urllib.request.Request(base + "/api/login", method="POST", data=json.dumps({"password": PW}).encode())
        with urllib.request.urlopen(r) as resp:
            cookie = resp.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(COOKIE + "="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_one_browsers_login_does_not_help_another(self):
        base = self.serve(make_handler(App(self.dir), auth=auth()))
        a, _ = self.browser()
        b, _ = self.browser()
        self.req(a, "POST", base + "/api/login", {"password": PW})
        self.assertEqual(self.req(a, "GET", base + "/api/clinic")[0], 200)
        self.assertEqual(self.req(b, "GET", base + "/api/clinic")[0], 401)

    def test_no_password_configured_locally_means_open_as_before(self):
        base = self.serve(make_handler(App(self.dir)))
        anon, _ = self.browser()
        self.assertEqual(self.req(anon, "GET", base + "/api/clinic")[0], 200)
        self.assertEqual(self.req(anon, "GET", base + "/api/mode")[1]["auth"], False)

    def test_vercel_functions_enforce_the_password_and_fail_closed(self):
        os.environ["CLINIC_DATA_DIR"] = self.dir
        os.environ.pop("CLINIC_PASSWORD", None)
        os.environ["VERCEL"] = "1"                                            # as on Vercel, but no password set
        import api.call, api.initial, api.login, api.logout, api.mode
        urls = {n: self.serve(m.handler) + f"/api/{n}" for n, m in
                (("mode", api.mode), ("initial", api.initial), ("call", api.call), ("login", api.login), ("logout", api.logout))}
        anon, _ = self.browser()
        self.assertEqual(self.req(anon, "GET", urls["initial"])[0], 503)      # fails closed
        self.assertEqual(self.req(anon, "GET", urls["mode"])[1]["misconfigured"], True)
        os.environ["CLINIC_PASSWORD"] = PW                                    # now the password is configured
        self.assertEqual(self.req(anon, "GET", urls["initial"])[0], 401)
        self.assertEqual(self.req(anon, "POST", urls["call"], {"method": "GET", "path": "/api/status"})[0], 401)
        self.assertEqual(self.req(anon, "POST", urls["login"], {"password": "nope"})[0], 401)
        self.assertEqual(self.req(anon, "POST", urls["login"], {"password": PW})[0], 200)
        status, init = self.req(anon, "GET", urls["initial"])
        self.assertEqual(status, 200)
        self.assertIn("groups", init)
        self.assertEqual(self.req(anon, "GET", urls["mode"])[1]["authenticated"], True)
        self.req(anon, "POST", urls["logout"], {})
        self.assertEqual(self.req(anon, "GET", urls["initial"])[0], 401)


if __name__ == "__main__":
    unittest.main()
