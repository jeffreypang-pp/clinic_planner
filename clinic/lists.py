"""Named lists of treatments to book: the Timetable tab's "Add treatments" list.

A saved list is the *plan* (each treatment's three step times, room, proposed start, ...),
not bookings.  Lists live in ``treatment_lists.json`` next to the clinic data files, separate
from the three clinic data groups, and are only written when the user saves one (the UI asks
for confirmation every time).  Each item uses the same field names as ``Clinic.book_list`` so a
saved list can be run as-is.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

from .model import (STEP_NAMES, Clinic, ClinicError, Conflict, NotFound, _dumps, check_step_minutes,
                    format_slot, parse_time)

FILE_NAME = "treatment_lists.json"
MAX_ITEMS = 200
MAX_NAME = 80


class TreatmentLists:
    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, FILE_NAME)

    # ---- file ------------------------------------------------------------
    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ClinicError(f"{FILE_NAME}: invalid JSON ({e})") from None
        lists = data.get("lists") if isinstance(data, dict) else None
        if not isinstance(lists, dict):
            raise ClinicError(f'{FILE_NAME}: expected {{"lists": {{...}}}}')
        return lists

    def _write(self, lists: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_dumps({"version": 1, "lists": lists}))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---- api -------------------------------------------------------------
    def all(self, clinic: Clinic) -> list[dict]:
        """Every saved list (sorted by name), each with its items."""
        out = []
        for name, entry in sorted(self._read().items(), key=lambda kv: kv[0].lower()):
            loc = clinic.locations.get(entry.get("location_id"))
            out.append({"name": name, "location_id": entry.get("location_id"),
                        "location": loc.name if loc else None, "saved": entry.get("saved"),
                        "count": len(entry.get("items", [])), "items": entry.get("items", [])})
        return out

    def save(self, clinic: Clinic, name, location_id, items, overwrite: bool = False) -> dict:
        """Validate and store a list.  Refuses (``Conflict``) to replace an existing name unless
        ``overwrite``.  Reads and rewrites only this file, and only this list's entry changes."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME \
                or any(ord(ch) < 32 for ch in name):
            raise ClinicError(f"A list needs a name of 1-{MAX_NAME} characters")
        name = name.strip()
        loc = clinic._get(clinic.locations, location_id, "location")
        if not isinstance(items, list) or not items:
            raise ClinicError("The list is empty; add at least one treatment")
        if len(items) > MAX_ITEMS:
            raise ClinicError(f"At most {MAX_ITEMS} treatments per list")
        clean = [self._normalise(clinic, loc.id, i + 1, item) for i, item in enumerate(items)]
        lists = self._read()
        if name in lists and not overwrite:
            raise Conflict(f"A saved list called {name!r} already exists")
        lists[name] = {"location_id": loc.id, "saved": datetime.now().isoformat(timespec="seconds"),
                       "items": clean}
        self._write(lists)
        return {"name": name, **lists[name]}

    def delete(self, name: str) -> None:
        lists = self._read()
        if name not in lists:
            raise NotFound(f"No saved list called {name!r}")
        del lists[name]
        self._write(lists)

    @staticmethod
    def _normalise(clinic: Clinic, location_id: str, n: int, item) -> dict:
        """Check one item (``n`` = its 1-based position) and return it in canonical form."""
        what = f"Treatment {n}"
        if not isinstance(item, dict):
            raise ClinicError(f"{what}: must be an object")
        slots = check_step_minutes(item.get("numbing_minutes", 0), item.get("doctor_minutes", 0),
                                   item.get("staff_minutes", 0), what)
        active = {step for step, n in zip(STEP_NAMES, slots) if n}
        try:    # who does which step: a staff id, or null = "assign later"
            named = clinic._step_staff(active, item.get("staff"))
        except ClinicError as e:
            raise type(e)(f"{what}: {e}") from None
        start = item.get("start")
        if not start:
            raise ClinicError(f"{what}: missing 'start'")
        room_id = item.get("room_id") or None
        if room_id and clinic._get(clinic.rooms, room_id, "room").location_id != location_id:
            raise ClinicError(f"{what}: room {room_id!r} is not at this location")
        doctor_id = item.get("doctor_id") or None
        if doctor_id:
            clinic._get(clinic.doctors, doctor_id, "doctor")
        template = item.get("treatment_id") or None
        if template:
            clinic._get(clinic.treatments, template, "treatment")
        return {"name": str(item.get("name") or "").strip()[:200],
                "treatment_id": template,
                "numbing_minutes": item.get("numbing_minutes", 0),
                "doctor_minutes": item.get("doctor_minutes", 0),
                "staff_minutes": item.get("staff_minutes", 0),
                "room_id": room_id, "doctor_id": doctor_id,
                "staff": {step: named.get(step) for step in STEP_NAMES if step in active},
                "start": format_slot(parse_time(start)),
                "client": str(item.get("client") or "").strip()[:200]}


class MemoryLists(TreatmentLists):
    """The same lists and rules, kept in a dict that the caller stores (the browser, in the
    stateless/Vercel deployment) instead of a file on disk."""

    def __init__(self, data: dict | None = None) -> None:
        self.path = "browser storage"
        self.data = dict(data or {})

    def _read(self) -> dict:
        return dict(self.data)

    def _write(self, lists: dict) -> None:
        self.data = lists
