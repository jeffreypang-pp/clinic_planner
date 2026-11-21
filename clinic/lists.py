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
import re
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
            default = entry.get("location_id")
            loc = clinic.locations.get(default)
            # older lists had one clinic for the whole list: give each item its clinic explicitly
            items = [dict(i, location_id=i.get("location_id") or default) if isinstance(i, dict) else i
                     for i in entry.get("items", [])]
            used = list(dict.fromkeys(i.get("location_id") for i in items if isinstance(i, dict)))
            out.append({"name": name, "location_id": default, "location": loc.name if loc else None,
                        "locations": [clinic.locations[l].name if l in clinic.locations else "(removed clinic)" for l in used],
                        "saved": entry.get("saved"), "count": len(items), "items": items})
        return out

    def save(self, clinic: Clinic, name, location_id, items, overwrite: bool = False) -> dict:
        """Validate and store a list.  Refuses (``Conflict``) to replace an existing name unless
        ``overwrite``.  Reads and rewrites only this file, and only this list's entry changes."""
        name, loc_id, clean, _warnings = build_list(clinic, name, location_id, items)
        lists = self._read()
        if name in lists and not overwrite:
            raise Conflict(f"A saved list called {name!r} already exists")
        lists[name] = {"location_id": loc_id, "saved": datetime.now().isoformat(timespec="seconds"),
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
    def _normalise(clinic: Clinic, location_id: str, n: int, item, lenient: bool = False,
                   warnings: list | None = None) -> dict:
        """Check one item (``n`` = its 1-based position) and return it in canonical form.

        Each item belongs to a clinic (``location_id``); an item without one belongs to the list's
        clinic (``location_id`` argument), which is how older lists and files load.

        ``lenient`` (used when loading a file): ids that no longer exist (clinic, room, doctor,
        staff, template) are dropped with a warning instead of being an error; the times and start
        are always checked."""
        what = f"Treatment {n}"
        warnings = warnings if warnings is not None else []
        if not isinstance(item, dict):
            raise ClinicError(f"{what}: must be an object")
        if lenient:
            item = dict(item)
            if isinstance(item.get("staff"), dict):
                gone = {k for k, v in item["staff"].items() if v and v not in clinic.staff}
                if gone:
                    warnings.append(f"{what}: staff member(s) no longer exist; set to assign later")
                    item["staff"] = {k: (None if k in gone else v) for k, v in item["staff"].items()}
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
        item_loc = item.get("location_id") or location_id
        if item_loc not in clinic.locations:
            if not lenient:
                try:
                    clinic._get(clinic.locations, item_loc, "clinic")
                except ClinicError as e:
                    raise type(e)(f"{what}: {e}") from None
            warnings.append(f"{what}: its clinic no longer exists; moved to {clinic.locations[location_id].name}")
            item_loc = location_id
        room_id = item.get("room_id") or None
        if room_id and (room_id not in clinic.rooms or clinic.rooms[room_id].location_id != item_loc):
            if not lenient:
                clinic._get(clinic.rooms, room_id, "room")
                raise ClinicError(f"{what}: room {room_id!r} is not at {clinic.locations[item_loc].name}")
            warnings.append(f"{what}: its room isn't at {clinic.locations[item_loc].name} any more; choose a room")
            room_id = None
        doctor_id = item.get("doctor_id") or None
        if doctor_id and doctor_id not in clinic.doctors:
            if not lenient:
                clinic._get(clinic.doctors, doctor_id, "doctor")
            warnings.append(f"{what}: its doctor no longer exists; set to any available")
            doctor_id = None
        template = item.get("treatment_id") or None
        if template and template not in clinic.treatments:
            if not lenient:
                clinic._get(clinic.treatments, template, "treatment")
            warnings.append(f"{what}: its template no longer exists; treated as a custom treatment")
            template = None
        return {"name": str(item.get("name") or "").strip()[:200],
                "location_id": item_loc,
                "treatment_id": template,
                "numbing_minutes": item.get("numbing_minutes", 0),
                "doctor_minutes": item.get("doctor_minutes", 0),
                "staff_minutes": item.get("staff_minutes", 0),
                "room_id": room_id, "doctor_id": doctor_id,
                "staff": {step: named.get(step) for step in STEP_NAMES if step in active},
                "start": format_slot(parse_time(start)),
                "client": str(item.get("client") or "").strip()[:200]}


def build_list(clinic: Clinic, name, location_id, items, lenient: bool = False) -> tuple[str, str, list, list]:
    """Validate a whole list: returns ``(name, location_id, canonical_items, warnings)``."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME \
            or any(ord(ch) < 32 for ch in name):
        raise ClinicError(f"A list needs a name of 1-{MAX_NAME} characters")
    loc = clinic._get(clinic.locations, location_id, "location")
    if not isinstance(items, list) or not items:
        raise ClinicError("The list is empty; add at least one treatment")
    if len(items) > MAX_ITEMS:
        raise ClinicError(f"At most {MAX_ITEMS} treatments per list")
    warnings: list[str] = []
    clean = [TreatmentLists._normalise(clinic, loc.id, i + 1, item, lenient, warnings)
             for i, item in enumerate(items)]
    return name.strip(), loc.id, clean, warnings


# A single list as a stand-alone file (what "Save to file…" writes and "Load from file…" reads)
LIST_FILE_KIND = "clinic-treatment-list"
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    """A file name for a list: no path parts or characters Windows/macOS/Linux dislike, ends in .json."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    if base.lower().endswith(".json"):
        base = base[:-5].rstrip(" .")
    base = base[:100].rstrip(" .") or "treatment list"
    if base.split(".")[0].lower() in _WINDOWS_RESERVED:
        base = "_" + base
    return base + ".json"


def render_list_file(clinic: Clinic, name, location_id, items, lenient: bool = False) -> dict:
    """Validate a list and produce the file for it: ``{filename, text, name, location_id, items, warnings}``."""
    name, loc_id, clean, warnings = build_list(clinic, name, location_id, items, lenient)
    data = {"kind": LIST_FILE_KIND, "version": 1, "name": name, "location_id": loc_id,
            "saved": datetime.now().isoformat(timespec="seconds"), "items": clean}
    return {"filename": safe_filename(name), "text": _dumps(data), "name": name, "location_id": loc_id,
            "items": clean, "warnings": warnings}


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
