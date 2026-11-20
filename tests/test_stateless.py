"""Stateless mode (Vercel): the browser carries the data; the server stores and writes nothing."""
import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from clinic.model import Clinic
from clinic.sample import build_sample
from clinic.server import App, dispatch, make_handler
from clinic.stateless import StatelessApp, handle_call, initial_payload, serve


class StatelessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=14).save_dir(self.dir)
        init = initial_payload(self.dir)
        self.state = {"version": 1, "groups": init["groups"], "base": init["groups"],
                      "lists": init["lists"], "rawHash": init["hashes"]}
        self.snapshot = self.files()
        self.day = "2026-11-23"

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return Clinic.read_files(self.dir)

    def call(self, method, path, body=None, query=None, state=None, force_ok=True):
        out = handle_call({"method": method, "path": path, "query": query or {}, "body": body,
                           "state": self.state if state is None else state}, self.dir)
        if "state" in out:
            self.state = out["state"]
        return out

    # ---- the starting point -------------------------------------------------
    def test_initial_payload_is_the_deployed_data(self):
        init = initial_payload(self.dir)
        self.assertEqual(init["groups"], Clinic.load_dir(self.dir).to_groups())
        self.assertEqual(set(init["hashes"]), {"resources.json", "planning.json", "schedule.json"})
        self.assertEqual(init["hashes"]["planning.json"],
                         hashlib.sha256(self.snapshot["planning.json"].encode()).hexdigest())
        self.assertEqual(init["lists"], {})

    def test_falls_back_to_the_sample_when_nothing_is_deployed(self):
        with tempfile.TemporaryDirectory() as empty:
            init = initial_payload(empty)
            self.assertTrue(Clinic.from_groups(init["groups"]).doctors)
            self.assertEqual(set(init["hashes"].values()), {None})

    # ---- same answers as the stateful server --------------------------------
    def test_read_requests_match_the_stateful_app(self):
        app = App(self.dir)
        for path, query in (("/api/timetable", {"location": ["central"], "date": [self.day]}),
                            ("/api/roster", {"start": [self.day], "days": ["7"]}),
                            ("/api/availability", {"treatment": ["botox"], "date": [self.day], "start": ["14:00"]}),
                            ("/api/slots", {"treatment": ["botox"], "date": [self.day]}),
                            ("/api/bookings", {"date": [self.day]})):
            out = self.call("GET", path, query=query)
            self.assertEqual(out["status"], 200, path)
            self.assertEqual(out["result"], dispatch(app, "GET", path, query, None)[1], path)
            self.assertNotIn("state", out)                      # reads send nothing back

    def test_changes_come_back_as_new_state_and_nothing_touches_disk(self):
        before = copy.deepcopy(self.state)
        out = self.call("PUT", "/api/presence", {"role": "doctor", "person_id": "okafor", "date": self.day,
                                                 "location_id": None})
        self.assertEqual(out["status"], 200)
        self.assertNotEqual(out["state"]["groups"], before["groups"])
        self.assertEqual(out["state"]["base"], before["base"])                # baseline unchanged
        self.assertEqual(self.files(), self.snapshot)                          # the server wrote nothing
        st = self.call("GET", "/api/status")["result"]
        self.assertEqual([u["file"] for u in st["unsaved"]], ["schedule.json"])
        self.assertEqual(st["external"], [])
        # a booking list, as the UI sends it
        items = [dict(name="Mine", numbing_minutes=15, doctor_minutes=30, staff_minutes=15,
                      room_id="central_laser_b", start="12:00", client="C")]
        out = self.call("POST", "/api/book-list", {"location_id": "central", "date": self.day, "items": items})
        self.assertEqual([x["status"] for x in out["result"]["results"]], ["booked"])
        self.assertEqual(sum(1 for b in out["state"]["groups"]["schedule"]["bookings"] if b["name"] == "Mine"), 1)
        # the request's own state object is not modified in place
        self.assertEqual(before["groups"]["schedule"], self.call("GET", "/api/clinic", state=before)["result"]["groups"]["schedule"])

    def test_errors_are_reported_with_status_and_leave_state_alone(self):
        before = copy.deepcopy(self.state)
        out = self.call("POST", "/api/book-list", {"location_id": "central", "date": self.day, "items": "nope"})
        self.assertEqual(out["status"], 400)
        self.assertNotIn("state", out)
        self.assertEqual(self.call("DELETE", "/api/bookings/ghost")["status"], 404)
        self.assertEqual(self.call("GET", "/api/nope")["status"], 404)
        self.assertEqual(self.state, before)

    # ---- save = download, discard = revert ----------------------------------
    def test_save_returns_only_changed_files_and_resets_the_baseline(self):
        self.call("PUT", "/api/presence", {"role": "staff", "person_id": "s1", "date": self.day, "location_id": None})
        out = self.call("POST", "/api/save", {})
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["result"]["saved"], ["schedule.json"])
        self.assertEqual(list(out["result"]["files"]), ["schedule.json"])
        self.assertEqual(out["result"]["unsaved"], [])
        self.assertEqual(self.files(), self.snapshot)                          # still nothing written server-side
        # the returned file is exactly what a normal save would have written
        saved = json.loads(out["result"]["files"]["schedule.json"])
        self.assertEqual(saved, self.state["groups"]["schedule"])
        self.assertEqual(self.state["base"]["schedule"], self.state["groups"]["schedule"])
        self.assertEqual(self.call("GET", "/api/status")["result"]["unsaved"], [])

    def test_discard_goes_back_to_the_last_saved_state(self):
        self.call("PUT", "/api/presence", {"role": "staff", "person_id": "s1", "date": self.day, "location_id": None})
        self.call("POST", "/api/save", {})
        self.call("PUT", "/api/presence", {"role": "staff", "person_id": "s2", "date": self.day, "location_id": None})
        self.assertEqual(len(self.call("GET", "/api/status")["result"]["unsaved"]), 1)
        out = self.call("POST", "/api/discard", {})
        self.assertEqual(out["result"]["unsaved"], [])
        self.assertEqual(self.state["groups"], self.state["base"])
        # s1's earlier (saved) change survived, s2's discarded change did not
        sched = self.state["groups"]["schedule"]["presence"][self.day]["staff"]
        self.assertNotIn("s1", sched)
        self.assertIn("s2", sched)

    def test_newer_deployed_data_is_detected_and_protected(self):
        self.call("PUT", "/api/presence", {"role": "staff", "person_id": "s1", "date": self.day, "location_id": None})
        path = os.path.join(self.dir, "schedule.json")
        with open(path) as f:
            newer = f.read().replace('"version": 1', '"version": 1, "redeployed": true', 1)
        with open(path, "w") as f:
            f.write(newer)                                                      # a new deploy changed the file
        st = self.call("GET", "/api/status")["result"]
        self.assertEqual(st["external"], ["schedule.json"])
        out = self.call("POST", "/api/save", {})
        self.assertEqual(out["status"], 409)
        self.assertIn("schedule.json", out["result"]["error"])
        self.assertEqual(self.call("POST", "/api/save", {"force": True})["status"], 200)
        # reloading the deployed data picks the new deploy up and clears the warning
        out = self.call("POST", "/api/discard", {"to": "deployed"})
        self.assertEqual((out["result"]["external"], out["result"]["unsaved"]), ([], []))
        self.assertEqual(self.state["rawHash"]["schedule.json"],
                         hashlib.sha256(newer.encode()).hexdigest())

    # ---- saved lists live in the state, not on disk -----------------------------
    def test_saved_lists_are_kept_in_the_browser_state(self):
        item = dict(name="Mine", numbing_minutes=15, doctor_minutes=30, staff_minutes=15,
                    room_id="central_laser_b", start="12:00", client="C")
        out = self.call("PUT", "/api/lists", {"name": "Plan A", "location_id": "central", "items": [item]})
        self.assertEqual(out["status"], 200)
        self.assertEqual(list(out["state"]["lists"]), ["Plan A"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "treatment_lists.json")))
        self.assertEqual(self.call("PUT", "/api/lists", {"name": "Plan A", "location_id": "central", "items": [item]})["status"], 409)
        got = self.call("GET", "/api/lists")["result"]["lists"]
        self.assertEqual([(x["name"], x["count"]) for x in got], [("Plan A", 1)])
        self.call("DELETE", "/api/lists", query={"name": ["Plan A"]})
        self.assertEqual(self.state["lists"], {})

    # ---- bad input ----------------------------------------------------------------
    def test_bad_state_and_payloads_are_rejected(self):
        for state in (None, {}, {"version": 2}, {"version": 1}, {"version": 1, "groups": {}, "base": 5}):
            with self.assertRaises(Exception):
                StatelessApp(state, self.dir)
        broken = copy.deepcopy(self.state)
        broken["groups"]["planning"]["doctor_skills"]["ghost"] = []
        status, payload = serve("POST", "/api/call", json.dumps({"method": "GET", "path": "/api/status", "state": broken}).encode(), self.dir)
        self.assertEqual(status, 400)
        self.assertIn("ghost", payload["error"])
        for raw in (b"{nope", b"[]", json.dumps({"path": "/etc/passwd", "state": self.state}).encode(),
                    json.dumps({"path": "/api/status", "query": 5, "state": self.state}).encode()):
            self.assertEqual(serve("POST", "/api/call", raw, self.dir)[0], 400, raw)
        self.assertEqual(serve("GET", "/api/call", b"", self.dir)[0], 404)
        self.assertEqual(serve("GET", "/api/mode", b"", self.dir), (200, {"mode": "stateless"}))


