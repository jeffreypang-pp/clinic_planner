"""End-to-end: nothing is written to disk until /api/save; external changes are protected."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from clinic.model import Clinic
from clinic.sample import build_sample
from clinic.server import App, make_handler


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "data")
        build_sample().save_dir(self.dir)  # a clinic "already on disk"
        self.app = App(self.dir)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def disk(self):
        return Clinic.read_files(self.dir)

    def a_day_with_a_doctor(self):
        c = self.app.clinic
        (role, pid, day), loc = next((k, v) for k, v in c.presence.items() if k[0] == "doctor")
        return pid, day.isoformat(), loc

    def test_changes_are_not_written_until_saved(self):
        before = self.disk()
        pid, day, loc = self.a_day_with_a_doctor()
        status, r = self.call("PUT", "/api/presence",
                              {"role": "doctor", "person_id": pid, "date": day, "location_id": None})
        self.assertEqual(status, 200)
        self.call("PUT", "/api/availability", {"kind": "room", "id": "central_laser_a", "date": day,
                                               "start": "10:00", "end": "11:00", "available": False})
        self.assertEqual(self.disk(), before)                       # disk untouched
        status, r = self.call("GET", "/api/clinic")
        self.assertEqual([u["file"] for u in r["unsaved"]], ["schedule.json"])
        self.assertGreater(r["unsaved"][0]["added"] + r["unsaved"][0]["removed"], 0)
        self.assertEqual(r["external"], [])

    def test_save_writes_only_the_changed_files(self):
        before = self.disk()
        pid, day, loc = self.a_day_with_a_doctor()
        self.call("PUT", "/api/presence", {"role": "doctor", "person_id": pid, "date": day, "location_id": None})
        status, r = self.call("POST", "/api/save", {})
        self.assertEqual((status, r["saved"], r["unsaved"]), (200, ["schedule.json"], []))
        after = self.disk()
        self.assertNotEqual(after["schedule.json"], before["schedule.json"])
        self.assertEqual(after["resources.json"], before["resources.json"])
        self.assertEqual(after["planning.json"], before["planning.json"])
        self.assertEqual(Clinic.load_dir(self.dir).presence_at("doctor", pid, day), None)

    def test_discard_reverts_to_disk(self):
        before = self.disk()
        pid, day, loc = self.a_day_with_a_doctor()
        self.call("PUT", "/api/presence", {"role": "doctor", "person_id": pid, "date": day, "location_id": None})
        status, r = self.call("POST", "/api/discard", {})
        self.assertEqual((status, r["unsaved"]), (200, []))
        self.assertEqual(self.app.clinic.presence_at("doctor", pid, day), loc)
        self.assertEqual(self.disk(), before)

    def test_clear_day_is_in_memory_until_saved(self):
        before = self.disk()
        day = next(iter(self.app.clinic.bookings.values())).date.isoformat()
        status, r = self.call("POST", "/api/clear-day", {"location_id": "central", "date": day, "dry_run": True})
        self.assertEqual((status, r["bookings"]), (200, 1))
        self.assertEqual(self.call("GET", "/api/status")[1]["unsaved"], [])          # dry run = no change
        status, r = self.call("POST", "/api/clear-day", {"location_id": "central", "date": day})
        self.assertEqual(r["bookings"], 1)
        self.assertEqual(self.disk(), before)                                         # still not on disk
        self.assertEqual([u["file"] for u in self.call("GET", "/api/status")[1]["unsaved"]], ["schedule.json"])
        self.call("POST", "/api/save", {})
        self.assertEqual(Clinic.load_dir(self.dir).bookings, {})

    def test_old_treatment_format_is_rejected_with_a_helpful_message(self):
        planning = self.call("GET", "/api/clinic")[1]["groups"]["planning"]
        planning["treatments"].append({"id": "peel", "name": "Peel", "duration_minutes": 30})
        status, r = self.call("PUT", "/api/clinic", {"planning": planning})
        self.assertEqual(status, 400)
        self.assertIn("doctor_minutes", r["error"])
        self.assertEqual(self.call("GET", "/api/status")[1]["unsaved"], [])    # nothing changed

    def test_book_list_stops_at_first_failure_and_is_unsaved_until_saved(self):
        before = self.disk()
        day = next(iter(self.app.clinic.bookings.values())).date.isoformat()   # a Monday; Dr Lim is the botox doctor at Central
        items = [{"treatment_id": "botox", "start": "14:00", "client": "One"},
                 {"treatment_id": "botox", "start": "14:00", "client": "Two"},   # same doctor, same time
                 {"treatment_id": "consult", "start": "16:00"}]
        body = {"location_id": "central", "date": day, "items": items}
        status, r = self.call("POST", "/api/book-list", dict(body, dry_run=True))
        self.assertEqual((status, [x["status"] for x in r["results"]]), (200, ["booked", "failed", "not_attempted"]))
        self.assertEqual(self.call("GET", "/api/status")[1]["unsaved"], [])    # a dry run changes nothing
        status, r = self.call("POST", "/api/book-list", body)
        self.assertEqual((r["booked"], r["stopped_at"]), (1, 1))
        self.assertEqual(self.disk(), before)                                   # booked in memory only
        self.assertEqual([u["file"] for u in self.call("GET", "/api/status")[1]["unsaved"]], ["schedule.json"])
        status, r = self.call("POST", "/api/book-list", {"location_id": "central", "date": day, "items": "nope"})
        self.assertEqual(status, 400)

    def test_custom_treatments_via_book_list(self):
        before = self.disk()
        day = next(iter(self.app.clinic.bookings.values())).date.isoformat()
        item = dict(numbing_minutes=15, doctor_minutes=30, staff_minutes=15, room_id="central_laser_b",
                    start="12:00", name="Mine", client="C")
        body = {"location_id": "central", "date": day, "items": [item, dict(item, doctor_minutes=0)]}   # 2nd breaks the rule
        status, r = self.call("POST", "/api/book-list", body)
        self.assertEqual((status, [x["status"] for x in r["results"]]), (200, ["booked", "failed"]))
        self.assertIn("numbing", " ".join(r["results"][1]["reasons"]))
        self.assertEqual(r["results"][0]["booking"]["treatment"], "Mine")
        self.assertEqual(self.disk(), before)                                        # in memory until saved
        self.call("POST", "/api/save", {})
        saved = Clinic.load_dir(self.dir)
        mine = next(b for b in saved.bookings.values() if b.name == "Mine")
        self.assertEqual((mine.room_id, mine.numbing_slots, mine.doctor_slots, mine.staff_slots), ("central_laser_b", 1, 2, 1))

    def test_saved_lists_api_is_explicit_and_separate_from_clinic_data(self):
        before = self.disk()
        lists_file = os.path.join(self.dir, "treatment_lists.json")
        self.assertEqual(self.call("GET", "/api/lists")[1]["lists"], [])
        self.assertFalse(os.path.exists(lists_file))                              # reading never creates it
        item = dict(name="Mine", numbing_minutes=15, doctor_minutes=30, staff_minutes=15,
                    room_id="central_laser_b", start="12:00", client="C")
        body = {"name": "Plan A", "location_id": "central", "items": [item, dict(item, start="14:00")]}
        status, r = self.call("PUT", "/api/lists", body)
        self.assertEqual((status, r["saved"], [x["count"] for x in r["lists"]]), (200, "Plan A", [2]))
        self.assertTrue(os.path.exists(lists_file))
        self.assertEqual(self.disk(), before)                                     # the three clinic files untouched
        status, r = self.call("GET", "/api/status")
        self.assertEqual((r["unsaved"], r["external"]), ([], []))                 # and it doesn't show as unsaved
        self.assertEqual(self.call("PUT", "/api/lists", body)[0], 409)            # same name needs overwrite
        self.assertEqual(self.call("PUT", "/api/lists", dict(body, overwrite=True))[0], 200)
        bad = dict(body, name="Bad", items=[dict(item, doctor_minutes=0)])
        status, r = self.call("PUT", "/api/lists", bad)
        self.assertEqual(status, 400)
        self.assertIn("Treatment 1", r["error"])
        # a saved list can be run as-is
        day = next(iter(self.app.clinic.bookings.values())).date.isoformat()
        saved = self.call("GET", "/api/lists")[1]["lists"][0]
        status, r = self.call("POST", "/api/book-list", {"location_id": saved["location_id"], "date": day,
                                                         "items": saved["items"], "dry_run": True})
        self.assertEqual([x["status"] for x in r["results"]], ["booked", "booked"])
        status, r = self.call("DELETE", "/api/lists?name=Plan%20A")
        self.assertEqual((status, r["lists"]), (200, []))
        self.assertEqual(self.call("DELETE", "/api/lists?name=Plan%20A")[0], 404)
        self.assertEqual(self.disk(), before)

    def test_json_editor_apply_is_not_a_save_either(self):
        before = self.disk()
        status, r = self.call("GET", "/api/clinic")
        planning = r["groups"]["planning"]
        planning["treatments"].append({"id": "peel", "name": "Peel", "doctor_minutes": 30})
        status, r = self.call("PUT", "/api/clinic", {"planning": planning})
        self.assertEqual(status, 200)
        self.assertEqual([u["file"] for u in r["unsaved"]], ["planning.json"])
        self.assertEqual(self.disk(), before)

    def test_file_changed_behind_our_back_blocks_save(self):
        """e.g. you ran git pull while the app was open."""
        pid, day, loc = self.a_day_with_a_doctor()
        self.call("PUT", "/api/presence", {"role": "doctor", "person_id": pid, "date": day, "location_id": None})
        sched = os.path.join(self.dir, "schedule.json")
        with open(sched) as f:
            pulled = f.read().replace('"version": 1', '"version": 1, "pulled": true', 1)
        with open(sched, "w") as f:
            f.write(pulled)
        status, r = self.call("GET", "/api/status")        # lightweight poll used by the UI
        self.assertEqual(r["external"], ["schedule.json"])
        self.assertEqual([u["file"] for u in r["unsaved"]], ["schedule.json"])
        status, r = self.call("POST", "/api/save", {})
        self.assertEqual(status, 409)
        self.assertIn("schedule.json", r["error"])
        with open(sched) as f:
            self.assertEqual(f.read(), pulled)                      # their pulled file survived
        status, r = self.call("POST", "/api/save", {"force": True})  # explicit overwrite is possible
        self.assertEqual((status, r["saved"]), (200, ["schedule.json"]))

    def test_saving_never_touches_files_we_did_not_edit(self):
        pid, day, loc = self.a_day_with_a_doctor()
        self.call("PUT", "/api/presence", {"role": "doctor", "person_id": pid, "date": day, "location_id": None})
        res = os.path.join(self.dir, "resources.json")
        with open(res) as f:
            pulled = f.read().replace('"version": 1', '"version": 1, "pulled": true', 1)
        with open(res, "w") as f:
            f.write(pulled)
        status, r = self.call("POST", "/api/save", {})              # we only edited schedule.json
        self.assertEqual((status, r["saved"]), (200, ["schedule.json"]))
        with open(res) as f:
            self.assertEqual(f.read(), pulled)

    def test_discard_picks_up_pulled_changes(self):
        res = os.path.join(self.dir, "resources.json")
        with open(res) as f:
            data = json.load(f)
        data["staff"].append({"id": "new", "name": "New Person"})
        with open(res, "w") as f:
            json.dump(data, f)
        status, r = self.call("POST", "/api/discard", {})
        self.assertIn("new", [s["id"] for s in r["clinic"]["staff"]])
        self.assertEqual(r["external"], [])


class HandEditedFileTests(unittest.TestCase):
    def test_reformatted_file_is_not_reported_as_unsaved(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "data")
            build_sample().save_dir(d)
            path = os.path.join(d, "resources.json")
            with open(path) as f:
                data = json.load(f)
            with open(path, "w") as f:
                json.dump(data, f)                       # same content, different formatting
            app = App(d)
            self.assertEqual((app.unsaved(), app.external()), ([], []))
            app.clinic.staff["s1"].name = "Renamed"
            self.assertEqual([u["file"] for u in app.unsaved()], ["resources.json"])


class FreshInstallTests(unittest.TestCase):
    def test_sample_is_not_written_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "fresh")
            app = App(d)
            self.assertFalse(os.path.exists(d))
            self.assertEqual({u["file"] for u in app.unsaved()},
                             {"resources.json", "planning.json", "schedule.json"})


if __name__ == "__main__":
    unittest.main()
