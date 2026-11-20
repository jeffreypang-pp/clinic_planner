import os
import tempfile
import unittest
from datetime import date

from clinic.model import Clinic, ClinicError, NotFound, format_slot, parse_time
from clinic.sample import build_sample

MON = date(2026, 11, 23)  # a Monday
SUN = date(2026, 11, 22)


def small_clinic() -> Clinic:
    """2 locations; 1 laser doctor, 1 botox doctor, 2 staff."""
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


def roster(c: Clinic, role, pid, loc, start, end, day=MON):
    c.add_shift(role, pid, loc, day, parse_time(start), parse_time(end))


class TimeTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(parse_time("09:15"), 37)
        self.assertEqual(format_slot(37), "09:15")
        self.assertEqual(parse_time("24:00"), 96)

    def test_rejects_off_grid(self):
        for bad in ("09:10", "25:00", "9", "ab:cd", "24:15"):
            with self.assertRaises(ClinicError):
                parse_time(bad)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.c = small_clinic()
        roster(self.c, "doctor", "d1", "a", "09:00", "17:00")
        roster(self.c, "staff", "s1", "a", "09:00", "17:00")

    def avail(self, treatment="laser", loc="a", start="10:00", day=MON):
        return self.c.check_availability(treatment, loc, day, parse_time(start))

    def test_available_when_all_resources_present(self):
        r = self.avail()
        self.assertTrue(r["available"], r["reasons"])
        self.assertEqual([d["id"] for d in r["doctors"]], ["d1"])
        self.assertEqual([x["id"] for x in r["rooms"]], ["a_laser"])  # equipment-limited

    def test_outside_opening_hours_and_closed_day(self):
        self.assertFalse(self.avail(start="08:45")["available"])
        self.assertFalse(self.avail(start="16:15")["available"])  # 60 min would end 17:15
        self.assertTrue(self.avail(start="16:00")["available"])   # ends exactly at close
        r = self.avail(day=SUN)
        self.assertFalse(r["available"])
        self.assertIn("closed", r["reasons"][0])

    def test_doctor_skill_limits_treatment(self):
        roster(self.c, "doctor", "d2", "a", "09:00", "17:00")
        roster(self.c, "staff", "s2", "a", "09:00", "17:00")
        self.assertEqual([d["id"] for d in self.avail("laser")["doctors"]], ["d1"])
        self.assertEqual({d["id"] for d in self.avail("botox")["doctors"]}, {"d1", "d2"})

    def test_doctor_must_be_on_shift_at_that_location_for_whole_span(self):
        self.assertFalse(self.avail(loc="b", start="11:00")["available"])  # d1 is at A
        self.c.shifts.clear()
        roster(self.c, "doctor", "d1", "a", "09:00", "10:30")
        roster(self.c, "staff", "s1", "a", "09:00", "17:00")
        self.assertFalse(self.avail(start="10:00")["available"])  # shift ends mid-treatment
        self.assertTrue(self.avail(start="09:30")["available"])

    def test_adjacent_shifts_cover_a_span(self):
        self.c.shifts.clear()
        roster(self.c, "doctor", "d1", "a", "09:00", "10:30")
        roster(self.c, "doctor", "d1", "a", "10:30", "12:00")
        roster(self.c, "staff", "s1", "a", "09:00", "17:00")
        self.assertTrue(self.avail(start="10:00")["available"])

    def test_staff_count_required(self):
        roster(self.c, "doctor", "d2", "a", "09:00", "17:00")
        r = self.avail("botox")  # needs 2 staff, only s1 rostered
        self.assertFalse(r["available"])
        self.assertTrue(any("2 staff" in x for x in r["reasons"]))
        roster(self.c, "staff", "s2", "a", "09:00", "17:00")
        self.assertTrue(self.avail("botox")["available"])

    def test_room_blocking(self):
        self.c.book("laser", "a", MON, parse_time("10:00"))
        roster(self.c, "staff", "s2", "a", "09:00", "17:00")
        roster(self.c, "doctor", "d2", "a", "09:00", "17:00")
        r = self.avail("laser", start="10:30")  # only laser room is taken
        self.assertFalse(r["available"])
        self.assertTrue(any("room" in x for x in r["reasons"]))
        self.assertTrue(self.avail("laser", start="11:00")["available"])  # back-to-back ok

    def test_double_booking_person_blocked(self):
        roster(self.c, "staff", "s2", "a", "09:00", "17:00")
        b = self.c.book("botox", "a", MON, parse_time("10:00"))
        self.assertEqual(b.doctor_id, "d1")
        # d1 and both staff are now used; d2 not rostered -> no second botox at 10:15
        self.assertFalse(self.avail("botox", start="10:15")["available"])

    def test_person_cannot_be_in_two_places(self):
        with self.assertRaises(ClinicError):  # d1 is at A until 17:00
            roster(self.c, "doctor", "d1", "b", "16:00", "17:30")
        roster(self.c, "doctor", "d1", "b", "17:00", "18:00")  # leaving A then arriving at B is fine

    def test_shuffle_between_locations(self):
        self.c.shifts.clear()
        roster(self.c, "doctor", "d1", "a", "09:00", "13:00")
        roster(self.c, "doctor", "d1", "b", "13:00", "18:00")
        roster(self.c, "staff", "s1", "a", "09:00", "13:00")
        roster(self.c, "staff", "s1", "b", "13:00", "18:00")
        self.assertTrue(self.avail("botox", "a", "09:00")["available"] is False)  # needs 2 staff
        self.assertTrue(self.avail("laser", "a", "09:00")["available"])
        self.assertFalse(self.avail("laser", "b", "11:00")["available"])
        self.assertTrue(self.avail("laser", "b", "14:00")["available"] is False)  # B has no laser room

    def test_booking_blocks_person_at_other_location(self):
        c = small_clinic()
        roster(c, "doctor", "d1", "a", "09:00", "17:00")
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        roster(c, "staff", "s2", "a", "09:00", "17:00")
        c.book("laser", "a", MON, parse_time("10:00"))
        self.assertEqual(c.timetable("a", MON)["doctors"][0]["cells"][parse_time("10:00")][:7], "booked:")

    def test_find_slots(self):
        slots = self.c.find_slots("laser", MON)
        self.assertEqual(slots["a"][0], "09:00")
        self.assertEqual(slots["a"][-1], "16:00")
        self.assertEqual(slots["b"], [])  # nobody rostered at B

    def test_unknown_ids(self):
        with self.assertRaises(NotFound):
            self.c.check_availability("nope", "a", MON, 40)


