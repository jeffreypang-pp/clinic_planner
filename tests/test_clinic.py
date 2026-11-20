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
    """2 locations. laser = numbing 15 + doctor 30 + staff 15 (60 min, needs a laser room);
    botox = doctor 30 only; facial = staff-only 30.  d1 does laser+botox, d2 botox; s1, s2."""
    return Clinic.from_dict({
        "locations": [
            {"id": "a", "name": "A", "opening_hours": {"mon": ["09:00", "17:00"]},
             "rooms": [{"id": "a_laser", "name": "Laser", "equipment": ["laser"]},
                       {"id": "a_laser2", "name": "Laser 2", "equipment": ["laser"]},
                       {"id": "a_plain", "name": "Plain", "equipment": []}]},
            {"id": "b", "name": "B", "opening_hours": {"mon": ["10:00", "18:00"]},
             "rooms": [{"id": "b_plain", "name": "Plain", "equipment": []}]},
        ],
        "treatments": [
            {"id": "laser", "name": "Laser", "numbing_minutes": 15, "doctor_minutes": 30,
             "staff_minutes": 15, "required_equipment": ["laser"]},
            {"id": "botox", "name": "Botox", "doctor_minutes": 30},
            {"id": "facial", "name": "Facial", "staff_minutes": 30},
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


class TreatmentTests(unittest.TestCase):
    def test_steps_are_laid_out_back_to_back(self):
        c = small_clinic()
        steps = [(x.name, format_slot(x.start), format_slot(x.end)) for x in c.treatments["laser"].steps(t("10:00"))]
        self.assertEqual(steps, [("numbing", "10:00", "10:15"), ("doctor", "10:15", "10:45"),
                                 ("staff", "10:45", "11:00")])
        self.assertEqual(c.treatments["laser"].duration_slots * 15, 60)

    def test_zero_minute_steps_are_skipped_and_staff_only(self):
        c = small_clinic()
        self.assertEqual([x.name for x in c.treatments["botox"].steps(0)], ["doctor"])
        facial = c.treatments["facial"]
        self.assertFalse(facial.needs_doctor)
        self.assertEqual([x.name for x in facial.steps(0)], ["staff"])
        self.assertTrue(c.treatments["laser"].needs_doctor)

    def test_validation(self):
        base = small_clinic().to_dict()
        def with_treatment(**kw):
            return dict(base, doctors=[], treatments=[dict({"id": "x", "name": "X"}, **kw)])
        Clinic.from_dict(with_treatment(doctor_minutes=15))
        for bad in (dict(), dict(doctor_minutes=20), dict(doctor_minutes=-15), dict(doctor_minutes="30"),
                    dict(numbing_minutes=15),                    # numbing without a doctor treatment
                    dict(duration_minutes=30), dict(doctor_minutes=15, staff_required=1)):
            with self.assertRaises(ClinicError, msg=str(bad)):
                Clinic.from_dict(with_treatment(**bad))


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
        self.assertEqual([x["id"] for x in r["rooms"]], ["a_laser", "a_laser2"])  # equipment-limited
        self.assertEqual([(x["name"], x["start"], x["end"]) for x in r["steps"]],
                         [("numbing", "10:00", "10:15"), ("doctor", "10:15", "10:45"), ("staff", "10:45", "11:00")])
        self.assertTrue(all([p["id"] for p in x["staff"]] == ["s1"] for x in r["steps"]))

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
        self.assertTrue(self.avail("botox", "b", "11:00")["available"])
        self.assertFalse(self.avail("laser", "b", "11:00")["available"])   # B has no laser room
        self.assertFalse(self.avail("laser", "a", "10:00")["available"])   # and they left A

    def test_untick_removes_availability(self):
        tick(self.c, "doctor", "d1", None)
        self.assertFalse(self.avail()["available"])

    def test_every_step_needs_a_staff_member(self):
        """Numbing, doctor treatment and staff treatment each need 1 staff available in their own window."""
        for window, step in (("10:00", "numbing"), ("10:15", "doctor treatment"), ("10:45", "staff treatment")):
            c = small_clinic()
            tick(c, "doctor", "d1", "a")
            tick(c, "staff", "s1", "a")
            c.set_blocked("staff", "s1", MON, t(window), t(window) + 1, True)   # the only staff, 15 min
            r = c.check_availability("laser", "a", MON, t("10:00"))
            self.assertFalse(r["available"], step)
            self.assertTrue(any(f"Not enough staff for the {step}" in x for x in r["reasons"]), (step, r["reasons"]))
            tick(c, "staff", "s2", "a")                                          # a second person fixes it
            self.assertTrue(c.check_availability("laser", "a", MON, t("10:00"))["available"], step)

    def test_doctor_is_only_needed_for_the_doctor_step(self):
        tick(self.c, "staff", "s2", "a")
        self.c.book("laser", "a", MON, t("10:00"))          # d1 busy 10:15-10:45 only
        self.assertFalse(self.avail(start="10:00")["available"])    # doctor steps coincide
        self.assertFalse(self.avail(start="09:45")["available"])    # doctor 10:00-10:30 overlaps 10:15-10:45
        self.assertTrue(self.avail(start="10:30")["available"])     # numbing 10:30 overlaps, doctor 10:45 free
        self.assertTrue(self.avail(start="09:30")["available"])     # doctor 09:45-10:15 ends as the other starts
        self.assertTrue(self.avail(start="09:15")["available"])     # doctor 09:30-10:00 is clear of 10:15-10:45

    def test_staff_only_treatment_needs_no_doctor(self):
        tick(self.c, "doctor", "d1", None)
        r = self.avail("facial", start="10:00")
        self.assertTrue(r["available"], r["reasons"])
        self.assertEqual((r["needs_doctor"], r["doctors"]), (False, []))
        self.assertEqual([x["name"] for x in r["steps"]], ["staff"])
        tick(self.c, "staff", "s1", None)
        self.assertFalse(self.avail("facial")["available"])         # but it does need a staff member

    def test_room_is_held_for_the_whole_treatment(self):
        tick(self.c, "staff", "s2", "a")
        self.c.set_blocked("room", "a_laser2", MON, t("09:00"), t("17:00"), True)   # only a_laser usable
        self.c.book("laser", "a", MON, t("10:00"))                                  # a_laser 10:00-11:00
        r = self.avail(start="10:45")      # staff step of the first overlaps, but its doctor is free at 11:00
        self.assertFalse(r["available"])
        self.assertTrue(any("room" in x for x in r["reasons"]))
        self.assertTrue(self.avail(start="11:00")["available"])                      # back-to-back is fine

    def test_equipment_decides_which_rooms_fit(self):
        self.assertEqual({r["id"] for r in self.avail("laser")["rooms"]}, {"a_laser", "a_laser2"})
        self.assertEqual({r["id"] for r in self.avail("facial")["rooms"]}, {"a_laser", "a_laser2", "a_plain"})

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

    def ok(self, start, treatment="laser"):
        return self.c.check_availability(treatment, "a", MON, t(start))

    def test_room_block(self):
        self.c.set_blocked("room", "a_laser", MON, t("10:30"), t("12:00"), True)
        self.c.set_blocked("room", "a_laser2", MON, t("10:30"), t("12:00"), True)
        self.assertTrue(self.ok("10:00")["available"] is False)   # 10:00-11:00 overlaps
        self.assertTrue(self.ok("09:30")["available"])            # ends exactly when the block starts
        self.assertFalse(self.ok("11:30")["available"])
        self.assertTrue(self.ok("12:00")["available"])
        self.c.set_blocked("room", "a_laser", MON, t("10:00"), t("13:00"), False)
        self.assertTrue(self.ok("10:00")["available"])            # a_laser is back
        self.c.set_blocked("room", "a_laser2", MON, t("10:00"), t("13:00"), False)
        self.assertEqual(self.c.blocks, {})

    def test_doctor_block_only_matters_during_the_doctor_step(self):
        self.c.set_blocked("doctor", "d1", MON, t("10:00"), t("10:15"), True)   # laser's numbing window
        self.assertTrue(self.ok("10:00")["available"])
        self.c.set_blocked("doctor", "d1", MON, t("10:00"), t("10:15"), False)
        self.c.set_blocked("doctor", "d1", MON, t("10:15"), t("10:30"), True)   # inside the doctor step
        self.assertFalse(self.ok("10:00")["available"])
        self.c.set_blocked("doctor", "d1", MON, t("10:15"), t("10:30"), False)
        self.c.set_blocked("doctor", "d1", MON, t("12:00"), t("13:00"), True)   # lunch
        self.assertFalse(self.ok("11:45", "botox")["available"])
        self.assertTrue(self.ok("13:00", "botox")["available"])

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

    def test_book_auto_assigns_doctor_and_room_and_leaves_staff_for_later(self):
        c = self.c
        b = c.book("laser", "a", MON, t("09:00"), client="X")
        self.assertEqual((b.doctor_id, b.room_id), ("d1", "a_laser"))
        self.assertEqual(b.staff, {})                 # staff are "assign later" unless named
        with self.assertRaises(ClinicError):
            c.book("laser", "a", MON, t("09:15"))     # the only doctor is busy
        c.cancel_booking(b.id)
        c.book("laser", "a", MON, t("09:15"))

    def test_steps_can_name_different_staff_and_skipped_steps_have_none(self):
        tick(self.c, "staff", "s2", "a")
        b = self.c.book("laser", "a", MON, t("10:00"), staff={"numbing": "s1", "doctor": "s2"})
        self.assertEqual(b.staff, {"numbing": "s1", "doctor": "s2"})                 # the staff step: assign later
        botox = self.c.book("botox", "a", MON, t("14:00"))
        self.assertEqual(self.c.describe_booking(botox)["unassigned_steps"], 1)      # only the doctor step exists
        facial = self.c.book("facial", "a", MON, t("15:00"))
        self.assertEqual((facial.doctor_id, facial.staff), (None, {}))

    def test_explicit_choices_are_validated(self):
        tick(self.c, "doctor", "d2", "a")
        with self.assertRaises(ClinicError):
            self.c.book("laser", "a", MON, t("09:00"), doctor_id="d2")              # unqualified
        with self.assertRaises(ClinicError):
            self.c.book("facial", "a", MON, t("09:00"), doctor_id="d1")             # staff-only
        with self.assertRaises(ClinicError):
            self.c.book("laser", "a", MON, t("09:00"), room_id="a_plain")           # wrong equipment
        self.assertEqual(self.c.book("laser", "a", MON, t("09:00"), room_id="a_laser2").room_id, "a_laser2")

    def test_unticking_is_reported_not_destructive(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.assertEqual(self.c.issues(), [])
        tick(self.c, "doctor", "d1", None)
        self.assertTrue(self.c.bookings)
        issues = self.c.issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("D1", issues[0])

    def test_moving_a_staff_member_away_is_reported_as_a_shortage(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.assertEqual(self.c.issues(), [])
        tick(self.c, "staff", "s1", "b")               # the only staff member leaves location A
        self.assertTrue(any("Staff shortage at A" in i and "09:00-10:00" in i for i in self.c.issues()))

    def test_blocking_after_booking_is_reported(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.c.set_blocked("room", "a_laser", MON, t("09:30"), t("10:00"), True)
        self.assertTrue(any("room Laser" in i for i in self.c.issues()))
        self.c.set_blocked("room", "a_laser", MON, t("09:30"), t("10:00"), False)
        self.assertEqual(self.c.issues(), [])
        self.c.set_blocked("doctor", "d1", MON, t("09:00"), t("09:15"), True)        # numbing window only
        self.assertEqual(self.c.issues(), [])
        self.c.set_blocked("doctor", "d1", MON, t("09:15"), t("09:30"), True)        # doctor step
        self.assertTrue(any("doctor D1" in i for i in self.c.issues()))

    def test_changing_opening_hours_is_reported(self):
        self.c.book("laser", "a", MON, t("09:00"))
        self.c.set_opening_hours("a", {0: (t("10:00"), t("17:00"))})
        self.assertTrue(any("opening hours" in i for i in self.c.issues()))


class CustomTreatmentTests(unittest.TestCase):
    """A treatment is added by giving numbing / doctor / staff minutes and the room."""

    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "staff", "s1", "a")
        tick(self.c, "staff", "s2", "a")

    def add(self, start="10:00", n=15, d=30, st=15, room="a_plain", **kw):
        return self.c.book_custom("a", MON, t(start), n, d, st, room, **kw)

    def test_the_only_rule_on_times(self):
        """If the doctor treatment time is 0, the numbing time must be 0."""
        self.add(n=0, d=30, st=0)                                    # doctor only
        self.add(start="11:00", n=15, d=30, st=15)                   # all three
        self.add(start="12:00", n=0, d=0, st=30)                     # staff alone
        self.add(start="13:00", n=0, d=15, st=0)
        with self.assertRaises(ClinicError) as cm:
            self.add(start="14:00", n=15, d=0, st=15)                # numbing but no doctor treatment
        self.assertIn("numbing", str(cm.exception))
        for bad in ((0, 0, 0), (10, 30, 0), (-15, 30, 0), (15, "30", 0), (0, 0, 7)):
            with self.assertRaises(ClinicError, msg=str(bad)):
                self.add(start="15:00", n=bad[0], d=bad[1], st=bad[2])
        self.assertEqual(len(self.c.bookings), 4)                    # the invalid ones booked nothing

    def test_books_into_the_chosen_room_and_lays_out_steps(self):
        b = self.add(room="a_laser2", name="Custom thing", client="Zed")
        self.assertEqual((b.room_id, b.name, b.client, b.treatment_id), ("a_laser2", "Custom thing", "Zed", None))
        self.assertEqual([(x.name, format_slot(x.start), format_slot(x.end)) for x in b.steps()],
                         [("numbing", "10:00", "10:15"), ("doctor", "10:15", "10:45"), ("staff", "10:45", "11:00")])
        self.assertEqual((b.doctor_id, b.staff), ("d1", {}))                          # staff: assign later
        d = self.c.describe_booking(b)
        self.assertEqual((d["treatment"], d["room"], d["doctor_minutes"]), ("Custom thing", "Laser 2", 30))
        self.assertEqual((d["unassigned_steps"], d["staff"]), (3, []))

    def test_room_is_required_and_must_be_free(self):
        with self.assertRaises(ClinicError):
            self.c.book_custom("a", MON, t("10:00"), 15, 30, 15, "")
        with self.assertRaises(ClinicError):
            self.add(room="b_plain")                                 # a room at another location
        self.add(room="a_plain")
        tick(self.c, "doctor", "d2", "a")
        with self.assertRaises(ClinicError) as cm:
            self.add(start="10:30", room="a_plain")                  # overlaps the first in the same room
        self.assertIn("Room Plain", str(cm.exception))
        self.add(start="10:30", room="a_laser")                      # another room is fine
        self.c.set_blocked("room", "a_laser2", MON, t("12:00"), t("13:00"), True)
        with self.assertRaises(ClinicError):
            self.add(start="12:15", room="a_laser2")                 # blocked room

    def test_templates_are_optional_so_no_skill_or_equipment_limits_without_one(self):
        tick(self.c, "doctor", "d1", None)
        tick(self.c, "doctor", "d2", "a")                            # d2 only knows botox
        b = self.add(room="a_plain")                                 # no template: any doctor may do it
        self.assertEqual(b.doctor_id, "d2")
        with self.assertRaises(ClinicError):                         # linked to 'laser': d2 isn't qualified
            self.add(start="12:00", room="a_laser", treatment_id="laser")
        with self.assertRaises(ClinicError):                         # linked to 'laser': the room needs a laser
            tick(self.c, "doctor", "d1", "a")
            self.add(start="12:00", room="a_plain", treatment_id="laser")
        ok = self.add(start="12:00", room="a_laser", treatment_id="laser")
        self.assertEqual((ok.doctor_id, ok.treatment_id), ("d1", "laser"))
        self.assertEqual(self.c.describe_booking(ok)["treatment"], "Laser")   # name falls back to the template

    def test_staff_alone_needs_no_doctor_and_explicit_doctor_is_validated(self):
        tick(self.c, "doctor", "d1", None)
        b = self.add(n=0, d=0, st=45)
        self.assertEqual((b.doctor_id, b.staff), (None, {}))
        self.assertEqual(self.c.describe_booking(b)["unassigned_steps"], 1)
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "doctor", "d2", "a")
        self.assertEqual(self.add(start="12:00", doctor_id="d2").doctor_id, "d2")
        with self.assertRaises(ClinicError):
            self.add(start="13:00", n=0, d=0, st=15, doctor_id="d1")      # staff only: no doctor

    def test_availability_check_for_custom(self):
        av = self.c.check_custom("a", MON, t("10:00"), 15, 30, 15, room_id="a_plain")
        self.assertTrue(av["available"], av["reasons"])
        self.assertEqual([r["id"] for r in av["rooms"]], ["a_plain"])
        self.assertEqual([x["name"] for x in av["steps"]], ["numbing", "doctor", "staff"])
        self.assertFalse(self.c.check_custom("a", MON, t("08:00"), 15, 30, 15, room_id="a_plain")["available"])

    def test_survives_save_and_load_and_old_bookings_still_load(self):
        self.add(room="a_laser", name="Mine", client="C")
        self.add(start="12:00", n=0, d=0, st=30, room="a_plain")
        data = self.c.to_dict()
        again = Clinic.from_dict(data)
        self.assertEqual(again.to_dict(), data)
        self.assertEqual(again.issues(), [])
        first = next(b for b in data["bookings"] if b["name"] == "Mine")
        self.assertEqual((first["numbing_minutes"], first["doctor_minutes"], first["staff_minutes"]), (15, 30, 15))
        # a booking saved by the previous version only named a template: minutes come from the template
        legacy = small_clinic().to_dict()
        legacy["presence"] = data["presence"]
        legacy["bookings"] = [{"id": "old", "treatment_id": "laser", "location_id": "a", "room_id": "a_laser",
                               "doctor_id": "d1", "staff": {"numbing": "s1", "doctor": "s1", "staff": "s1"},
                               "date": "2026-11-23", "start": "09:00", "client": ""}]
        old = Clinic.from_dict(legacy)
        self.assertEqual((old.bookings["old"].numbing_slots, old.bookings["old"].doctor_slots,
                          old.bookings["old"].staff_slots), (1, 2, 1))
        self.assertEqual(old.issues(), [])
        for broken in ({"treatment_id": None},                       # neither template nor minutes
                       {"numbing_minutes": 15, "doctor_minutes": 0, "staff_minutes": 15}):
            bad = dict(legacy["bookings"][0], **broken)
            with self.assertRaises(ClinicError, msg=str(broken)):
                Clinic.from_dict(dict(legacy, bookings=[bad]))

    def test_issues_for_custom_bookings(self):
        b = self.add(room="a_plain")
        self.assertEqual(self.c.issues(), [])
        self.c.set_blocked("room", "a_plain", MON, t("10:15"), t("10:30"), True)
        self.assertTrue(any("room Plain" in i for i in self.c.issues()))
        self.c.set_blocked("room", "a_plain", MON, t("10:15"), t("10:30"), False)
        tick(self.c, "doctor", "d1", None)
        self.assertTrue(any("doctor D1" in i for i in self.c.issues()))


class StaffPoolTests(unittest.TestCase):
    """Staff are interchangeable: counted per 15-minute block, named only when asked."""

    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "doctor", "d2", "a")
        tick(self.c, "staff", "s1", "a")
        tick(self.c, "staff", "s2", "a")

    def facial(self, start, room, staff=None, **kw):        # staff alone, 30 min, needs 1 staff
        return self.c.book_custom("a", MON, t(start), 0, 0, 30, room, staff=staff, **kw)

    def b_(self, text):
        return t(text)

    # ---- the "Total staff" row -------------------------------------------------
    def test_total_staff_counts_every_step_of_every_treatment(self):
        self.c.book_custom("a", MON, t("10:00"), 15, 30, 15, "a_laser")                      # 10:00-11:00
        self.c.book_custom("a", MON, t("10:30"), 15, 30, 15, "a_laser2", doctor_id="d2")     # 10:30-11:30
        tot = self.c.timetable("a", MON)["staff_total"]
        self.assertEqual([tot["needed"][t(x)] for x in ("09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30")],
                         [0, 1, 1, 2, 2, 1, 1, 0])
        self.assertEqual(set(tot["available"][t("10:00"):t("11:00")]), {2})
        self.assertEqual(len(tot["needed"]), 96)

    def test_available_follows_ticks_and_blocks(self):
        tot = lambda: self.c.timetable("a", MON)["staff_total"]["available"]
        self.assertEqual({tot()[t("10:00")], tot()[t("20:00")]}, {2})
        self.c.set_blocked("staff", "s2", MON, t("10:00"), t("10:30"), True)
        self.assertEqual([tot()[t(x)] for x in ("09:45", "10:00", "10:15", "10:30")], [2, 1, 1, 2])
        tick(self.c, "staff", "s2", "b")
        self.assertEqual(tot()[t("12:00")], 1)
        self.assertEqual(self.c.timetable("b", MON)["staff_total"]["available"][t("12:00")], 1)

    def test_other_locations_and_days_do_not_count(self):
        tick(self.c, "staff", "s2", "b")
        self.c.book_custom("b", MON, t("10:00"), 0, 0, 30, "b_plain")
        self.assertEqual(sum(self.c.staff_demand("a", MON)), 0)
        self.assertEqual(sum(self.c.staff_demand("b", date(2026, 11, 30))), 0)
        self.assertEqual(max(self.c.staff_demand("b", MON)), 1)

    # ---- rule 1: needed <= available, in every block -------------------------------
    def test_total_needed_can_never_exceed_the_staff_available(self):
        self.facial("10:00", "a_plain")
        self.facial("10:00", "a_laser")                                       # 2 needed, 2 available
        with self.assertRaises(ClinicError) as cm:
            self.facial("10:15", "a_laser2")                                  # would need 3 at 10:15
        self.assertIn("Not enough staff", str(cm.exception))
        self.assertIn("3 staff would be needed but only 2 are available", str(cm.exception))
        self.facial("10:30", "a_laser2")                                      # the others end at 10:30: back-to-back is fine

    def test_one_block_over_is_enough_to_refuse(self):
        self.facial("10:00", "a_plain")
        self.facial("10:30", "a_laser")
        # 10:15-10:45 overlaps each of them by one block only, but they total 2 with the new one: fine;
        self.facial("10:15", "a_laser2")
        with self.assertRaises(ClinicError):                                  # a fourth at 10:15 needs 3 in block 10:15
            self.c.book_custom("a", MON, t("10:15"), 0, 0, 15, "a_plain")

    def test_blocked_and_unticked_staff_are_not_available(self):
        self.c.set_blocked("staff", "s2", MON, t("10:00"), t("10:30"), True)
        self.facial("10:00", "a_plain")                                       # 1 needed, 1 available
        with self.assertRaises(ClinicError):
            self.facial("10:00", "a_laser")                                   # 2 needed, 1 available
        self.facial("10:30", "a_laser")                                       # s2 is back
        self.facial("10:30", "a_laser2")
        tick(self.c, "staff", "s2", None)
        with self.assertRaises(ClinicError):
            self.c.book_custom("a", MON, t("10:30"), 0, 0, 15, "a_plain")    # only s1 left, already needed twice

    def test_template_treatments_obey_the_same_limit(self):
        self.c.book("facial", "a", MON, t("10:00"))
        self.c.book("facial", "a", MON, t("10:00"))
        av = self.c.check_availability("facial", "a", MON, t("10:00"))
        self.assertFalse(av["available"])
        self.assertEqual(av["steps"][0]["capacity_left"], 0)
        self.assertEqual(self.c.check_availability("facial", "a", MON, t("10:30"))["steps"][0]["capacity_left"], 2)

    # ---- rule 2: a named person can't be in two overlapping steps -------------------
    def test_a_named_staff_member_cannot_overlap_themselves(self):
        self.facial("10:00", "a_plain", staff={"staff": "s1"})
        with self.assertRaises(ClinicError) as cm:
            self.facial("10:15", "a_laser", staff={"staff": "s1"})
        self.assertIn("S1 is already in another treatment", str(cm.exception))
        self.facial("10:30", "a_laser", staff={"staff": "s1"})                 # back-to-back is fine
        self.facial("10:15", "a_laser2")                                       # an unnamed step may overlap a named one

    def test_a_named_person_still_counts_toward_the_total(self):
        self.facial("10:00", "a_plain", staff={"staff": "s1"})
        self.facial("10:00", "a_laser")                                        # 2 of 2
        with self.assertRaises(ClinicError) as cm:
            self.facial("10:00", "a_laser2")
        self.assertIn("Not enough staff", str(cm.exception))

    def test_a_named_person_must_be_here_and_not_blocked(self):
        tick(self.c, "staff", "s2", "b")
        with self.assertRaises(ClinicError) as cm:
            self.facial("10:00", "a_plain", staff={"staff": "s2"})
        self.assertIn("S2 is not in A", str(cm.exception))
        self.c.set_blocked("staff", "s1", MON, t("10:00"), t("10:15"), True)
        with self.assertRaises(ClinicError) as cm:
            self.facial("10:00", "a_plain", staff={"staff": "s1"})
        self.assertIn("blocked", str(cm.exception))
        self.facial("10:15", "a_plain", staff={"staff": "s1"})

    def test_one_person_can_do_consecutive_steps_of_the_same_treatment(self):
        named = {"numbing": "s1", "doctor": "s1", "staff": "s1"}
        b = self.c.book_custom("a", MON, t("10:00"), 15, 30, 15, "a_plain", staff=named)
        self.assertEqual(b.staff, named)
        self.assertEqual(self.c.issues(), [])

    def test_naming_a_step_that_does_not_exist_or_an_unknown_person_is_an_error(self):
        for staff in ({"numbing": "s1"}, {"extra": "s1"}):                     # botox-like: doctor step only
            with self.assertRaises(ClinicError, msg=str(staff)):
                self.c.book_custom("a", MON, t("10:00"), 0, 30, 0, "a_plain", staff=staff)
        with self.assertRaises(NotFound):
            self.c.book_custom("a", MON, t("10:00"), 0, 30, 0, "a_plain", staff={"doctor": "ghost"})
        b = self.c.book_custom("a", MON, t("10:00"), 0, 30, 0, "a_plain", staff={"numbing": None, "doctor": ""})
        self.assertEqual(b.staff, {})                                          # None / "" = assign later

    # ---- assign later ----------------------------------------------------------------
    def test_assign_later_then_name_and_unname(self):
        b = self.facial("10:00", "a_plain")
        d = self.c.describe_booking(b)
        self.assertEqual((b.staff, d["unassigned_steps"]), ({}, 1))
        self.assertEqual([o["id"] for o in d["steps"][0]["staff_options"]], ["s1", "s2"])
        self.c.set_booking_staff(b.id, {"staff": "s1"})
        self.assertEqual(b.staff, {"staff": "s1"})
        b2 = self.facial("10:15", "a_laser")
        self.assertEqual([o["id"] for o in self.c.describe_booking(b2)["steps"][0]["staff_options"]], ["s2"])
        with self.assertRaises(ClinicError) as cm:
            self.c.set_booking_staff(b2.id, {"staff": "s1"})
        self.assertIn("already in another treatment", str(cm.exception))
        self.assertEqual(b2.staff, {})                                         # all-or-nothing
        self.c.set_booking_staff(b.id, {"staff": None})
        self.assertEqual(b.staff, {})
        self.c.set_booking_staff(b2.id, {"staff": "s1"})                       # free now
        self.assertEqual(b2.staff, {"staff": "s1"})
        self.c.set_booking_staff(b2.id, {"staff": ""})
        self.assertEqual(b2.staff, {})

    def test_set_booking_staff_validation(self):
        b = self.facial("10:00", "a_plain")
        for bad in ({"numbing": "s1"}, {"nonsense": "s1"}, {"staff": "ghost"}):
            with self.assertRaises(ClinicError, msg=str(bad)):
                self.c.set_booking_staff(b.id, bad)
        with self.assertRaises(ClinicError):
            self.c.set_booking_staff(b.id, ["s1"])
        with self.assertRaises(NotFound):
            self.c.set_booking_staff("nope", {"staff": "s1"})
        tick(self.c, "staff", "s2", "b")
        with self.assertRaises(ClinicError):
            self.c.set_booking_staff(b.id, {"staff": "s2"})                    # not in this location today
        self.c.set_blocked("staff", "s1", MON, t("10:00"), t("10:15"), True)
        with self.assertRaises(ClinicError):
            self.c.set_booking_staff(b.id, {"staff": "s1"})                    # blocked in that window
        self.assertEqual(b.staff, {})

    def test_issues_report_shortages_and_broken_names(self):
        b = self.facial("10:00", "a_plain", staff={"staff": "s1"})
        self.facial("10:00", "a_laser")
        self.assertEqual(self.c.issues(), [])
        tick(self.c, "staff", "s2", None)
        issues = self.c.issues()
        self.assertTrue(any(i.startswith("Staff shortage at A on 2026-11-23 10:00-10:30") and "2 staff needed but only 1" in i
                            for i in issues), issues)
        tick(self.c, "staff", "s1", None)
        self.assertTrue(any("S1 is not in A" in i for i in self.c.issues()))
        self.assertTrue(self.c.bookings)                                       # nothing was deleted

    def test_saved_format_uses_null_for_assign_later(self):
        b = self.c.book_custom("a", MON, t("10:00"), 15, 30, 15, "a_plain", staff={"doctor": "s2"})
        data = self.c.to_dict()
        saved = next(x for x in data["bookings"] if x["id"] == b.id)
        self.assertEqual(saved["staff"], {"numbing": None, "doctor": "s2", "staff": None})
        again = Clinic.from_dict(data)
        self.assertEqual(again.bookings[b.id].staff, {"doctor": "s2"})
        self.assertEqual(again.to_dict(), data)

    def test_template_booking_can_name_staff_too(self):
        b = self.c.book("laser", "a", MON, t("10:00"), staff={"doctor": "s2"})
        self.assertEqual(b.staff, {"doctor": "s2"})
        with self.assertRaises(ClinicError):
            self.c.book("botox", "a", MON, t("12:00"), staff={"numbing": "s1"})   # botox has no numbing step

    def test_book_list_with_named_staff_stops_at_a_conflict(self):
        base = dict(numbing_minutes=0, doctor_minutes=0, staff_minutes=30)
        r = self.c.book_list("a", MON, [dict(base, room_id="a_plain", start="10:00", staff={"staff": "s1"}),
                                        dict(base, room_id="a_laser", start="10:15", staff={"staff": "s1"}),
                                        dict(base, room_id="a_laser2", start="11:00")])
        self.assertEqual([x["status"] for x in r["results"]], ["booked", "failed", "not_attempted"])
        self.assertTrue(any("already in another treatment" in m for m in r["results"][1]["reasons"]))
        r = self.c.book_list("a", MON, [dict(base, room_id="a_plain", start="12:00"),
                                        dict(base, room_id="a_laser", start="12:00"),
                                        dict(base, room_id="a_laser2", start="12:00")])   # 3 needed, 2 available
        self.assertEqual([x["status"] for x in r["results"]], ["booked", "booked", "failed"])
        self.assertTrue(any("Not enough staff" in m for m in r["results"][2]["reasons"]))


class BookListTests(unittest.TestCase):
    """Go down a list of (treatment, proposed start); stop at the first that can't be done."""

    def setUp(self):
        self.c = small_clinic()
        tick(self.c, "doctor", "d1", "a")
        tick(self.c, "staff", "s1", "a")
        tick(self.c, "staff", "s2", "a")

    def run_list(self, items, **kw):
        return self.c.book_list("a", MON, items, **kw)

    @staticmethod
    def statuses(r):
        return [x["status"] for x in r["results"]]

    def test_all_feasible_items_are_booked_in_order(self):
        r = self.run_list([{"treatment_id": "laser", "start": "09:00", "client": "A"},
                           {"treatment_id": "botox", "start": "10:30"},
                           {"treatment_id": "facial", "start": "09:00"}])
        self.assertEqual((self.statuses(r), r["booked"], r["stopped_at"]), (["booked"] * 3, 3, None))
        self.assertEqual(len(self.c.bookings), 3)
        self.assertEqual(r["results"][0]["booking"]["steps"][0]["label"], "Numbing")

    def test_stops_at_first_failure_and_skips_the_rest(self):
        r = self.run_list([{"treatment_id": "laser", "start": "09:00"},
                           {"treatment_id": "laser", "start": "09:15"},   # d1 is in the doctor step 09:15-09:45
                           {"treatment_id": "botox", "start": "14:00"}])  # perfectly feasible, but never tried
        self.assertEqual(self.statuses(r), ["booked", "failed", "not_attempted"])
        self.assertEqual((r["booked"], r["stopped_at"]), (1, 1))
        self.assertTrue(any("doctor" in x for x in r["results"][1]["reasons"]))
        self.assertEqual(len(self.c.bookings), 1)          # the first stays booked, nothing after it
        self.assertEqual(r["results"][2]["treatment"], "Botox")   # skipped rows are still labelled

    def test_each_item_sees_what_the_earlier_ones_used(self):
        r = self.run_list([{"treatment_id": "botox", "start": "10:00"},
                           {"treatment_id": "botox", "start": "10:00"}])   # only one doctor
        self.assertEqual(self.statuses(r), ["booked", "failed"])
        self.assertTrue(any("doctor" in x for x in r["results"][1]["reasons"]))

    def test_not_enough_room_or_staff_also_stops_the_list(self):
        c = small_clinic()
        tick(c, "staff", "s1", "b")
        r = c.book_list("b", MON, [{"treatment_id": "facial", "start": "10:00"},
                                   {"treatment_id": "facial", "start": "10:15"},    # the one room + one staff are in use
                                   {"treatment_id": "facial", "start": "12:00"}])
        self.assertEqual(self.statuses(r), ["booked", "failed", "not_attempted"])
        self.assertTrue(any(("room" in x) or ("staff" in x) for x in r["results"][1]["reasons"]))
        # the room is the limit when staff are plentiful
        tick(c, "staff", "s2", "b")
        r = c.book_list("b", MON, [{"treatment_id": "facial", "start": "14:00"},
                                   {"treatment_id": "facial", "start": "14:15"}])
        self.assertTrue(any("room" in x for x in r["results"][1]["reasons"]))

    def test_dry_run_reports_but_books_nothing(self):
        items = [{"treatment_id": "laser", "start": "09:00"}, {"treatment_id": "laser", "start": "09:15"}]
        before = self.c.to_dict()
        r = self.run_list(items, dry_run=True)
        self.assertEqual((self.statuses(r), r["dry_run"]), (["booked", "failed"], True))
        self.assertEqual(self.c.to_dict(), before)
        self.assertEqual(self.statuses(self.run_list(items)), ["booked", "failed"])     # same outcome for real
        self.assertEqual(len(self.c.bookings), 1)

    def test_bad_items_fail_instead_of_raising(self):
        for bad, text in (({"treatment_id": "nope", "start": "10:00"}, "nope"),
                          ({"treatment_id": "laser", "start": "10:10"}, "15-minute"),
                          ({"treatment_id": "laser", "start": "08:00"}, "opening hours"),
                          ({"treatment_id": "laser"}, "start")):
            r = self.run_list([bad, {"treatment_id": "botox", "start": "12:00"}])
            self.assertEqual(self.statuses(r), ["failed", "not_attempted"], bad)
            self.assertIn(text, " ".join(r["results"][0]["reasons"]), bad)

    def test_items_can_target_another_location(self):
        tick(self.c, "staff", "s1", "b")
        tick(self.c, "doctor", "d2", "b")
        r = self.run_list([{"treatment_id": "botox", "start": "10:00", "location_id": "b"}])
        self.assertEqual(self.statuses(r), ["booked"])
        self.assertEqual(r["results"][0]["booking"]["location_id"], "b")

    def test_list_of_custom_treatments_stops_at_first_failure(self):
        base = dict(numbing_minutes=15, doctor_minutes=30, staff_minutes=15)
        r = self.run_list([dict(base, room_id="a_plain", start="09:00", name="One"),
                           dict(base, room_id="a_laser", start="09:15", name="Two"),    # the only doctor is busy
                           dict(base, room_id="a_laser2", start="12:00", name="Three")])
        self.assertEqual(self.statuses(r), ["booked", "failed", "not_attempted"])
        self.assertEqual([x["treatment"] for x in r["results"]], ["One", "Two", "Three"])
        self.assertTrue(any("doctor" in m for m in r["results"][1]["reasons"]))
        self.assertEqual(len(self.c.bookings), 1)

    def test_custom_items_in_a_list_validate_times_and_room(self):
        base = dict(start="10:00", numbing_minutes=15, doctor_minutes=30, staff_minutes=15)
        for item, text in ((dict(base, doctor_minutes=0), "numbing"),            # the one rule
                           (dict(base), "room"),                                  # no room chosen
                           (dict(base, room_id="b_plain"), "not at"),
                           (dict(base, room_id="a_plain", numbing_minutes=10), "multiple of 15")):
            r = self.run_list([item, dict(base, room_id="a_plain", start="12:00")])
            self.assertEqual(self.statuses(r), ["failed", "not_attempted"], item)
            self.assertIn(text, " ".join(r["results"][0]["reasons"]), item)

    def test_templates_and_custom_items_can_be_mixed(self):
        r = self.run_list([{"treatment_id": "botox", "start": "09:00"},
                           dict(numbing_minutes=0, doctor_minutes=0, staff_minutes=30, room_id="a_plain",
                                start="09:00", name="Facial-ish")], dry_run=True)
        self.assertEqual(self.statuses(r), ["booked", "booked"])
        self.assertEqual(self.c.bookings, {})

    def test_empty_list(self):
        self.assertEqual(self.run_list([]), {"results": [], "total": 0, "booked": 0, "stopped_at": None,
                                             "dry_run": False})


class ClearDayTests(unittest.TestCase):
    OTHER_DAY = date(2026, 11, 30)  # next Monday

    def setUp(self):
        c = self.c = small_clinic()
        for day in (MON, self.OTHER_DAY):
            for loc in ("a", "b"):
                tick(c, "doctor", "d1" if loc == "a" else "d2", loc, day)
                tick(c, "staff", "s1" if loc == "a" else "s2", loc, day)
        c.set_opening_hours("a", {0: (t("09:00"), t("17:00"))})
        c.set_opening_hours("b", {0: (t("09:00"), t("17:00"))})
        self.a_booking = c.book("laser", "a", MON, t("09:00"))
        self.a_other_day = c.book("laser", "a", self.OTHER_DAY, t("09:00"))
        c.set_blocked("room", "a_laser", MON, t("12:00"), t("13:00"), True)
        c.set_blocked("room", "a_laser", MON, t("15:00"), t("16:00"), True)
        c.set_blocked("doctor", "d1", MON, t("13:00"), t("14:00"), True)   # d1 is at A on Monday
        c.set_blocked("staff", "s2", MON, t("13:00"), t("14:00"), True)    # s2 is at B
        c.set_blocked("room", "b_plain", MON, t("11:00"), t("12:00"), True)
        c.set_blocked("room", "a_laser", self.OTHER_DAY, t("12:00"), t("13:00"), True)

    def test_dry_run_changes_nothing_and_reports(self):
        before = self.c.to_dict()
        r = self.c.clear_day("a", MON, dry_run=True)
        self.assertEqual((r["bookings"], r["blocks"]), (1, 3))   # 2 room spans + 1 doctor span
        self.assertEqual(sorted(r["blocked"]), ["D1", "Laser"])
        self.assertEqual(self.c.to_dict(), before)

    def test_clears_only_that_location_and_day(self):
        self.c.clear_day("a", MON)
        self.assertNotIn(self.a_booking.id, self.c.bookings)
        self.assertIn(self.a_other_day.id, self.c.bookings)                       # other day kept
        self.assertEqual(self.c.blocks.get(("room", "a_laser", MON)), None)
        self.assertEqual(self.c.blocks.get(("doctor", "d1", MON)), None)
        self.assertIn(("room", "a_laser", self.OTHER_DAY), self.c.blocks)        # other day kept
        self.assertIn(("room", "b_plain", MON), self.c.blocks)                    # other location kept
        self.assertIn(("staff", "s2", MON), self.c.blocks)                        # s2 is at B
        self.assertEqual(self.c.presence_at("doctor", "d1", MON), "a")            # ticks untouched

    def test_nothing_to_clear_and_validation(self):
        self.c.clear_day("a", MON)
        r = self.c.clear_day("a", MON)
        self.assertEqual((r["bookings"], r["blocks"]), (0, 0))
        with self.assertRaises(NotFound):
            self.c.clear_day("nowhere", MON)
        self.assertEqual(self.c.issues(), [])


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
        b = c.book("laser", "a", MON, t("09:00"), staff={"numbing": "s1", "doctor": "s1", "staff": "s1"})
        ta, tb = c.timetable("a", MON), c.timetable("b", MON)
        row = lambda tt, kind, pid: next(r for r in tt[kind] if r["id"] == pid)
        d1 = row(ta, "doctors", "d1")
        self.assertTrue(d1["here"])
        self.assertEqual(d1["at"], "a")
        # laser at 09:00 = numbing 09:00-09:15, doctor 09:15-09:45, staff 09:45-10:00
        # the doctor is only booked for their own step
        self.assertEqual([d1["cells"][t(x)] for x in ("09:00", "09:15", "09:30", "09:45")],
                         ["free", f"booked:{b.id}:doctor", f"booked:{b.id}:doctor", "free"])
        self.assertEqual(d1["cells"][t("10:00")], "free")
        self.assertEqual(d1["cells"][t("17:00")], "closed")  # present but location closed
        d2 = row(ta, "doctors", "d2")
        self.assertFalse(d2["here"])
        self.assertEqual((d2["at"], d2["cells"][t("12:00")]), ("b", "away:b"))
        self.assertEqual(row(tb, "doctors", "d1")["cells"][t("12:00")], "away:a")
        self.assertTrue(row(tb, "doctors", "d2")["here"])
        s1 = row(ta, "staff", "s1")                       # one person did all three steps
        self.assertEqual([s1["cells"][t(x)] for x in ("09:00", "09:15", "09:45", "10:00")],
                         [f"booked:{b.id}:numbing", f"booked:{b.id}:doctor", f"booked:{b.id}:staff", "free"])
        s2 = row(ta, "staff", "s2")
        self.assertEqual((s2["here"], s2["at"], s2["cells"][t("12:00")]), (False, None, "off"))
        room = next(r for r in ta["rooms"] if r["id"] == "a_laser")["cells"]
        self.assertEqual((room[t("08:45")], room[t("09:45")]), ("closed", f"booked:{b.id}:all"))
        self.assertEqual(room[t("10:00")], "free")        # the room is held 09:00-10:00 only
        self.assertEqual(len(ta["bookings"]), 1)
        self.assertEqual([x["name"] for x in ta["bookings"][0]["steps"]], ["numbing", "doctor", "staff"])
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
        # group 2: hours, treatments, skills
        self.assertEqual(set(plan), {"version", "opening_hours", "date_overrides", "treatments",
                                     "doctor_skills"})
        # group 3: who is where each day, blocked times, bookings
        self.assertEqual(set(sched), {"version", "presence", "blocks", "bookings"})
        self.assertTrue(sched["presence"])

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
        bad["schedule"]["presence"] = {"2026-11-23": {"doctors": {"lim": "nowhere"}}}
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
            # a roster tick touches only schedule.json too (presence lives there)
            self.c.set_presence("staff", "s1", date(2026, 11, 23), None)
            self.assertEqual(self.c.save_dir(d), ["schedule.json"])
            # a treatment/skill/hours change touches only planning.json
            self.c.set_opening_hours("central", {0: (t("08:00"), t("19:00"))})
            self.assertEqual(self.c.save_dir(d), ["planning.json"])
            # adding a staff member touches only resources.json
            data = self.c.to_dict()
            data["staff"].append({"id": "new", "name": "New"})
            Clinic.from_dict(data).save_dir(d)
            with open(os.path.join(d, "resources.json")) as f:
                self.assertIn("New", f.read())

    def test_presence_in_planning_is_migrated_not_lost(self):
        g = json.loads(json.dumps(self.c.to_groups()))
        g["planning"]["presence"] = g["schedule"].pop("presence")      # old layout
        migrated = Clinic.from_groups(g)
        self.assertEqual(migrated.to_dict(), self.c.to_dict())
        self.assertNotIn("presence", migrated.to_groups()["planning"])  # rewritten in the new place
        g["schedule"]["presence"] = self.c.to_groups()["schedule"]["presence"]  # in both -> refuse
        with self.assertRaises(ClinicError):
            Clinic.from_groups(g)

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
