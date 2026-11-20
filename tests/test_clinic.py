import json
import os
import tempfile
import time
import unittest
from datetime import date

from clinic.model import (Clinic, ClinicError, NotFound, GROUP_FILES, format_slot, parse_time)
from clinic.sample import build_sample

MON = date(2026, 11, 23)  # a Monday
SUN = date(2026, 11, 22)


def small_clinic() -> Clinic:
    """2 locations; d1 does laser+botox, d2 botox only; 2 staff; nobody present yet."""
    return Clinic.from_dict({
        "locations": [
            {"id": "a", "name": "A", "opening_hours": {"mon": ["09:00", "17:00"]},
             "rooms": [{"id": "a_laser", "name": "Laser", "equipment": ["laser"]},
                       {"id": "a_plain", "name": "Plain", "equipment": []}]},
            {"id": "b", "name": "B", "opening_hours": {"mon": ["10:00", "18:00"]},
             "rooms": [{"id": "b_plain", "name": "Plain", "equipment": []}]},
        ],
        "treatments": [
            {"id": "laser", "name": "Laser", "duration_minutes": 60,
             "required_equipment": ["laser"], "staff_required": 1},
            {"id": "botox", "name": "Botox", "duration_minutes": 30, "staff_required": 2},
        ],
        "doctors": [{"id": "d1", "name": "D1", "treatments": ["laser", "botox"]},
                    {"id": "d2", "name": "D2", "treatments": ["botox"]}],
        "staff": [{"id": "s1", "name": "S1"}, {"id": "s2", "name": "S2"}],
    })


def tick(c: Clinic, role, pid, loc, day=MON):
    c.set_presence(role, pid, day, loc)


def t(text):
    return parse_time(text)


class TimeTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(parse_time("09:15"), 37)
        self.assertEqual(format_slot(37), "09:15")
        self.assertEqual(parse_time("24:00"), 96)

    def test_rejects_off_grid(self):
        for bad in ("09:10", "25:00", "9", "ab:cd", "24:15"):
            with self.assertRaises(ClinicError):
                parse_time(bad)


