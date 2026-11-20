"""Core domain model for the aesthetics-clinic planner.

Time is modelled in 15-minute slots: slot 0 = 00:00, slot 36 = 09:00,
slot 96 = 24:00 (end of day).  A span is half-open ``[start, end)``.

Concepts
--------
Location   - has weekly opening hours (plus optional per-date overrides) and rooms.
Room       - belongs to one location, owns a set of equipment tags.
Treatment  - three sequential steps, each a multiple of 15 min (0 = step skipped):
               1. numbing           - needs 1 staff
               2. doctor treatment  - needs a (qualified) doctor + 1 staff
               3. staff treatment   - needs 1 staff
             A treatment with no doctor step is carried out by staff alone (just one
             staff step).  One room (with the required equipment) is held for the whole
             treatment.  The doctor is only busy during step 2; each staff member is only
             busy during the step they do (steps may be done by different staff).
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
  planning  - opening hours, treatments, doctor skills             (regularly)
  schedule  - presence (who is where each day), blocks, bookings   (constantly)
"""
from __future__ import annotations

import difflib
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
STEP_NAMES = ("numbing", "doctor", "staff")
STEP_LABELS = {"numbing": "Numbing", "doctor": "Doctor treatment", "staff": "Staff treatment"}


@dataclass(frozen=True)
class Step:
    name: str   # one of STEP_NAMES
    start: int
    end: int

    @property
    def label(self) -> str:
        return STEP_LABELS[self.name]


