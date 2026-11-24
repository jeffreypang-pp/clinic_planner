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
        day = next(iter(self.app.clinic.bookings.values())).date.isoformat()   # a Monday; Dr Tan is the botox doctor at Central
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

    def test_staff_training_can_be_edited_through_the_api(self):
        before = self.disk()
        sid = next(iter(self.app.clinic.staff))
        status, r = self.call("PUT", "/api/training", {"equipment_needing_training": ["laser"], "skills": {sid: ["laser"]}})
        self.assertEqual(status, 200)
        c = self.app.clinic
        self.assertEqual((c.training_equipment, c.staff[sid].skills), ({"laser"}, {"laser"}))
        self.assertEqual(self.call("GET", "/api/clinic")[1]["clinic"]["equipment_needing_training"], ["laser"])
        self.assertEqual(self.disk(), before)                                          # in memory until saved
        self.assertEqual(self.call("PUT", "/api/training", {})[0], 400)
        self.assertEqual(self.call("PUT", "/api/training", {"skills": {"nobody": []}})[0], 404)
        self.call("POST", "/api/save", {})
        self.assertEqual(Clinic.load_dir(self.dir).staff[sid].skills, {"laser"})

    def test_assigning_staff_later_via_the_api(self):
        before = self.disk()
        c = self.app.clinic
        day = next(iter(c.bookings.values())).date.isoformat()
        base = dict(numbing_minutes=0, doctor_minutes=0, staff_minutes=30, room_id="central_laser_b", start="12:00")
        status, r = self.call("POST", "/api/book-list", {"location_id": "central", "date": day, "items": [base]})
        booking = r["results"][0]["booking"]
        self.assertEqual((booking["unassigned_steps"], booking["steps"][0]["staff"]), (1, None))
        options = [o["id"] for o in booking["steps"][0]["staff_options"]]
        self.assertTrue(options)
        status, r = self.call("PUT", f"/api/bookings/{booking['id']}/staff", {"staff": {"staff": options[0]}})
        self.assertEqual((status, r["booking"]["unassigned_steps"], r["booking"]["steps"][0]["staff_id"]), (200, 0, options[0]))
        self.assertEqual(self.call("PUT", f"/api/bookings/{booking['id']}/staff", {"staff": {"staff": "ghost"}})[0], 404)
        self.assertEqual(self.call("PUT", f"/api/bookings/{booking['id']}/staff", {"staff": {"numbing": options[0]}})[0], 400)
        self.assertEqual(self.call("PUT", "/api/bookings/nope/staff", {"staff": {}})[0], 404)
        status, r = self.call("PUT", f"/api/bookings/{booking['id']}/staff", {"staff": {"staff": None}})
        self.assertEqual(r["booking"]["unassigned_steps"], 1)
        status, r = self.call("GET", f"/api/timetable?location=central&date={day}")
        self.assertIn("staff_total", r)
        self.assertEqual(max(r["staff_total"]["needed"]), 1)
        self.assertEqual(self.disk(), before)                                     # all in memory until saved

    def test_list_files_via_the_api(self):
        item = dict(name="Mine", numbing_minutes=15, doctor_minutes=30, staff_minutes=15, room_id="central_laser_b",
                    start="12:00", client="C")
        before = self.disk()
        status, r = self.call("POST", "/api/list-file", {"name": "Plan / A", "location_id": "central", "items": [item]})
        self.assertEqual((status, r["filename"]), (200, "Plan _ A.json"))
        self.assertEqual(json.loads(r["text"])["items"][0]["name"], "Mine")
        self.assertEqual(self.disk(), before)
        stale = dict(item, room_id="gone")
        status, r = self.call("POST", "/api/list-file", {"name": "x", "location_id": "central", "items": [stale]})
        self.assertEqual(status, 404)                                                          # strict by default
        status, r = self.call("POST", "/api/list-file", {"name": "x", "location_id": "central", "items": [stale], "lenient": True})
        self.assertEqual((status, r["items"][0]["room_id"], len(r["warnings"])), (200, None, 1))
        self.assertEqual(self.call("POST", "/api/list-file", {"name": "x", "location_id": "central", "items": []})[0], 400)

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