class BookingTests(unittest.TestCase):
    def test_book_auto_assigns_and_cancel_frees(self):
        c = small_clinic()
        roster(c, "doctor", "d1", "a", "09:00", "17:00")
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        b = c.book("laser", "a", MON, parse_time("09:00"), client="X")
        self.assertEqual((b.doctor_id, b.room_id, b.staff_ids), ("d1", "a_laser", ["s1"]))
        with self.assertRaises(ClinicError):
            c.book("laser", "a", MON, parse_time("09:30"))
        c.cancel_booking(b.id)
        c.book("laser", "a", MON, parse_time("09:30"))

    def test_explicit_unqualified_doctor_rejected(self):
        c = small_clinic()
        roster(c, "doctor", "d1", "a", "09:00", "17:00")
        roster(c, "doctor", "d2", "a", "09:00", "17:00")
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        with self.assertRaises(ClinicError):
            c.book("laser", "a", MON, parse_time("09:00"), doctor_id="d2")

    def test_removing_shift_is_reported_as_issue(self):
        c = small_clinic()
        sh = c.add_shift("doctor", "d1", "a", MON, parse_time("09:00"), parse_time("17:00"))
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        c.book("laser", "a", MON, parse_time("09:00"))
        self.assertEqual(c.issues(), [])
        c.remove_shift(sh.id)
        issues = c.issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("D1", issues[0])

    def test_changing_opening_hours_is_reported_as_issue(self):
        c = small_clinic()
        roster(c, "doctor", "d1", "a", "09:00", "17:00")
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        c.book("laser", "a", MON, parse_time("09:00"))
        c.set_opening_hours("a", {0: (parse_time("10:00"), parse_time("17:00"))})
        self.assertTrue(any("opening hours" in i for i in c.issues()))