class PresenceTests(unittest.TestCase):
    def test_one_location_per_day(self):
        c = small_clinic()
        tick(c, "doctor", "d1", "a")
        self.assertEqual(c.presence_at("doctor", "d1", MON), "a")
        tick(c, "doctor", "d1", "b")          # ticking at B moves them from A
        self.assertEqual(c.presence_at("doctor", "d1", MON), "b")
        self.assertEqual([k for k in c.presence if k[1] == "d1"], [("doctor", "d1", MON)])
        tick(c, "doctor", "d1", None)         # untick
        self.assertIsNone(c.presence_at("doctor", "d1", MON))

    def test_presence_is_per_day_and_validated(self):
        c = small_clinic()
        tick(c, "staff", "s1", "a")
        self.assertIsNone(c.presence_at("staff", "s1", date(2026, 11, 24)))
        with self.assertRaises(NotFound):
            tick(c, "staff", "s1", "nowhere")
        with self.assertRaises(NotFound):
            tick(c, "staff", "ghost", "a")
        with self.assertRaises(ClinicError):
            tick(c, "nurse", "s1", "a")

    def test_doctor_and_staff_namespaces_are_separate(self):
        c = Clinic.from_dict({"locations": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
                              "doctors": [{"id": "x", "name": "Dr X"}], "staff": [{"id": "x", "name": "Sx"}]})
        tick(c, "doctor", "x", "a")
        tick(c, "staff", "x", "b")
        self.assertEqual((c.presence_at("doctor", "x", MON), c.presence_at("staff", "x", MON)), ("a", "b"))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "staff", "s1", "a")

    def avail(self, treatment="laser", loc="a", start="10:00", day=MON):
        return self.c.check_availability(treatment, loc, day, t(start))

    def test_available_when_all_resources_present(self):
        r = self.avail()
        self.assertTrue(r["available"], r["reasons"])
        self.assertEqual([d["id"] for d in r["doctors"]], ["d1"])
        self.assertEqual([x["id"] for x in r["rooms"]], ["a_laser"])  # equipment-limited

    def test_present_people_are_free_all_day_unless_booked(self):
        for start in ("09:00", "12:00", "16:00"):
            self.assertTrue(self.avail(start=start)["available"], start)

    def test_outside_opening_hours_and_closed_day(self):
        self.assertFalse(self.avail(start="08:45")["available"])
        self.assertFalse(self.avail(start="16:15")["available"])  # 60 min would end 17:15
        self.assertTrue(self.avail(start="16:00")["available"])   # ends exactly at close
        tick(self.c, "doctor", "d1", "a", SUN)
        tick(self.c, "staff", "s1", "a", SUN)
        r = self.avail(day=SUN)
        self.assertFalse(r["available"])
        self.assertIn("closed", r["reasons"][0])

    def test_doctor_skill_limits_treatment(self):
        tick(self.c, "doctor", "d2", "a")
        tick(self.c, "staff", "s2", "a")
        self.assertEqual([d["id"] for d in self.avail("laser")["doctors"]], ["d1"])
        self.assertEqual({d["id"] for d in self.avail("botox")["doctors"]}, {"d1", "d2"})

    def test_must_be_ticked_in_at_that_location(self):
        r = self.avail(loc="b", start="11:00")  # d1 and s1 are at A
        self.assertFalse(r["available"])
        self.assertTrue(any("No doctor qualified" in x for x in r["reasons"]))
        tick(self.c, "doctor", "d1", "b")
        tick(self.c, "staff", "s1", "b")
        self.assertTrue(self.avail("botox", "b", "11:00")["available"] is False)  # botox needs 2 staff
        self.assertFalse(self.avail("laser", "b", "11:00")["available"])           # B has no laser room
        self.assertFalse(self.avail("laser", "a", "10:00")["available"])           # and they left A

    def test_untick_removes_availability(self):
        tick(self.c, "doctor", "d1", None)
        self.assertFalse(self.avail()["available"])

    def test_staff_count_required(self):
        tick(self.c, "doctor", "d2", "a")
        r = self.avail("botox")  # needs 2 staff, only s1 present
        self.assertFalse(r["available"])
        self.assertTrue(any("2 staff" in x for x in r["reasons"]))
        tick(self.c, "staff", "s2", "a")
        self.assertTrue(self.avail("botox")["available"])

    def test_room_blocking_by_booking(self):
        self.c.book("laser", "a", MON, t("10:00"))
        tick(self.c, "staff", "s2", "a")
        tick(self.c, "doctor", "d2", "a")
        r = self.avail("laser", start="10:30")  # only laser room is taken
        self.assertFalse(r["available"])
        self.assertTrue(any("room" in x for x in r["reasons"]))
        self.assertTrue(self.avail("laser", start="11:00")["available"])  # back-to-back ok

    def test_double_booking_person_blocked(self):
        tick(self.c, "staff", "s2", "a")
        b = self.c.book("botox", "a", MON, t("10:00"))
        self.assertEqual(b.doctor_id, "d1")
        # d1 and both staff are now used; d2 isn't present -> no second botox at 10:15
        self.assertFalse(self.avail("botox", start="10:15")["available"])

    def test_find_slots(self):
        slots = self.c.find_slots("laser", MON)
        self.assertEqual(slots["a"][0], "09:00")
        self.assertEqual(slots["a"][-1], "16:00")
        self.assertEqual(slots["b"], [])  # nobody ticked in at B

    def test_unknown_ids(self):
        with self.assertRaises(NotFound):
            self.c.check_availability("nope", "a", MON, 40)


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "staff", "s1", "a")

    def ok(self, start):
        return self.c.check_availability("laser", "a", MON, t(start))

    def test_room_block(self):
        self.assertTrue(self.ok("10:00")["available"])
        self.c.set_blocked("room", "a_laser", MON, t("10:30"), t("12:00"), True)
        self.assertFalse(self.ok("10:00")["available"])   # 10:00-11:00 overlaps
        self.assertTrue(self.ok("09:00")["available"])    # ends exactly when block starts
        self.assertFalse(self.ok("11:30")["available"])
        self.assertTrue(self.ok("12:00")["available"])
        self.c.set_blocked("room", "a_laser", MON, t("10:00"), t("13:00"), False)
        self.assertTrue(self.ok("10:00")["available"])
        self.assertEqual(self.c.blocks, {})

    def test_doctor_and_staff_block(self):
        self.c.set_blocked("doctor", "d1", MON, t("12:00"), t("13:00"), True)  # lunch
        self.assertFalse(self.ok("11:30")["available"])
        self.assertTrue(self.ok("13:00")["available"])
        self.c.set_blocked("doctor", "d1", MON, t("12:00"), t("13:00"), False)
        self.c.set_blocked("staff", "s1", MON, t("14:00"), t("15:00"), True)
        r = self.ok("14:00")
        self.assertFalse(r["available"])
        self.assertTrue(any("staff" in x for x in r["reasons"]))

    def test_block_validation(self):
        with self.assertRaises(ClinicError):
            self.c.set_blocked("desk", "x", MON, 40, 44, True)
        with self.assertRaises(NotFound):
            self.c.set_blocked("room", "nope", MON, 40, 44, True)
        with self.assertRaises(ClinicError):
            self.c.set_blocked("room", "a_laser", MON, 44, 40, True)

    def test_timetable_cells_and_roundtrip(self):
        self.c.set_blocked("room", "a_laser", MON, t("09:00"), t("10:00"), True)
        self.c.set_blocked("room", "a_laser", MON, t("10:30"), t("11:00"), True)
        self.c.set_blocked("doctor", "d1", MON, t("12:00"), t("12:30"), True)
        tt = self.c.timetable("a", MON)
        room = next(r for r in tt["rooms"] if r["id"] == "a_laser")["cells"]
        self.assertEqual((room[t("08:45")], room[t("09:00")], room[t("10:00")], room[t("10:30")]),
                         ("closed", "blocked", "free", "blocked"))
        doc = next(d for d in tt["doctors"] if d["id"] == "d1")["cells"]
        self.assertEqual((doc[t("11:45")], doc[t("12:00")], doc[t("12:30")]), ("free", "blocked", "free"))
        data = self.c.to_dict()
        self.assertEqual([(b["kind"], b["start"], b["end"]) for b in data["blocks"]],
                         [("doctor", "12:00", "12:30"), ("room", "09:00", "10:00"), ("room", "10:30", "11:00")])
        self.assertEqual(Clinic.from_dict(data).to_dict(), data)


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "staff", "s1", "a")

    def test_book_auto_assigns_and_cancel_frees(self):
        c = self.c
        b = c.book("laser", "a", MON, t("09:00"), client="X")
        self.assertEqual((b.doctor_id, b.room_id, b.staff_ids), ("d1", "a_laser", ["s1"]))
        with self.assertRaises(ClinicError):
            c.book("laser", "a", MON, t("09:30"))
        c.cancel_booking(b.id)
        c.book("laser", "a", MON, t("09:30"))

    def test_explicit_unqualified_doctor_rejected(self):
        tick(self.c, "doctor", "d2", "a")
        with self.assertRaises(ClinicError):
            self.c.book("laser", "a", MON, t("09:00"), doctor_id="d2")

    def test_unticking_is_reported_not_destructive(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.assertEqual(self.c.issues(), [])
        tick(self.c, "doctor", "d1", None)
        self.assertTrue(self.c.bookings)
        issues = self.c.issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("D1", issues[0])

    def test_moving_person_to_other_location_is_reported(self):
        self.c.book("laser", "a", MON, t("09:00"))
        tick(self.c, "staff", "s1", "b")
        self.assertTrue(any("S1" in i for i in self.c.issues()))

    def test_blocking_after_booking_is_reported(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.c.set_blocked("room", "a_laser", MON, t("09:30"), t("10:00"), True)
        self.assertTrue(any("Laser" in i for i in self.c.issues()))

    def test_changing_opening_hours_is_reported(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.c.set_opening_hours("a", {0: (t("10:00"), t("17:00"))})
        self.assertTrue(any("opening hours" in i for i in self.c.issues()))


class OpeningHoursTests(unittest.TestCase):
    def test_adjustable_hours_and_overrides(self):
        c = small_clinic()
        loc = c.locations["a"]
        self.assertEqual(loc.hours_on(MON), (36, 68))
        c.set_opening_hours("a", {0: (t("08:00"), t("20:15")), 1: (40, 60)})
        self.assertEqual(loc.hours_on(MON), (32, 81))
        self.assertIsNotNone(loc.hours_on(date(2026, 11, 24)))
        loc.date_overrides[MON] = None
        self.assertIsNone(loc.hours_on(MON))
        with self.assertRaises(ClinicError):
            c.set_opening_hours("a", {0: (50, 40)})


class TimetableTests(unittest.TestCase):
    def test_cells_and_checkmarks(self):
        c = small_clinic()
        tick(c, "doctor", "d1", "a")
        tick(c, "doctor", "d2", "b")
        tick(c, "staff", "s1", "a")
        b = c.book("laser", "a", MON, t("09:00"))
        ta, tb = c.timetable("a", MON), c.timetable("b", MON)
        row = lambda tt, kind, pid: next(r for r in tt[kind] if r["id"] == pid)
        d1 = row(ta, "doctors", "d1")
        self.assertTrue(d1["here"])
        self.assertEqual(d1["at"], "a")
        self.assertEqual(d1["cells"][t("09:00")], f"booked:{b.id}")
        self.assertEqual(d1["cells"][t("10:00")], "free")
        self.assertEqual(d1["cells"][t("17:00")], "closed")  # present but location closed
        d2 = row(ta, "doctors", "d2")
        self.assertFalse(d2["here"])
        self.assertEqual((d2["at"], d2["cells"][t("12:00")]), ("b", "away:b"))
        self.assertEqual(row(tb, "doctors", "d1")["cells"][t("12:00")], "away:a")
        self.assertTrue(row(tb, "doctors", "d2")["here"])
        s2 = row(ta, "staff", "s2")
        self.assertEqual((s2["here"], s2["at"], s2["cells"][t("12:00")]), (False, None, "off"))
        room = next(r for r in ta["rooms"] if r["id"] == "a_laser")["cells"]
        self.assertEqual((room[t("08:45")], room[t("09:45")]), ("closed", f"booked:{b.id}"))
        self.assertEqual(len(ta["bookings"]), 1)
        self.assertEqual(len(tb["rooms"]), 1)

    def test_weekly_roster(self):
        c = small_clinic()
        tick(c, "doctor", "d1", "a", MON)
        tick(c, "doctor", "d1", "b", date(2026, 11, 25))
        r = c.roster(MON, 7)
        self.assertEqual(r["dates"][0], "2026-11-23")
        d1 = next(p for p in r["people"] if p["role"] == "doctor" and p["id"] == "d1")
        self.assertEqual(d1["at"], ["a", None, "b", None, None, None, None])


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.c = build_sample(date(2026, 11, 20), days=14)

    def test_split_puts_each_kind_of_data_in_its_group(self):
        g = self.c.to_groups()
        self.assertEqual(set(g), {"resources", "planning", "schedule"})
        res, plan, sched = g["resources"], g["planning"], g["schedule"]
        # group 1: who and where - nothing that changes often
        self.assertEqual(set(res), {"version", "locations", "doctors", "staff"})
        self.assertEqual(set(res["locations"][0]), {"id", "name", "rooms"})
        self.assertEqual(set(res["doctors"][0]), {"id", "name"})
        # group 2: hours, treatments, skills, who is where each day
        self.assertEqual(set(plan), {"version", "opening_hours", "date_overrides", "treatments",
                                     "doctor_skills", "presence"})
        # group 3: bookings and blocked times
        self.assertEqual(set(sched), {"version", "blocks", "bookings"})

    def test_groups_roundtrip(self):
        g = self.c.to_groups()
        again = Clinic.from_groups(json.loads(json.dumps(g)))
        self.assertEqual(again.to_dict(), self.c.to_dict())
        self.assertEqual(again.issues(), [])

    def test_groups_validate_cross_references(self):
        g = self.c.to_groups()
        bad = json.loads(json.dumps(g))
        bad["planning"]["opening_hours"]["nowhere"] = {}
        with self.assertRaises(ClinicError):
            Clinic.from_groups(bad)
        bad = json.loads(json.dumps(g))
        bad["planning"]["doctor_skills"]["ghost"] = []
        with self.assertRaises(ClinicError):
            Clinic.from_groups(bad)
        bad = json.loads(json.dumps(g))
        bad["planning"]["presence"] = {"2026-11-23": {"doctors": {"lim": "nowhere"}}}
        with self.assertRaises(ClinicError):
            Clinic.from_groups(bad)
        bad = json.loads(json.dumps(g))
        bad["schedule"]["bookings"][0]["room_id"] = "ghost"
        with self.assertRaises(ClinicError):
            Clinic.from_groups(bad)
        with self.assertRaises(ClinicError):
            Clinic.from_groups({"extras": {}})

    def test_save_dir_writes_three_files_and_only_what_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "data")
            self.assertEqual(sorted(self.c.save_dir(d)), sorted(GROUP_FILES.values()))
            self.assertEqual(self.c.save_dir(d), [])  # nothing changed -> nothing rewritten
            self.assertEqual(Clinic.load_dir(d).to_dict(), self.c.to_dict())
            # a booking change touches only schedule.json
            self.c.set_blocked("room", "central_laser_a", date(2026, 11, 23), t("10:00"), t("11:00"), True)
            self.assertEqual(self.c.save_dir(d), ["schedule.json"])
            # a roster tick touches only planning.json
            self.c.set_presence("staff", "s1", date(2026, 11, 23), None)
            self.assertEqual(self.c.save_dir(d), ["planning.json"])
            # adding a staff member touches only resources.json
            data = self.c.to_dict()
            data["staff"].append({"id": "new", "name": "New"})
            Clinic.from_dict(data).save_dir(d)
            with open(os.path.join(d, "resources.json")) as f:
                self.assertIn("New", f.read())

    def test_json_files_are_human_friendly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.c.save_dir(tmp)
            with open(os.path.join(tmp, "planning.json")) as f:
                text = f.read()
        self.assertIn('"mon": ["09:00", "19:00"]', text)  # short lists on one line

    def test_load_dir_with_missing_files_and_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(Clinic.dir_has_data(tmp))
            self.assertEqual(Clinic.load_dir(tmp).to_dict()["doctors"], [])
            with open(os.path.join(tmp, "schedule.json"), "w") as f:
                f.write("{nope")
            with self.assertRaises(ClinicError):
                Clinic.load_dir(tmp)


class ValidationTests(unittest.TestCase):
    def test_from_dict_validation(self):
        data = small_clinic().to_dict()
        bad = dict(data, doctors=[{"id": "d1", "name": "X", "treatments": ["ghost"]}])
        with self.assertRaises(ClinicError):
            Clinic.from_dict(bad)
        bad = dict(data, treatments=data["treatments"] + [
            {"id": "odd", "name": "Odd", "duration_minutes": 20}])
        with self.assertRaises(ClinicError):
            Clinic.from_dict(bad)
        bad = dict(data, locations=data["locations"] + [data["locations"][0]])
        with self.assertRaises(ClinicError):
            Clinic.from_dict(bad)
        bad = dict(data, presence={"2026-11-23": {"nurses": {"s1": "a"}}})
        with self.assertRaises(ClinicError):
            Clinic.from_dict(bad)


class SampleTests(unittest.TestCase):
    def test_sample_is_consistent_and_bookable(self):
        c = build_sample(date(2026, 11, 20), days=28)
        self.assertEqual(c.issues(), [])
        self.assertEqual(len(c.locations), 3)
        slots = c.find_slots("hifu", date(2026, 11, 24))  # Tue
        self.assertTrue(any(slots.values()))


if __name__ == "__main__":
    unittest.main()
