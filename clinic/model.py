"""Core domain model for the aesthetics-clinic planner.

Time is modelled in 15-minute slots: slot 0 = 00:00, slot 36 = 09:00,
slot 96 = 24:00 (end of day).  A span is half-open ``[start, end)``.

Concepts
--------
Location   - has weekly opening hours (plus optional per-date overrides) and rooms.
Room       - belongs to one location, owns a set of equipment tags.
Treatment  - duration (multiple of 15 min), required equipment, staff headcount.
Doctor     - may only perform a subset of treatments (their "skills").
Staff      - interchangeable; any staff member can assist any treatment.
Presence   - per DAY, a doctor/staff member is at ONE location or at none (the
             tick box in the UI).  Once present they are free all day unless
             booked for a treatment or explicitly blocked.
Block      - times a doctor, staff member or room can't be used (leave,
             maintenance...).  Optional; default is nothing blocked.
Booking    - a treatment at a location/date/start using 1 doctor, N staff, 1 room.

Data is stored as three groups (three JSON files), by how often they change:
  resources - locations, rooms (+equipment), doctors, staff        (rarely)
  planning  - opening hours, treatments, doctor skills, presence   (regularly)
  schedule  - bookings and blocked times                           (constantly)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date as Date, timedelta

SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES  # 96
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FORMAT_VERSION = 1
GROUPS = ("resources", "planning", "schedule")
GROUP_FILES = {g: f"{g}.json" for g in GROUPS}
BLOCK_KINDS = ("doctor", "staff", "room")


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
        self.bookings: dict[str, Booking] = {}
        # (role, person_id, date) -> location_id.  A dict key per person per day,
        # so "at most one location per day" holds by construction.
        self.presence: dict[tuple[str, str, Date], str] = {}
        # (kind, id, date) -> slots blocked; kind is doctor | staff | room
        self.blocks: dict[tuple[str, str, Date], set[int]] = {}

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

    def _resource_table(self, kind: str) -> dict:
        if kind == "room":
            return self.rooms
        if kind in ("doctor", "staff"):
            return self._people(kind)
        raise ClinicError(f"Unknown kind {kind!r}; expected one of {list(BLOCK_KINDS)}")

    # ---- presence: who is at which location on a day -----------------------
    def set_presence(self, role: str, person_id: str, day, location_id: str | None) -> None:
        """Put a doctor/staff member at ``location_id`` for the whole day (``None`` = not in
        any location).  Replaces wherever they were that day: one location per day.
        Existing bookings are never touched; see ``issues()``."""
        day = parse_date(day)
        self._get(self._people(role), person_id, role)
        if location_id is not None:
            self._get(self.locations, location_id, "location")
            self.presence[(role, person_id, day)] = location_id
        else:
            self.presence.pop((role, person_id, day), None)

    def presence_at(self, role: str, person_id: str, day) -> str | None:
        return self.presence.get((role, person_id, parse_date(day)))

    # ---- blocked times (doctor / staff / room) -------------------------------
    def set_blocked(self, kind: str, resource_id: str, day, start: int, end: int, blocked: bool) -> None:
        """Block or unblock ``[start, end)`` on a date for a doctor, staff member or room."""
        day = parse_date(day)
        res = self._get(self._resource_table(kind), resource_id, kind)
        _check_span(start, end, f"Blocking {res.name}")
        slots = self.blocks.setdefault((kind, resource_id, day), set())
        if blocked:
            slots.update(range(start, end))
        else:
            slots.difference_update(range(start, end))
        if not slots:
            del self.blocks[(kind, resource_id, day)]

    def _is_blocked(self, kind: str, rid: str, day: Date, start: int, end: int) -> bool:
        return bool(self.blocks.get((kind, rid, day), set()) & set(range(start, end)))

    # ---- availability ----------------------------------------------------
    def _person_status(self, role: str, pid: str, loc_id: str, day: Date, start: int, end: int,
                       ignore: str | None = None) -> str:
        """'free' | 'not_here' (not ticked in at this location that day) | 'busy' (blocked/booked)."""
        if self.presence.get((role, pid, day)) != loc_id:
            return "not_here"
        if self._is_blocked(role, pid, day, start, end):
            return "busy"
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
                    [p for p in pool if statuses[p.id] != "not_here"])

        # doctors
        skilled = [d for d in self.doctors.values() if t.id in d.treatments]
        free_docs, present_docs = free_people("doctor", skilled)
        if not skilled:
            reasons.append(f"No doctor performs {t.name}")
        elif not present_docs:
            reasons.append(f"No doctor qualified for {t.name} is in {loc.name} on {day}")
        elif not free_docs:
            reasons.append(f"Every qualified doctor in {loc.name} is booked or blocked for {span}")

        # staff (interchangeable)
        free_staff, present_staff = free_people("staff", list(self.staff.values()))
        if len(free_staff) < t.staff_required:
            reasons.append(f"Needs {t.staff_required} staff but only {len(free_staff)} free at "
                           f"{loc.name} for {span} ({len(present_staff)} in {loc.name} that day)")

        # rooms
        fitting = [r for r in self.rooms.values()
                   if r.location_id == location_id and t.required_equipment <= r.equipment]
        free_rooms = [r for r in fitting
                      if not self._room_busy(r.id, day, start, end, ignore_booking)
                      and not self._is_blocked("room", r.id, day, start, end)]
        if not fitting:
            need = ", ".join(sorted(t.required_equipment)) or "any equipment"
            reasons.append(f"{loc.name} has no treatment room with the required equipment ({need})")
        elif not free_rooms:
            reasons.append(f"Every suitable room at {loc.name} is booked or unavailable for {span}")

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

        Cell values: ``free`` | ``off`` (not in any location that day) | ``closed``
        (location not open) | ``blocked`` (manually made unavailable) |
        ``away:<location_id>`` (in / booked at another location) | ``booked:<booking_id>``.
        """
        day = parse_date(day)
        loc = self._get(self.locations, location_id, "location")
        hours = loc.hours_on(day)
        todays = self._bookings_on(day)
        open_cells = ["free" if hours and hours[0] <= i < hours[1] else "closed"
                      for i in range(SLOTS_PER_DAY)]

        def with_blocks(kind, rid, cells):
            for i in self.blocks.get((kind, rid, day), ()):
                if cells[i] == "free":
                    cells[i] = "blocked"
            return cells

        def person_row(role, p):
            at = self.presence.get((role, p.id, day))
            if at is None:
                cells = ["off"] * SLOTS_PER_DAY
            elif at != location_id:
                cells = [f"away:{at}"] * SLOTS_PER_DAY
            else:
                cells = with_blocks(role, p.id, list(open_cells))
            for b in todays:
                uses = b.doctor_id == p.id if role == "doctor" else p.id in b.staff_ids
                if uses:
                    tag = f"booked:{b.id}" if b.location_id == location_id else f"away:{b.location_id}"
                    for i in range(b.start, self._end(b)):
                        cells[i] = tag
            return {"id": p.id, "name": p.name, "cells": cells,
                    "here": at == location_id, "at": at}

        def room_row(room):
            cells = with_blocks("room", room.id, list(open_cells))
            for b in todays:
                if b.room_id == room.id:
                    for i in range(b.start, self._end(b)):
                        cells[i] = f"booked:{b.id}"
            return {"id": room.id, "name": room.name, "equipment": sorted(room.equipment), "cells": cells}

        here = [b for b in todays if b.location_id == location_id]
        lo, hi = hours if hours else (36, 76)
        for b in here:
            lo, hi = min(lo, b.start), max(hi, self._end(b))

        return {
            "location_id": loc.id, "location": loc.name, "date": day.isoformat(),
            "hours": [format_slot(hours[0]), format_slot(hours[1])] if hours else None,
            "view": [lo, hi],
            "doctors": [person_row("doctor", d) for d in self.doctors.values()],
            "staff": [person_row("staff", s) for s in self.staff.values()],
            "rooms": [room_row(r) for r in self.rooms.values() if r.location_id == location_id],
            "bookings": [self.describe_booking(b) for b in sorted(here, key=lambda b: b.start)],
        }

    def roster(self, start, days: int = 7) -> dict:
        """Who is where for ``days`` consecutive days (for the weekly roster grid)."""
        start = parse_date(start)
        dates = [start + timedelta(days=i) for i in range(days)]
        def row(role, p):
            return {"role": role, "id": p.id, "name": p.name,
                    "at": [self.presence.get((role, p.id, d)) for d in dates]}
        return {"dates": [d.isoformat() for d in dates],
                "people": [row("doctor", d) for d in self.doctors.values()]
                          + [row("staff", s) for s in self.staff.values()]}

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

        Editing presence, blocks or hours never silently deletes bookings; instead they show up here.
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
        """The whole clinic as one JSON-able dict (see ``to_groups`` for the 3-file split)."""
        def hours_json(h):
            return None if h is None else [format_slot(h[0]), format_slot(h[1])]
        presence: dict[str, dict] = {}
        for (role, pid, day), loc in sorted(self.presence.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
            presence.setdefault(day.isoformat(), {"doctors": {}, "staff": {}})[
                "doctors" if role == "doctor" else "staff"][pid] = loc
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
            "presence": presence,
            "blocks": self._blocks_json(),
            "bookings": [{"id": b.id, "treatment_id": b.treatment_id, "location_id": b.location_id,
                          "room_id": b.room_id, "doctor_id": b.doctor_id, "staff_ids": b.staff_ids,
                          "date": b.date.isoformat(), "start": format_slot(b.start), "client": b.client}
                         for b in sorted(self.bookings.values(), key=lambda b: (b.date, b.start))],
        }

    def _blocks_json(self) -> list[dict]:
        out = []
        for (kind, rid, day), slots in sorted(self.blocks.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
            run_start = prev = None
            for i in sorted(slots) + [None]:
                if run_start is not None and i != prev + 1:
                    out.append({"kind": kind, "id": rid, "date": day.isoformat(),
                                "start": format_slot(run_start), "end": format_slot(prev + 1)})
                    run_start = None
                if i is not None and run_start is None:
                    run_start = i
                prev = i
        return out

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

        for day, entry in (data.get("presence") or {}).items():
            extra = set(entry) - {"doctors", "staff"}
            if extra:
                raise ClinicError(f"presence {day}: unexpected keys {sorted(extra)}; use 'doctors' and 'staff'")
            for key, role in (("doctors", "doctor"), ("staff", "staff")):
                for pid, loc in (entry.get(key) or {}).items():
                    c.set_presence(role, pid, day, loc)

        for blk in data.get("blocks", []):
            start, end = parse_time(need(blk, "start", "block")), parse_time(need(blk, "end", "block"))
            c.set_blocked(need(blk, "kind", "block"), need(blk, "id", "block"),
                          need(blk, "date", "block"), start, end, True)

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

    # ---- the three data groups -------------------------------------------
    def to_groups(self) -> dict[str, dict]:
        """Split into ``resources`` / ``planning`` / ``schedule`` (see module docstring)."""
        d = self.to_dict()
        return {
            "resources": {
                "version": FORMAT_VERSION,
                "locations": [{"id": l["id"], "name": l["name"], "rooms": l["rooms"]} for l in d["locations"]],
                "doctors": [{"id": x["id"], "name": x["name"]} for x in d["doctors"]],
                "staff": d["staff"],
            },
            "planning": {
                "version": FORMAT_VERSION,
                "opening_hours": {l["id"]: l["opening_hours"] for l in d["locations"]},
                "date_overrides": {l["id"]: l["date_overrides"] for l in d["locations"]},
                "treatments": d["treatments"],
                "doctor_skills": {x["id"]: x["treatments"] for x in d["doctors"]},
                "presence": d["presence"],
            },
            "schedule": {
                "version": FORMAT_VERSION,
                "blocks": d["blocks"],
                "bookings": d["bookings"],
            },
        }

    @classmethod
    def from_groups(cls, groups: dict) -> "Clinic":
        """Inverse of ``to_groups``; any missing group counts as empty. Cross-checks references."""
        for g in groups:
            if g not in GROUPS:
                raise ClinicError(f"Unknown data group {g!r}; expected {list(GROUPS)}")
        res, plan, sched = (groups.get(g) or {} for g in GROUPS)
        for name, part in zip(GROUPS, (res, plan, sched)):
            if not isinstance(part, dict):
                raise ClinicError(f"Group {name!r} must be a JSON object")
        hours, overrides = plan.get("opening_hours") or {}, plan.get("date_overrides") or {}
        loc_ids = {l.get("id") for l in res.get("locations", [])}
        for what, table in (("opening_hours", hours), ("date_overrides", overrides)):
            unknown = set(table) - loc_ids
            if unknown:
                raise ClinicError(f"planning.{what} refers to unknown location(s) {sorted(unknown)}")
        skills = plan.get("doctor_skills") or {}
        unknown = set(skills) - {d.get("id") for d in res.get("doctors", [])}
        if unknown:
            raise ClinicError(f"planning.doctor_skills refers to unknown doctor(s) {sorted(unknown)}")
        return cls.from_dict({
            "locations": [dict(l, opening_hours=hours.get(l.get("id")) or {},
                               date_overrides=overrides.get(l.get("id")) or {})
                          for l in res.get("locations", [])],
            "treatments": plan.get("treatments", []),
            "doctors": [dict(d, treatments=skills.get(d.get("id")) or []) for d in res.get("doctors", [])],
            "staff": res.get("staff", []),
            "presence": plan.get("presence", {}),
            "blocks": sched.get("blocks", []),
            "bookings": sched.get("bookings", []),
        })

    def save_dir(self, directory: str) -> list[str]:
        """Write ``resources.json``, ``planning.json`` and ``schedule.json`` into ``directory``.

        A file is only rewritten if its content changed (so git diffs stay small and the
        rarely-changing groups stay untouched).  Returns the names of the files written.
        """
        os.makedirs(directory, exist_ok=True)
        written = []
        for group, data in self.to_groups().items():
            path = os.path.join(directory, GROUP_FILES[group])
            text = _dumps(data)
            if os.path.exists(path):
                with open(path) as f:
                    if f.read() == text:
                        continue
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            written.append(GROUP_FILES[group])
        return written

    @classmethod
    def load_dir(cls, directory: str) -> "Clinic":
        groups = {}
        for group, name in GROUP_FILES.items():
            path = os.path.join(directory, name)
            if os.path.exists(path):
                with open(path) as f:
                    try:
                        groups[group] = json.load(f)
                    except json.JSONDecodeError as e:
                        raise ClinicError(f"{name}: invalid JSON ({e})") from None
        return cls.from_groups(groups)

    @staticmethod
    def dir_has_data(directory: str) -> bool:
        return any(os.path.exists(os.path.join(directory, n)) for n in GROUP_FILES.values())


def _dumps(data) -> str:
    """Pretty JSON, but short scalar lists (e.g. ["10:00", "20:00"]) stay on one line."""
    text = json.dumps(data, indent=2)
    text = re.sub(r'\[\n\s*([^\[\]{}]*?)\n\s*\]',
                  lambda m: "[" + re.sub(r',\n\s*', ', ', m.group(1)) + "]", text)
    return text + "\n"
