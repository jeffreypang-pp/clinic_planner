"""Core domain model for the aesthetics-clinic planner.

Time is modelled in 15-minute slots: slot 0 = 00:00, slot 36 = 09:00,
slot 96 = 24:00 (end of day).  A span is half-open ``[start, end)``.

Concepts
--------
Location   - has weekly opening hours (plus optional per-date overrides) and rooms.
Room       - belongs to one location, owns a set of equipment tags.
Treatment  - duration (multiple of 15 min), required equipment, staff headcount.
Doctor     - may only perform a subset of treatments.
Staff      - interchangeable; any staff member can assist any treatment.
Shift      - a doctor/staff member works at ONE location for a span on a date.
             Moving between locations = several shifts on the same day.
Booking    - a treatment at a location/date/start using 1 doctor, N staff, 1 room.

A person can't be at two places at once: shifts of the same person must not
overlap, and a person can't be in two overlapping bookings.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date as Date

SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES  # 96
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FORMAT_VERSION = 1


class ClinicError(ValueError):
    """Invalid input or an operation that would break a rule."""


class NotFound(ClinicError):
    """An id that does not exist."""


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------
def parse_time(text: str) -> int:
    """'09:15' -> slot 37.  '24:00' is allowed as an end-of-day marker."""
    try:
        h, m = str(text).split(":")
        minutes = int(h) * 60 + int(m)
        ok = 0 <= int(m) < 60 and 0 <= minutes <= 24 * 60
    except ValueError:
        ok = False
    if not ok:
        raise ClinicError(f"Bad time {text!r}; expected HH:MM")
    if minutes % SLOT_MINUTES:
        raise ClinicError(f"Time {text!r} is not on a {SLOT_MINUTES}-minute boundary")
    return minutes // SLOT_MINUTES


def format_slot(slot: int) -> str:
    minutes = slot * SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_date(text) -> Date:
    if isinstance(text, Date):
        return text
    try:
        return Date.fromisoformat(str(text))
    except ValueError:
        raise ClinicError(f"Bad date {text!r}; expected YYYY-MM-DD") from None


def _check_span(start: int, end: int, what: str) -> None:
    if not (0 <= start < end <= SLOTS_PER_DAY):
        raise ClinicError(f"{what}: invalid time span {format_slot(start)}-{format_slot(end)}")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# entities
# --------------------------------------------------------------------------
@dataclass
class Treatment:
    id: str
    name: str
    duration_slots: int
    required_equipment: set[str] = field(default_factory=set)
    staff_required: int = 1


@dataclass
class Room:
    id: str
    name: str
    location_id: str
    equipment: set[str] = field(default_factory=set)


@dataclass
class Location:
    id: str
    name: str
    # weekday (0=Mon) -> (open_slot, close_slot); missing weekday = closed
    opening_hours: dict[int, tuple[int, int]] = field(default_factory=dict)
    # specific date -> (open, close), or None for "closed that day"
    date_overrides: dict[Date, tuple[int, int] | None] = field(default_factory=dict)

    def hours_on(self, day: Date) -> tuple[int, int] | None:
        if day in self.date_overrides:
            return self.date_overrides[day]
        return self.opening_hours.get(day.weekday())


@dataclass
class Doctor:
    id: str
    name: str
    treatments: set[str] = field(default_factory=set)


@dataclass
class Staff:
    id: str
    name: str


@dataclass
class Shift:
    id: str
    role: str  # "doctor" | "staff"
    person_id: str
    location_id: str
    date: Date
    start: int
    end: int


@dataclass
class Booking:
    id: str
    treatment_id: str
    location_id: str
    room_id: str
    doctor_id: str
    staff_ids: list[str]
    date: Date
    start: int
    client: str = ""


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


# --------------------------------------------------------------------------
# the clinic (all locations + timetable)
# --------------------------------------------------------------------------
class Clinic:
    def __init__(self) -> None:
        self.locations: dict[str, Location] = {}
        self.rooms: dict[str, Room] = {}
        self.treatments: dict[str, Treatment] = {}
        self.doctors: dict[str, Doctor] = {}
        self.staff: dict[str, Staff] = {}
        self.shifts: dict[str, Shift] = {}
        self.bookings: dict[str, Booking] = {}

    # ---- lookups ---------------------------------------------------------
    @staticmethod
    def _get(table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFound(f"Unknown {kind} {key!r}") from None

    def _people(self, role: str) -> dict:
        if role == "doctor":
            return self.doctors
        if role == "staff":
            return self.staff
        raise ClinicError(f"Unknown role {role!r}; expected 'doctor' or 'staff'")

    def _end(self, b: Booking) -> int:
        return b.start + self.treatments[b.treatment_id].duration_slots

    def _bookings_on(self, day: Date) -> list[Booking]:
        return [b for b in self.bookings.values() if b.date == day]

    # ---- opening hours ---------------------------------------------------
    def set_opening_hours(self, location_id: str, hours: dict[int, tuple[int, int] | None]) -> None:
        """Replace weekly opening hours. ``hours`` maps weekday -> (open, close) or None."""
        loc = self._get(self.locations, location_id, "location")
        new: dict[int, tuple[int, int]] = {}
        for wd, span in hours.items():
            if span is None:
                continue
            _check_span(span[0], span[1], f"{loc.name} {WEEKDAYS[wd]}")
            new[int(wd)] = (span[0], span[1])
        loc.opening_hours = new

    # ---- shifts ----------------------------------------------------------
    def add_shift(self, role: str, person_id: str, location_id: str, day, start: int, end: int,
                  shift_id: str | None = None) -> Shift:
        day = parse_date(day)
        person = self._get(self._people(role), person_id, role)
        loc = self._get(self.locations, location_id, "location")
        _check_span(start, end, f"Shift for {person.name}")
        for s in self.shifts.values():
            if s.role == role and s.person_id == person_id and s.date == day \
                    and _overlap(start, end, s.start, s.end):
                where = self.locations[s.location_id].name
                raise ClinicError(
                    f"{person.name} already works {format_slot(s.start)}-{format_slot(s.end)} "
                    f"at {where} on {day} (can't overlap {loc.name} "
                    f"{format_slot(start)}-{format_slot(end)})")
        shift = Shift(shift_id or _new_id("sh"), role, person_id, location_id, day, start, end)
        self.shifts[shift.id] = shift
        return shift

    def remove_shift(self, shift_id: str) -> None:
        self._get(self.shifts, shift_id, "shift")
        del self.shifts[shift_id]

    # ---- availability ----------------------------------------------------
    def _person_status(self, role: str, pid: str, loc_id: str, day: Date, start: int, end: int,
                       ignore: str | None = None) -> str:
        """'free' | 'off_shift' (not rostered at this location for the whole span) | 'busy'."""
        covered: set[int] = set()
        for s in self.shifts.values():
            if s.role == role and s.person_id == pid and s.date == day and s.location_id == loc_id:
                covered.update(range(s.start, s.end))
        if not set(range(start, end)) <= covered:
            return "off_shift"
        for b in self._bookings_on(day):
            if b.id == ignore:
                continue
            uses = b.doctor_id == pid if role == "doctor" else pid in b.staff_ids
            if uses and _overlap(start, end, b.start, self._end(b)):
                return "busy"
        return "free"

    def _room_busy(self, room_id: str, day: Date, start: int, end: int, ignore: str | None) -> bool:
        return any(b.id != ignore and b.room_id == room_id
                   and _overlap(start, end, b.start, self._end(b))
                   for b in self._bookings_on(day))

    def check_availability(self, treatment_id: str, location_id: str, day, start: int,
                           ignore_booking: str | None = None) -> dict:
        """Can ``treatment_id`` start at ``start`` on ``day`` at ``location_id``?

        Returns ``available`` plus the doctors / staff / rooms that are free for
        the whole treatment, and human-readable ``reasons`` when it isn't.
        """
        day = parse_date(day)
        t = self._get(self.treatments, treatment_id, "treatment")
        loc = self._get(self.locations, location_id, "location")
        end = start + t.duration_slots
        span = f"{format_slot(start)}-{format_slot(min(end, SLOTS_PER_DAY))}"
        reasons: list[str] = []

        if not 0 <= start < SLOTS_PER_DAY:
            raise ClinicError(f"Bad start slot {start}")
        if end > SLOTS_PER_DAY:
            reasons.append(f"{t.name} would run past midnight")
        hours = loc.hours_on(day)
        if hours is None:
            reasons.append(f"{loc.name} is closed on {day}")
        elif start < hours[0] or end > hours[1]:
            reasons.append(f"{span} is outside {loc.name} opening hours "
                           f"({format_slot(hours[0])}-{format_slot(hours[1])})")
        if end > SLOTS_PER_DAY:  # can't evaluate resources beyond the day
            return self._availability_result(t, loc, day, start, end, [], [], [], reasons)

        def free_people(role, pool):
            statuses = {p.id: self._person_status(role, p.id, location_id, day, start, end, ignore_booking)
                        for p in pool}
            return ([p for p in pool if statuses[p.id] == "free"],
                    [p for p in pool if statuses[p.id] != "off_shift"])

        # doctors
        skilled = [d for d in self.doctors.values() if t.id in d.treatments]
        free_docs, rostered_docs = free_people("doctor", skilled)
        if not skilled:
            reasons.append(f"No doctor performs {t.name}")
        elif not rostered_docs:
            reasons.append(f"No doctor qualified for {t.name} is on shift at {loc.name} for {span}")
        elif not free_docs:
            reasons.append(f"Every qualified doctor on shift at {loc.name} is already booked for {span}")

        # staff (interchangeable)
        free_staff, rostered_staff = free_people("staff", list(self.staff.values()))
        if len(free_staff) < t.staff_required:
            reasons.append(f"Needs {t.staff_required} staff but only {len(free_staff)} free at "
                           f"{loc.name} for {span} ({len(rostered_staff)} on shift)")

        # rooms
        fitting = [r for r in self.rooms.values()
                   if r.location_id == location_id and t.required_equipment <= r.equipment]
        free_rooms = [r for r in fitting if not self._room_busy(r.id, day, start, end, ignore_booking)]
        if not fitting:
            need = ", ".join(sorted(t.required_equipment)) or "any equipment"
            reasons.append(f"{loc.name} has no treatment room with the required equipment ({need})")
        elif not free_rooms:
            reasons.append(f"Every suitable room at {loc.name} is booked for {span}")

        return self._availability_result(t, loc, day, start, end, free_docs, free_staff, free_rooms, reasons)

    def _availability_result(self, t, loc, day, start, end, docs, staff, rooms, reasons) -> dict:
        ok = (not reasons and bool(docs) and bool(rooms) and len(staff) >= t.staff_required)
        return {
            "available": ok,
            "treatment_id": t.id, "location_id": loc.id, "date": day.isoformat(),
            "start": format_slot(start), "end": format_slot(min(end, SLOTS_PER_DAY)),
            "doctors": [{"id": d.id, "name": d.name} for d in docs],
            "staff": [{"id": s.id, "name": s.name} for s in staff],
            "staff_needed": t.staff_required,
            "rooms": [{"id": r.id, "name": r.name} for r in rooms],
            "reasons": reasons,
        }

    def find_slots(self, treatment_id: str, day, location_id: str | None = None) -> dict[str, list[str]]:
        """All start times (per location) at which the treatment is bookable on ``day``."""
        day = parse_date(day)
        t = self._get(self.treatments, treatment_id, "treatment")
        out: dict[str, list[str]] = {}
        for loc in self.locations.values():
            if location_id and loc.id != location_id:
                continue
            hours = loc.hours_on(day)
            starts = []
            if hours:
                for s in range(hours[0], hours[1] - t.duration_slots + 1):
                    if self.check_availability(t.id, loc.id, day, s)["available"]:
                        starts.append(format_slot(s))
            out[loc.id] = starts
        return out

    # ---- bookings --------------------------------------------------------
    def book(self, treatment_id: str, location_id: str, day, start: int, client: str = "",
             doctor_id: str | None = None, room_id: str | None = None,
             staff_ids: list[str] | None = None, booking_id: str | None = None) -> Booking:
        """Book a treatment, auto-assigning any doctor/room/staff not specified."""
        day = parse_date(day)
        av = self.check_availability(treatment_id, location_id, day, start)
        if not av["available"]:
            raise ClinicError("Not available: " + "; ".join(av["reasons"] or ["no free resources"]))
        free_docs = [d["id"] for d in av["doctors"]]
        free_rooms = [r["id"] for r in av["rooms"]]
        free_staff = [s["id"] for s in av["staff"]]
        need = av["staff_needed"]

        if doctor_id and doctor_id not in free_docs:
            raise ClinicError(f"Doctor {doctor_id!r} is not available for that treatment/time")
        if room_id and room_id not in free_rooms:
            raise ClinicError(f"Room {room_id!r} is not available for that treatment/time")
        if staff_ids is not None:
            if len(set(staff_ids)) != need or not set(staff_ids) <= set(free_staff):
                raise ClinicError(f"Need exactly {need} distinct available staff")
        b = Booking(booking_id or _new_id("bk"), treatment_id, location_id,
                    room_id or free_rooms[0], doctor_id or free_docs[0],
                    list(staff_ids) if staff_ids is not None else free_staff[:need],
                    day, start, client)
        self.bookings[b.id] = b
        return b

    def cancel_booking(self, booking_id: str) -> None:
        self._get(self.bookings, booking_id, "booking")
        del self.bookings[booking_id]

    # ---- views -----------------------------------------------------------
    def timetable(self, location_id: str, day) -> dict:
        """Per-slot state of every doctor, staff member and room at one location.

        Cell values: ``free`` | ``off`` (not rostered anywhere) | ``closed``
        (room, location closed) | ``away:<location_id>`` (working/booked at
        another location) | ``booked:<booking_id>``.
        """
        day = parse_date(day)
        loc = self._get(self.locations, location_id, "location")
        hours = loc.hours_on(day)
        todays = self._bookings_on(day)

        def person_cells(role, pid):
            cells = ["off"] * SLOTS_PER_DAY
            for s in self.shifts.values():
                if s.role == role and s.person_id == pid and s.date == day:
                    for i in range(s.start, s.end):
                        cells[i] = "free" if s.location_id == location_id else f"away:{s.location_id}"
            for b in todays:
                uses = b.doctor_id == pid if role == "doctor" else pid in b.staff_ids
                if uses:
                    tag = f"booked:{b.id}" if b.location_id == location_id else f"away:{b.location_id}"
                    for i in range(b.start, self._end(b)):
                        cells[i] = tag
            return cells

        def room_cells(room):
            cells = ["free" if hours and hours[0] <= i < hours[1] else "closed"
                     for i in range(SLOTS_PER_DAY)]
            for b in todays:
                if b.room_id == room.id:
                    for i in range(b.start, self._end(b)):
                        cells[i] = f"booked:{b.id}"
            return cells

        here = [b for b in todays if b.location_id == location_id]
        lo, hi = hours if hours else (36, 76)
        for s in self.shifts.values():
            if s.date == day and s.location_id == location_id:
                lo, hi = min(lo, s.start), max(hi, s.end)
        for b in here:
            lo, hi = min(lo, b.start), max(hi, self._end(b))

        # "here" = has a shift at this location today; the UI lists those people first.
        # Everyone is still returned so you can see who is off or at another location.
        def row(p, role):
            cells = person_cells(role, p.id)
            return {"id": p.id, "name": p.name, "cells": cells,
                    "here": any(c == "free" or c.startswith("booked:") for c in cells)}

        return {
            "location_id": loc.id, "location": loc.name, "date": day.isoformat(),
            "hours": [format_slot(hours[0]), format_slot(hours[1])] if hours else None,
            "view": [lo, hi],
            "doctors": [row(d, "doctor") for d in self.doctors.values()],
            "staff": [row(s, "staff") for s in self.staff.values()],
            "rooms": [{"id": r.id, "name": r.name, "equipment": sorted(r.equipment), "cells": room_cells(r)}
                      for r in self.rooms.values() if r.location_id == location_id],
            "bookings": [self.describe_booking(b) for b in sorted(here, key=lambda b: b.start)],
        }

    def describe_booking(self, b: Booking) -> dict:
        t = self.treatments[b.treatment_id]
        return {
            "id": b.id, "client": b.client, "date": b.date.isoformat(),
            "treatment_id": t.id, "treatment": t.name,
            "location_id": b.location_id, "location": self.locations[b.location_id].name,
            "room_id": b.room_id, "room": self.rooms[b.room_id].name,
            "doctor_id": b.doctor_id, "doctor": self.doctors[b.doctor_id].name,
            "staff_ids": b.staff_ids, "staff": [self.staff[s].name for s in b.staff_ids],
            "start": format_slot(b.start), "end": format_slot(self._end(b)),
        }

    # ---- consistency report ---------------------------------------------
    def issues(self) -> list[str]:
        """Existing bookings that no longer fit the roster, opening hours or rooms.

        Editing shifts/hours never silently deletes bookings; instead they show up here.
        """
        out = []
        for b in sorted(self.bookings.values(), key=lambda b: (b.date, b.start)):
            t = self.treatments[b.treatment_id]
            label = f"Booking {b.id} ({t.name}, {b.date} {format_slot(b.start)}, " \
                    f"{self.locations[b.location_id].name})"
            av = self.check_availability(b.treatment_id, b.location_id, b.date, b.start, ignore_booking=b.id)
            problems = list(av["reasons"])
            if b.doctor_id not in {d["id"] for d in av["doctors"]}:
                problems.append(f"doctor {self.doctors[b.doctor_id].name} unavailable/unqualified")
            if b.room_id not in {r["id"] for r in av["rooms"]}:
                problems.append(f"room {self.rooms[b.room_id].name} unavailable/unsuitable")
            free_staff = {s["id"] for s in av["staff"]}
            for sid in b.staff_ids:
                if sid not in free_staff:
                    problems.append(f"staff {self.staff[sid].name} unavailable")
            if len(b.staff_ids) != t.staff_required:
                problems.append(f"needs {t.staff_required} staff, has {len(b.staff_ids)}")
            if problems:
                out.append(f"{label}: " + "; ".join(dict.fromkeys(problems)))
        return out

    # ---- (de)serialisation ----------------------------------------------
    def to_dict(self) -> dict:
        def hours_json(h):
            return None if h is None else [format_slot(h[0]), format_slot(h[1])]
        return {
            "version": FORMAT_VERSION,
            "locations": [{
                "id": l.id, "name": l.name,
                "opening_hours": {WEEKDAYS[wd]: hours_json(h) for wd, h in sorted(l.opening_hours.items())},
                "date_overrides": {d.isoformat(): hours_json(h) for d, h in sorted(l.date_overrides.items())},
                "rooms": [{"id": r.id, "name": r.name, "equipment": sorted(r.equipment)}
                          for r in self.rooms.values() if r.location_id == l.id],
            } for l in self.locations.values()],
            "treatments": [{
                "id": t.id, "name": t.name, "duration_minutes": t.duration_slots * SLOT_MINUTES,
                "required_equipment": sorted(t.required_equipment), "staff_required": t.staff_required,
            } for t in self.treatments.values()],
            "doctors": [{"id": d.id, "name": d.name, "treatments": sorted(d.treatments)}
                        for d in self.doctors.values()],
            "staff": [{"id": s.id, "name": s.name} for s in self.staff.values()],
            "shifts": [{"id": s.id, "role": s.role, "person_id": s.person_id,
                        "location_id": s.location_id, "date": s.date.isoformat(),
                        "start": format_slot(s.start), "end": format_slot(s.end)}
                       for s in sorted(self.shifts.values(), key=lambda s: (s.date, s.location_id, s.start))],
            "bookings": [{"id": b.id, "treatment_id": b.treatment_id, "location_id": b.location_id,
                          "room_id": b.room_id, "doctor_id": b.doctor_id, "staff_ids": b.staff_ids,
                          "date": b.date.isoformat(), "start": format_slot(b.start), "client": b.client}
                         for b in sorted(self.bookings.values(), key=lambda b: (b.date, b.start))],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Clinic":
        """Build and validate a clinic. Raises ClinicError on structural problems."""
        if not isinstance(data, dict):
            raise ClinicError("Clinic data must be a JSON object")
        c = cls()

        def unique(table, key, kind):
            if key in table:
                raise ClinicError(f"Duplicate {kind} id {key!r}")

        def need(obj, key, kind):
            if not isinstance(obj, dict) or key not in obj:
                raise ClinicError(f"{kind} is missing {key!r}")
            return obj[key]

        def hours_from(raw, what):
            if raw is None:
                return None
            if not (isinstance(raw, (list, tuple)) and len(raw) == 2):
                raise ClinicError(f"{what}: expected [open, close] or null")
            span = (parse_time(raw[0]), parse_time(raw[1]))
            _check_span(*span, what)
            return span

        for l in data.get("locations", []):
            lid = need(l, "id", "location")
            unique(c.locations, lid, "location")
            loc = Location(lid, need(l, "name", f"location {lid}"))
            for wd, raw in (l.get("opening_hours") or {}).items():
                if wd not in WEEKDAYS:
                    raise ClinicError(f"{loc.name}: unknown weekday {wd!r}; use {WEEKDAYS}")
                span = hours_from(raw, f"{loc.name} {wd}")
                if span:
                    loc.opening_hours[WEEKDAYS.index(wd)] = span
            for d, raw in (l.get("date_overrides") or {}).items():
                loc.date_overrides[parse_date(d)] = hours_from(raw, f"{loc.name} {d}")
            c.locations[lid] = loc
            for r in l.get("rooms", []):
                rid = need(r, "id", "room")
                unique(c.rooms, rid, "room")
                c.rooms[rid] = Room(rid, need(r, "name", f"room {rid}"), lid, set(r.get("equipment", [])))

        for t in data.get("treatments", []):
            tid = need(t, "id", "treatment")
            unique(c.treatments, tid, "treatment")
            minutes = need(t, "duration_minutes", f"treatment {tid}")
            if not isinstance(minutes, int) or minutes <= 0 or minutes % SLOT_MINUTES:
                raise ClinicError(f"Treatment {tid}: duration_minutes must be a positive multiple of {SLOT_MINUTES}")
            staff_req = t.get("staff_required", 1)
            if not isinstance(staff_req, int) or staff_req < 0:
                raise ClinicError(f"Treatment {tid}: staff_required must be an integer >= 0")
            c.treatments[tid] = Treatment(tid, need(t, "name", f"treatment {tid}"),
                                          minutes // SLOT_MINUTES, set(t.get("required_equipment", [])), staff_req)

        for d in data.get("doctors", []):
            did = need(d, "id", "doctor")
            unique(c.doctors, did, "doctor")
            skills = set(d.get("treatments", []))
            for tid in skills:
                if tid not in c.treatments:
                    raise ClinicError(f"Doctor {did}: unknown treatment {tid!r}")
            c.doctors[did] = Doctor(did, need(d, "name", f"doctor {did}"), skills)

        for s in data.get("staff", []):
            sid = need(s, "id", "staff")
            unique(c.staff, sid, "staff")
            c.staff[sid] = Staff(sid, need(s, "name", f"staff {sid}"))

        for s in data.get("shifts", []):
            sid = s.get("id") or _new_id("sh")
            unique(c.shifts, sid, "shift")
            c.add_shift(need(s, "role", "shift"), need(s, "person_id", "shift"),
                        need(s, "location_id", "shift"), need(s, "date", "shift"),
                        parse_time(need(s, "start", "shift")), parse_time(need(s, "end", "shift")), sid)

        for b in data.get("bookings", []):
            bid = b.get("id") or _new_id("bk")
            unique(c.bookings, bid, "booking")
            booking = Booking(
                bid, need(b, "treatment_id", "booking"), need(b, "location_id", "booking"),
                need(b, "room_id", "booking"), need(b, "doctor_id", "booking"),
                list(b.get("staff_ids", [])), parse_date(need(b, "date", "booking")),
                parse_time(need(b, "start", "booking")), b.get("client", ""))
            c._get(c.treatments, booking.treatment_id, "treatment")
            c._get(c.locations, booking.location_id, "location")
            room = c._get(c.rooms, booking.room_id, "room")
            if room.location_id != booking.location_id:
                raise ClinicError(f"Booking {bid}: room {room.id} is not at location {booking.location_id}")
            c._get(c.doctors, booking.doctor_id, "doctor")
            for sid in booking.staff_ids:
                c._get(c.staff, sid, "staff")
            _check_span(booking.start, booking.start + c.treatments[booking.treatment_id].duration_slots,
                        f"Booking {bid}")
            c.bookings[bid] = booking
        return c

    def save(self, path: str) -> None:
        """Atomically write the whole clinic (setup + shifts + bookings) as JSON."""
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path: str) -> "Clinic":
        with open(path) as f:
            return cls.from_dict(json.load(f))