class VercelEntryPointTests(unittest.TestCase):
    """The files in api/ are what Vercel runs; exercise them over real HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=14).save_dir(self.dir)
        os.environ["CLINIC_DATA_DIR"] = self.dir
        import api.call, api.initial, api.mode
        self.servers = []
        self.urls = {}
        for name, mod in (("mode", api.mode), ("initial", api.initial), ("call", api.call)):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.servers.append(httpd)
            self.urls[name] = f"http://127.0.0.1:{httpd.server_address[1]}/api/{name}"

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        os.environ.pop("CLINIC_DATA_DIR", None)
        self.tmp.cleanup()

    def test_the_three_functions_work_end_to_end(self):
        with urllib.request.urlopen(self.urls["mode"]) as r:
            mode = json.load(r)
            self.assertEqual((mode["mode"], mode["auth"], mode["authenticated"]), ("stateless", False, True))
        with urllib.request.urlopen(self.urls["initial"]) as r:
            init = json.load(r)
            self.assertEqual(r.headers["Cache-Control"], "no-store")
        state = {"version": 1, "groups": init["groups"], "base": init["groups"], "lists": init["lists"],
                 "rawHash": init["hashes"]}
        req = urllib.request.Request(self.urls["call"], method="POST", data=json.dumps(
            {"method": "GET", "path": "/api/timetable", "query": {"location": ["central"], "date": ["2026-11-23"]},
             "state": state}).encode())
        with urllib.request.urlopen(req) as r:
            out = json.load(r)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["result"]["location"], "Central")
        req = urllib.request.Request(self.urls["call"], method="POST", data=b"not json")
        try:
            urllib.request.urlopen(req)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


class LocalStatelessServerTests(unittest.TestCase):
    def test_stateless_flag_serves_the_same_api_and_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "data")
            build_sample(date(2026, 11, 20), days=7).save_dir(d)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, stateless_dir=d))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/mode") as r:
                    self.assertEqual(json.load(r)["mode"], "stateless")
                with urllib.request.urlopen(base + "/") as r:
                    self.assertIn(b"Clinic Planner", r.read())
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