class OpeningHoursTests(unittest.TestCase):
    def test_adjustable_hours_and_overrides(self):
        c = small_clinic()
        loc = c.locations["a"]
        self.assertEqual(loc.hours_on(MON), (36, 68))
        c.set_opening_hours("a", {0: (parse_time("08:00"), parse_time("20:15")), 1: (40, 60)})
        self.assertEqual(loc.hours_on(MON), (32, 81))
        self.assertIsNotNone(loc.hours_on(date(2026, 11, 24)))
        loc.date_overrides[MON] = None  # closed that specific day
        self.assertIsNone(loc.hours_on(MON))
        with self.assertRaises(ClinicError):
            c.set_opening_hours("a", {0: (50, 40)})


class TimetableTests(unittest.TestCase):
    def test_cells(self):
        c = small_clinic()
        roster(c, "doctor", "d1", "a", "09:00", "12:00")
        roster(c, "doctor", "d1", "b", "13:00", "17:00")
        roster(c, "staff", "s1", "a", "09:00", "17:00")
        b = c.book("laser", "a", MON, parse_time("09:00"))
        ta, tb = c.timetable("a", MON), c.timetable("b", MON)
        d1 = next(d for d in ta["doctors"] if d["id"] == "d1")["cells"]
        self.assertEqual(d1[parse_time("09:00")], f"booked:{b.id}")
        self.assertEqual(d1[parse_time("10:00")], "free")
        self.assertEqual(d1[parse_time("12:00")], "off")
        self.assertEqual(d1[parse_time("14:00")], "away:b")
        d1b = next(d for d in tb["doctors"] if d["id"] == "d1")["cells"]
        self.assertEqual(d1b[parse_time("09:30")], "away:a")  # booked elsewhere
        self.assertEqual(d1b[parse_time("14:00")], "free")
        room = next(r for r in ta["rooms"] if r["id"] == "a_laser")["cells"]
        self.assertEqual(room[parse_time("08:45")], "closed")
        self.assertEqual(room[parse_time("09:45")], f"booked:{b.id}")
        self.assertEqual(len(ta["bookings"]), 1)
        self.assertEqual(len(tb["rooms"]), 1)  # rooms only for that location


class PersistenceTests(unittest.TestCase):
    def test_full_roundtrip_through_file(self):
        c = build_sample(date(2026, 11, 20), days=14)
        before = c.to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "clinic.json")
            c.save(path)
            loaded = Clinic.load(path)
        self.assertEqual(loaded.to_dict(), before)
        self.assertEqual(loaded.issues(), [])
        self.assertTrue(loaded.bookings)

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
        bad = dict(data, shifts=[
            {"role": "doctor", "person_id": "d1", "location_id": "a", "date": "2026-11-23",
             "start": "09:00", "end": "12:00"},
            {"role": "doctor", "person_id": "d1", "location_id": "b", "date": "2026-11-23",
             "start": "11:45", "end": "14:00"}])
        with self.assertRaises(ClinicError):
            Clinic.from_dict(bad)


class EditAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.c = small_clinic()

    def spans(self, role="doctor", pid="d1", day=MON):
        return sorted((s.location_id, format_slot(s.start), format_slot(s.end))
                      for s in self.c.shifts.values()
                      if s.role == role and s.person_id == pid and s.date == day)

    def paint(self, loc, start, end, available=True, role="doctor", pid="d1"):
        self.c.set_person_availability(role, pid, loc, MON, parse_time(start), parse_time(end), available)

    def test_paint_available_and_merge(self):
        self.paint("a", "09:00", "11:00")
        self.paint("a", "11:00", "12:00")   # adjacent -> merged into one shift
        self.paint("a", "10:00", "10:30")   # already covered -> no change
        self.assertEqual(self.spans(), [("a", "09:00", "12:00")])

    def test_clear_splits_shift(self):
        self.paint("a", "09:00", "17:00")
        self.paint("a", "12:00", "13:00", available=False)
        self.assertEqual(self.spans(), [("a", "09:00", "12:00"), ("a", "13:00", "17:00")])
        self.paint("a", "09:00", "17:00", available=False)
        self.assertEqual(self.spans(), [])

    def test_painting_here_moves_person_from_other_location(self):
        self.paint("a", "09:00", "17:00")
        self.paint("b", "13:00", "15:00")   # shuffle: replaces A for that span
        self.assertEqual(self.spans(), [("a", "09:00", "13:00"), ("a", "15:00", "17:00"), ("b", "13:00", "15:00")])

    def test_clearing_at_a_location_leaves_other_location(self):
        self.paint("a", "09:00", "12:00")
        self.paint("b", "12:00", "17:00")
        self.paint("a", "09:00", "17:00", available=False)  # only removes A
        self.assertEqual(self.spans(), [("b", "12:00", "17:00")])

    def test_untouched_shift_keeps_id_and_other_people_unchanged(self):
        sh = self.c.add_shift("doctor", "d1", "a", MON, parse_time("09:00"), parse_time("10:00"))
        other = self.c.add_shift("doctor", "d2", "a", MON, parse_time("09:00"), parse_time("10:00"))
        self.paint("b", "12:00", "13:00")
        self.assertIn(sh.id, self.c.shifts)
        self.assertIn(other.id, self.c.shifts)

    def test_staff_and_validation(self):
        self.paint("a", "09:00", "10:00", role="staff", pid="s1")
        self.assertEqual(self.spans("staff", "s1"), [("a", "09:00", "10:00")])
        with self.assertRaises(ClinicError):
            self.paint("a", "10:00", "09:00")
        with self.assertRaises(NotFound):
            self.paint("nowhere", "09:00", "10:00")

    def test_painting_unavailable_flags_existing_booking(self):
        self.paint("a", "09:00", "17:00")
        self.paint("a", "09:00", "17:00", role="staff", pid="s1")
        self.c.book("laser", "a", MON, parse_time("10:00"))
        self.assertEqual(self.c.issues(), [])
        self.paint("a", "10:00", "10:15", available=False)
        self.assertTrue(self.c.bookings)  # booking kept
        self.assertTrue(any("D1" in i for i in self.c.issues()))

    def test_room_block_makes_room_unavailable(self):
        roster(self.c, "doctor", "d1", "a", "09:00", "17:00")
        roster(self.c, "staff", "s1", "a", "09:00", "17:00")
        ok = lambda t: self.c.check_availability("laser", "a", MON, parse_time(t))
        self.assertTrue(ok("10:00")["available"])
        self.c.set_room_availability("a_laser", MON, parse_time("10:30"), parse_time("12:00"), False)
        self.assertFalse(ok("10:00")["available"])   # 10:00-11:00 overlaps the block
        self.assertTrue(ok("09:00")["available"])    # ends exactly when block starts
        self.assertFalse(ok("11:30")["available"])
        self.assertTrue(ok("12:00")["available"])
        cells = next(r for r in self.c.timetable("a", MON)["rooms"] if r["id"] == "a_laser")["cells"]
        self.assertEqual(cells[parse_time("10:15")], "free")
        self.assertEqual(cells[parse_time("10:30")], "blocked")
        self.assertEqual(cells[parse_time("12:00")], "free")
        self.c.set_room_availability("a_laser", MON, parse_time("10:00"), parse_time("13:00"), True)
        self.assertTrue(ok("10:00")["available"])
        self.assertEqual(self.c.room_blocks, {})

    def test_blocking_room_flags_existing_booking_and_roundtrips(self):
        roster(self.c, "doctor", "d1", "a", "09:00", "17:00")
        roster(self.c, "staff", "s1", "a", "09:00", "17:00")
        self.c.book("laser", "a", MON, parse_time("10:00"))
        self.c.set_room_availability("a_laser", MON, parse_time("09:00"), parse_time("10:00"), False)
        self.c.set_room_availability("a_laser", MON, parse_time("10:30"), parse_time("11:00"), False)
        self.assertTrue(any("Laser" in i for i in self.c.issues()))
        data = self.c.to_dict()
        self.assertEqual([(b["start"], b["end"]) for b in data["room_blocks"]],
                         [("09:00", "10:00"), ("10:30", "11:00")])
        self.assertEqual(Clinic.from_dict(data).to_dict(), data)


class SampleTests(unittest.TestCase):
    def test_sample_is_consistent_and_bookable(self):
        c = build_sample(date(2026, 11, 20), days=28)
        self.assertEqual(c.issues(), [])
        self.assertEqual(len(c.locations), 3)
        slots = c.find_slots("hifu", date(2026, 11, 24))  # Tue
        self.assertTrue(any(slots.values()))


if __name__ == "__main__":
    unittest.main()
