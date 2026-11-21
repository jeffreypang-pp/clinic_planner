"""Lists of treatments to book: the Timetable tab's "Add treatments" list.

A list is the *plan* (each treatment's three step times, clinic, room, proposed start, ...),
not bookings.  A list is saved and loaded as a stand-alone file ("Save to file…" / "Load from
file…"); nothing is stored by the server.  Each item uses the same field names as
``Clinic.book_list`` so a list can be run as-is.
"""
from __future__ import annotations

import re
from datetime import datetime

from .model import STEP_NAMES, Clinic, ClinicError, _dumps, check_step_minutes, format_slot, parse_date, parse_time

MAX_ITEMS = 200
MAX_NAME = 80


def normalise_item(clinic: Clinic, location_id: str, n: int, item, lenient: bool = False,
                   warnings: list | None = None) -> dict:
    """Check one item (``n`` = its 1-based position) and return it in canonical form.

    An item may also carry the ``date`` it is for (``YYYY-MM-DD``).

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
    day = None      # optional: the date this treatment is for; without one it goes on whichever day the list is booked
    if item.get("date"):
        try:
            day = parse_date(item["date"]).isoformat()
        except ClinicError as e:
            if not lenient:
                raise type(e)(f"{what}: {e}") from None
            warnings.append(f"{what}: its date isn't a valid date; it will use the day you book on")
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
            "date": day,
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
    clean = [normalise_item(clinic, loc.id, i + 1, item, lenient, warnings)
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