@dataclass
class Treatment:
    id: str
    name: str
    numbing_slots: int = 0   # step 1: 1 staff
    doctor_slots: int = 0    # step 2: doctor + 1 staff (0 = staff-only treatment)
    staff_slots: int = 0     # step 3: 1 staff
    required_equipment: set[str] = field(default_factory=set)

    @property
    def duration_slots(self) -> int:
        return self.numbing_slots + self.doctor_slots + self.staff_slots

    @property
    def needs_doctor(self) -> bool:
        return self.doctor_slots > 0

    def steps(self, start: int) -> list[Step]:
        """The non-empty steps laid out back to back from ``start``."""
        out, at = [], start
        for name, n in zip(STEP_NAMES, (self.numbing_slots, self.doctor_slots, self.staff_slots)):
            if n:
                out.append(Step(name, at, at + n))
                at += n
        return out


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
    doctor_id: str | None          # None for staff-only treatments
    staff: dict[str, str]          # step name -> staff id (one per non-empty step)
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
    def _usage(self, b: Booking) -> list[tuple[str, str, str, int, int]]:
        """Who is busy when for a booking: ``(role, person_id, step, start, end)``."""
        out = []
        for st in self.treatments[b.treatment_id].steps(b.start):
            if st.name == "doctor" and b.doctor_id:
                out.append(("doctor", b.doctor_id, st.name, st.start, st.end))
            if b.staff.get(st.name):
                out.append(("staff", b.staff[st.name], st.name, st.start, st.end))
        return out

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
            for r, p, _step, a, e in self._usage(b):
                if r == role and p == pid and _overlap(start, end, a, e):
                    return "busy"
        return "free"

    def _free_people(self, role: str, pool: list, loc_id: str, day: Date, start: int, end: int,
                     ignore: str | None) -> tuple[list, list]:
        """(free for the whole span, in this location that day) among ``pool``."""
        status = {p.id: self._person_status(role, p.id, loc_id, day, start, end, ignore) for p in pool}
        return ([p for p in pool if status[p.id] == "free"],
                [p for p in pool if status[p.id] != "not_here"])

    def _room_busy(self, room_id: str, day: Date, start: int, end: int, ignore: str | None) -> bool:
        return any(b.id != ignore and b.room_id == room_id
                   and _overlap(start, end, b.start, self._end(b))
                   for b in self._bookings_on(day))

    def check_availability(self, treatment_id: str, location_id: str, day, start: int,
                           ignore_booking: str | None = None) -> dict:
        """Can ``treatment_id`` start at ``start`` on ``day`` at ``location_id``?

        Every step needs its resources for its own time window: one free staff member for
        each step, a free qualified doctor for the doctor step, and one free room with the
        required equipment for the whole treatment.  Returns ``available``, who/what is free
        (per step for staff), and human-readable ``reasons`` when it isn't.
        """
        day = parse_date(day)
        t = self._get(self.treatments, treatment_id, "treatment")
        loc = self._get(self.locations, location_id, "location")
        if not 0 <= start < SLOTS_PER_DAY:
            raise ClinicError(f"Bad start slot {start}")
        end = start + t.duration_slots
        span = f"{format_slot(start)}-{format_slot(min(end, SLOTS_PER_DAY))}"
        reasons: list[str] = []
        steps_out: list[dict] = []
        docs: list = []
        rooms: list = []

        if end > SLOTS_PER_DAY:
            reasons.append(f"{t.name} would run past midnight")
        hours = loc.hours_on(day)
        if hours is None:
            reasons.append(f"{loc.name} is closed on {day}")
        elif start < hours[0] or end > hours[1]:
            reasons.append(f"{span} is outside {loc.name} opening hours "
                           f"({format_slot(hours[0])}-{format_slot(hours[1])})")

        if end <= SLOTS_PER_DAY:  # can't evaluate resources beyond the day
            everyone = list(self.staff.values())
            for st in t.steps(start):
                window = f"{format_slot(st.start)}-{format_slot(st.end)}"
                free_staff, present_staff = self._free_people("staff", everyone, location_id, day,
                                                              st.start, st.end, ignore_booking)
                if not free_staff:
                    reasons.append(f"No staff free for {st.label.lower()} {window} at {loc.name} "
                                   f"({len(present_staff)} in {loc.name} that day)")
                steps_out.append({"name": st.name, "label": st.label, "start": format_slot(st.start),
                                  "end": format_slot(st.end), "needs_doctor": st.name == "doctor",
                                  "staff": [{"id": p.id, "name": p.name} for p in free_staff]})
                if st.name == "doctor":
                    skilled = [d for d in self.doctors.values() if t.id in d.treatments]
                    docs, present_docs = self._free_people("doctor", skilled, location_id, day,
                                                           st.start, st.end, ignore_booking)
                    if not skilled:
                        reasons.append(f"No doctor performs {t.name}")
                    elif not present_docs:
                        reasons.append(f"No doctor qualified for {t.name} is in {loc.name} on {day}")
                    elif not docs:
                        reasons.append(f"Every qualified doctor in {loc.name} is booked or blocked "
                                       f"for the doctor treatment {window}")

            fitting = [r for r in self.rooms.values()
                       if r.location_id == location_id and t.required_equipment <= r.equipment]
            rooms = [r for r in fitting
                     if not self._room_busy(r.id, day, start, end, ignore_booking)
                     and not self._is_blocked("room", r.id, day, start, end)]
            if not fitting:
                need = ", ".join(sorted(t.required_equipment)) or "any equipment"
                reasons.append(f"{loc.name} has no treatment room with the required equipment ({need})")
            elif not rooms:
                reasons.append(f"Every suitable room at {loc.name} is booked or unavailable for {span}")

        return {
            "available": not reasons,
            "treatment_id": t.id, "location_id": loc.id, "date": day.isoformat(),
            "start": format_slot(start), "end": format_slot(min(end, SLOTS_PER_DAY)),
            "needs_doctor": t.needs_doctor,
            "steps": steps_out,
            "doctors": [{"id": d.id, "name": d.name} for d in docs],
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
             booking_id: str | None = None) -> Booking:
        """Book a treatment, choosing what isn't specified.

        Greedy and deterministic: the first free qualified doctor, the first free suitable
        room, and for each step the first free staff member (keeping the same person from
        one step to the next where possible).  Bookings are never reshuffled afterwards.
        """
        day = parse_date(day)
        av = self.check_availability(treatment_id, location_id, day, start)
        if not av["available"]:
            raise ClinicError("Not available: " + "; ".join(av["reasons"] or ["no free resources"]))
        free_docs = [d["id"] for d in av["doctors"]]
        free_rooms = [r["id"] for r in av["rooms"]]
        if av["needs_doctor"]:
            if doctor_id and doctor_id not in free_docs:
                raise ClinicError(f"Doctor {doctor_id!r} is not available for that treatment/time")
            doctor = doctor_id or free_docs[0]
        elif doctor_id:
            raise ClinicError("This treatment is carried out by staff alone; no doctor is needed")
        else:
            doctor = None
        if room_id and room_id not in free_rooms:
            raise ClinicError(f"Room {room_id!r} is not available for that treatment/time")
        staff: dict[str, str] = {}
        previous = None
        for step in av["steps"]:
            candidates = [s["id"] for s in step["staff"]]
            previous = previous if previous in candidates else candidates[0]
            staff[step["name"]] = previous
        b = Booking(booking_id or _new_id("bk"), treatment_id, location_id, room_id or free_rooms[0],
                    doctor, staff, day, start, client)
        self.bookings[b.id] = b
        return b

    def book_list(self, location_id: str, day, items: list[dict], dry_run: bool = False) -> dict:
        """Go down a list of treatments with proposed start times and book them in order.

        Each item is ``{treatment_id, start: "HH:MM", client?, location_id?, date?}`` and sees
        the resources already used by the items before it.  The first item that can't be
        carried out (not enough doctor, room or staff, or outside opening hours) is reported
        as ``failed`` with the reasons, and processing STOPS there: the items after it are
        ``not_attempted``.  Items before it stay booked (unless ``dry_run``, which books
        nothing and just reports what would happen).
        """
        results: list[dict] = []
        created: list[str] = []
        stopped_at = None
        for i, item in enumerate(items):
            known = self.treatments.get(item.get("treatment_id"))
            row = {"index": i, "treatment_id": item.get("treatment_id"), "treatment": known.name if known else None,
                   "start": item.get("start"), "client": item.get("client", "")}
            results.append(row)
            if stopped_at is not None:
                row["status"] = "not_attempted"
                continue
            try:
                for key in ("treatment_id", "start"):
                    if not item.get(key):
                        raise ClinicError(f"Item {i + 1} is missing {key!r}")
                row["treatment"] = self._get(self.treatments, item["treatment_id"], "treatment").name
                loc, d, start = item.get("location_id") or location_id, item.get("date") or day, parse_time(item["start"])
                av = self.check_availability(item["treatment_id"], loc, d, start)
                if not av["available"]:
                    raise ClinicError("; ".join(av["reasons"]))
                b = self.book(item["treatment_id"], loc, d, start, item.get("client", ""))
            except ClinicError as e:
                row.update(status="failed", reasons=str(e).split("; ") if str(e) else ["not possible"])
                stopped_at = i
                continue
            created.append(b.id)
            row.update(status="booked", booking=self.describe_booking(b))
        if dry_run:
            for bid in created:
                del self.bookings[bid]
        return {"results": results, "total": len(items), "booked": len(created), "stopped_at": stopped_at,
                "dry_run": dry_run}

    def cancel_booking(self, booking_id: str) -> None:
        self._get(self.bookings, booking_id, "booking")
        del self.bookings[booking_id]

    def clear_day(self, location_id: str, day, dry_run: bool = False) -> dict:
        """Remove every booking and every blocked time for one location on one date.

        * bookings: those at ``location_id`` on ``day``
        * blocks: of the rooms at ``location_id`` and of the doctors/staff who are ticked in
          there that day (a person's blocks follow them, so they belong to where they are)
        Presence (the tick boxes) is left alone.  With ``dry_run`` nothing is changed; the
        result says what would be removed.
        """
        day = parse_date(day)
        self._get(self.locations, location_id, "location")
        bookings = [b for b in self._bookings_on(day) if b.location_id == location_id]
        keys = [k for k in self.blocks if k[2] == day and (
                    (k[0] == "room" and self.rooms[k[1]].location_id == location_id)
                    or (k[0] != "room" and self.presence.get((k[0], k[1], day)) == location_id))]
        spans = lambda slots: sum(1 for i in slots if i - 1 not in slots)
        result = {
            "bookings": len(bookings),
            "blocks": sum(spans(self.blocks[k]) for k in keys),
            "booking_details": [self.describe_booking(b) for b in sorted(bookings, key=lambda b: b.start)],
            "blocked": sorted({self._resource_table(k[0])[k[1]].name for k in keys}),
        }
        if not dry_run:
            for b in bookings:
                del self.bookings[b.id]
            for k in keys:
                del self.blocks[k]
        return result

    # ---- views -----------------------------------------------------------
    def timetable(self, location_id: str, day) -> dict:
        """Per-slot state of every doctor, staff member and room at one location.

        Cell values: ``free`` | ``off`` (not in any location that day) | ``closed``
        (location not open) | ``blocked`` (manually made unavailable) |
        ``away:<location_id>`` (in / booked at another location) |
        ``booked:<booking_id>:<step>`` (step = numbing|doctor|staff; ``all`` for a room).
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
                for r, pid, step, a, e in self._usage(b):
                    if r == role and pid == p.id:
                        tag = f"booked:{b.id}:{step}" if b.location_id == location_id else f"away:{b.location_id}"
                        for i in range(a, e):
                            cells[i] = tag
            return {"id": p.id, "name": p.name, "cells": cells,
                    "here": at == location_id, "at": at}

        def room_row(room):
            cells = with_blocks("room", room.id, list(open_cells))
            for b in todays:
                if b.room_id == room.id:  # a room is held for the whole treatment
                    for i in range(b.start, self._end(b)):
                        cells[i] = f"booked:{b.id}:all"
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
        doctor = self.doctors[b.doctor_id].name if b.doctor_id else None
        steps = []
        for st in t.steps(b.start):
            sid = b.staff.get(st.name)
            steps.append({"name": st.name, "label": st.label,
                          "start": format_slot(st.start), "end": format_slot(st.end),
                          "staff_id": sid, "staff": self.staff[sid].name if sid else None,
                          "doctor": doctor if st.name == "doctor" else None})
        staff_ids = list(dict.fromkeys(x["staff_id"] for x in steps if x["staff_id"]))
        return {
            "id": b.id, "client": b.client, "date": b.date.isoformat(),
            "treatment_id": t.id, "treatment": t.name,
            "location_id": b.location_id, "location": self.locations[b.location_id].name,
            "room_id": b.room_id, "room": self.rooms[b.room_id].name,
            "doctor_id": b.doctor_id, "doctor": doctor,
            "staff_ids": staff_ids, "staff": [self.staff[x].name for x in staff_ids],
            "steps": steps,
            "start": format_slot(b.start), "end": format_slot(self._end(b)),
        }

    # ---- consistency report ---------------------------------------------
    def _booking_problems(self, b: Booking) -> list[str]:
        t, loc, day, end = self.treatments[b.treatment_id], self.locations[b.location_id], b.date, self._end(b)
        probs = []
        hours = loc.hours_on(day)
        if hours is None:
            probs.append(f"{loc.name} is closed on {day}")
        elif b.start < hours[0] or end > hours[1]:
            probs.append(f"outside {loc.name} opening hours ({format_slot(hours[0])}-{format_slot(hours[1])})")
        for st in t.steps(b.start):
            window = f"{format_slot(st.start)}-{format_slot(st.end)}"
            sid = b.staff.get(st.name)
            if not sid:
                probs.append(f"no staff assigned for {st.label.lower()}")
            elif self._person_status("staff", sid, b.location_id, day, st.start, st.end, b.id) != "free":
                probs.append(f"staff {self.staff[sid].name} unavailable for {st.label.lower()} {window}")
            if st.name == "doctor":
                doc = self.doctors.get(b.doctor_id) if b.doctor_id else None
                if doc is None:
                    probs.append("no doctor assigned")
                else:
                    if t.id not in doc.treatments:
                        probs.append(f"doctor {doc.name} is not qualified for {t.name}")
                    if self._person_status("doctor", doc.id, b.location_id, day, st.start, st.end, b.id) != "free":
                        probs.append(f"doctor {doc.name} unavailable for the doctor treatment {window}")
        room = self.rooms[b.room_id]
        if not t.required_equipment <= room.equipment:
            probs.append(f"room {room.name} lacks the required equipment")
        if self._room_busy(room.id, day, b.start, end, b.id) or self._is_blocked("room", room.id, day, b.start, end):
            probs.append(f"room {room.name} unavailable")
        return probs

    def issues(self) -> list[str]:
        """Existing bookings that no longer fit the roster, blocks, opening hours or rooms.

        Editing presence, blocks or hours never silently deletes bookings; instead they show up here.
        """
        out = []
        for b in sorted(self.bookings.values(), key=lambda b: (b.date, b.start)):
            problems = self._booking_problems(b)
            if problems:
                label = (f"Booking {b.id} ({self.treatments[b.treatment_id].name}, {b.date} "
                         f"{format_slot(b.start)}, {self.locations[b.location_id].name})")
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
                "id": t.id, "name": t.name,
                "numbing_minutes": t.numbing_slots * SLOT_MINUTES,
                "doctor_minutes": t.doctor_slots * SLOT_MINUTES,
                "staff_minutes": t.staff_slots * SLOT_MINUTES,
                "required_equipment": sorted(t.required_equipment),
            } for t in self.treatments.values()],
            "doctors": [{"id": d.id, "name": d.name, "treatments": sorted(d.treatments)}
                        for d in self.doctors.values()],
            "staff": [{"id": s.id, "name": s.name} for s in self.staff.values()],
            "presence": presence,
            "blocks": self._blocks_json(),
            "bookings": [{"id": b.id, "treatment_id": b.treatment_id, "location_id": b.location_id,
                          "room_id": b.room_id, "doctor_id": b.doctor_id, "staff": b.staff,
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
            if "duration_minutes" in t or "staff_required" in t:
                raise ClinicError(f"Treatment {tid}: 'duration_minutes' / 'staff_required' are no longer used; "
                                  "give numbing_minutes, doctor_minutes and staff_minutes instead")
            slots = []
            for key in ("numbing_minutes", "doctor_minutes", "staff_minutes"):
                minutes = t.get(key, 0)
                if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0 or minutes % SLOT_MINUTES:
                    raise ClinicError(f"Treatment {tid}: {key} must be 0 or a multiple of {SLOT_MINUTES}")
                slots.append(minutes // SLOT_MINUTES)
            if not any(slots):
                raise ClinicError(f"Treatment {tid}: needs at least one step with minutes > 0")
            if slots[1] == 0 and slots[0] > 0:
                raise ClinicError(f"Treatment {tid}: numbing_minutes needs a doctor treatment; a staff-only "
                                  "treatment (doctor_minutes 0) only uses staff_minutes")
            c.treatments[tid] = Treatment(tid, need(t, "name", f"treatment {tid}"), *slots,
                                          set(t.get("required_equipment", [])))

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
            if "staff_ids" in b:
                raise ClinicError(f"Booking {bid}: 'staff_ids' is no longer used; give 'staff' as "
                                  "{step: staff_id} for the numbing / doctor / staff steps")
            booking = Booking(
                bid, need(b, "treatment_id", "booking"), need(b, "location_id", "booking"),
                need(b, "room_id", "booking"), b.get("doctor_id") or None,
                dict(b.get("staff") or {}), parse_date(need(b, "date", "booking")),
                parse_time(need(b, "start", "booking")), b.get("client", ""))
            t = c._get(c.treatments, booking.treatment_id, "treatment")
            c._get(c.locations, booking.location_id, "location")
            room = c._get(c.rooms, booking.room_id, "room")
            if room.location_id != booking.location_id:
                raise ClinicError(f"Booking {bid}: room {room.id} is not at location {booking.location_id}")
            if booking.doctor_id:
                c._get(c.doctors, booking.doctor_id, "doctor")
            real_steps = {st.name for st in t.steps(booking.start)}
            for step, sid in booking.staff.items():
                if step not in real_steps:
                    raise ClinicError(f"Booking {bid}: staff given for step {step!r}, which treatment "
                                      f"{t.id} does not have (it has {sorted(real_steps)})")
                c._get(c.staff, sid, "staff")
            _check_span(booking.start, booking.start + t.duration_slots, f"Booking {bid}")
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
            },
            "schedule": {
                "version": FORMAT_VERSION,
                "presence": d["presence"],
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
        # presence used to live in planning.json; still accept it there (it is moved to
        # schedule.json the next time the clinic is saved) but never silently drop or mix it
        presence, old_presence = sched.get("presence") or {}, plan.get("presence") or {}
        if presence and old_presence:
            raise ClinicError("presence is in both planning.json and schedule.json; "
                              "it now belongs in schedule.json only")
        return cls.from_dict({
            "locations": [dict(l, opening_hours=hours.get(l.get("id")) or {},
                               date_overrides=overrides.get(l.get("id")) or {})
                          for l in res.get("locations", [])],
            "treatments": plan.get("treatments", []),
            "doctors": [dict(d, treatments=skills.get(d.get("id")) or []) for d in res.get("doctors", [])],
            "staff": res.get("staff", []),
            "presence": presence or old_presence,
            "blocks": sched.get("blocks", []),
            "bookings": sched.get("bookings", []),
        })

    def render_files(self) -> dict[str, str]:
        """The exact text of each data file for the current in-memory state."""
        return {GROUP_FILES[g]: _dumps(d) for g, d in self.to_groups().items()}

    @staticmethod
    def read_files(directory: str) -> dict[str, str | None]:
        """Current text of each data file on disk (``None`` if it doesn't exist)."""
        out: dict[str, str | None] = {}
        for name in GROUP_FILES.values():
            path = os.path.join(directory, name)
            if os.path.exists(path):
                with open(path) as f:
                    out[name] = f.read()
            else:
                out[name] = None
        return out

    def pending_changes(self, baseline: dict[str, str | None]) -> list[dict]:
        """Files whose in-memory content differs from ``baseline`` (what was last loaded
        or saved), with the number of changed lines: ``[{file, added, removed}]``."""
        out = []
        for name, text in self.render_files().items():
            old = baseline.get(name)
            if old == text:
                continue
            diff = list(difflib.unified_diff((old or "").splitlines(), text.splitlines(), lineterm="", n=0))
            out.append({"file": name, "new": old is None,
                        "added": sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
                        "removed": sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))})
        return out

    def save_dir(self, directory: str, only: set[str] | None = None) -> list[str]:
        """Write the data files into ``directory``; returns the names written.

        A file is only rewritten if its content differs from what is on disk, and if
        ``only`` is given, only those files are considered.
        """
        os.makedirs(directory, exist_ok=True)
        written = []
        for name, text in self.render_files().items():
            if only is not None and name not in only:
                continue
            path = os.path.join(directory, name)
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
            written.append(name)
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
