"""Sample clinic used to seed a fresh install: 3 locations, 3 doctors, 6 staff."""
from __future__ import annotations

from datetime import date, timedelta

from .model import Clinic, WEEKDAYS, parse_time

# weekday -> list of (location_id, start, end); doctors shuffle between locations
DOCTOR_ROSTER = {
    "lim": {0: [("central", "09:00", "19:00")],
            1: [("central", "09:00", "13:00"), ("riverside", "14:00", "19:00")],
            2: [("riverside", "10:00", "20:00")],
            3: [("central", "09:00", "19:00")],
            4: [("northgate", "09:00", "17:00")],
            5: [("central", "09:00", "17:00")]},
    "patel": {0: [("riverside", "10:00", "20:00")],
              1: [("northgate", "09:00", "17:00")],
              2: [("central", "09:00", "19:00")],
              3: [("riverside", "10:00", "20:00")],
              4: [("central", "09:00", "19:00")],
              5: [("northgate", "09:00", "13:00"), ("central", "14:00", "17:00")]},
    "okafor": {0: [("central", "09:00", "19:00")],
               1: [("riverside", "10:00", "20:00")],
               2: [("northgate", "09:00", "17:00")],
               3: [("northgate", "09:00", "17:00")],
               4: [("riverside", "10:00", "20:00")],
               5: [("riverside", "10:00", "16:00")]},
}


def build_sample(start: date | None = None, days: int = 28) -> Clinic:
    start = start or date.today()
    data = {
        "locations": [
            {"id": "central", "name": "Central",
             "opening_hours": {d: ["09:00", "19:00"] for d in WEEKDAYS[:5]} | {"sat": ["09:00", "17:00"]},
             "rooms": [
                 {"id": "central_laser_a", "name": "Laser Suite A", "equipment": ["laser"]},
                 {"id": "central_laser_b", "name": "Laser Suite B", "equipment": ["laser", "picolaser"]},
                 {"id": "central_room1", "name": "Treatment Room 1", "equipment": []},
                 {"id": "central_facial", "name": "Facial Room", "equipment": ["hydrafacial", "led"]},
             ]},
            {"id": "riverside", "name": "Riverside",
             "opening_hours": {d: ["10:00", "20:00"] for d in WEEKDAYS[:5]} | {"sat": ["10:00", "16:00"]},
             "rooms": [
                 {"id": "river_room1", "name": "Treatment Room 1", "equipment": []},
                 {"id": "river_laser", "name": "Laser & HIFU Suite", "equipment": ["laser", "hifu"]},
                 {"id": "river_facial", "name": "Facial Room", "equipment": ["hydrafacial"]},
             ]},
            {"id": "northgate", "name": "Northgate",
             "opening_hours": {d: ["09:00", "17:00"] for d in WEEKDAYS[1:6]},
             "rooms": [
                 {"id": "north_room1", "name": "Treatment Room 1", "equipment": []},
                 {"id": "north_room2", "name": "Treatment Room 2", "equipment": ["hifu", "rf"]},
             ]},
        ],
        "treatments": [
            {"id": "consult", "name": "Consultation", "duration_minutes": 30, "staff_required": 0},
            {"id": "botox", "name": "Botox", "duration_minutes": 30, "staff_required": 1},
            {"id": "filler", "name": "Dermal filler", "duration_minutes": 45, "staff_required": 1},
            {"id": "laser_hair", "name": "Laser hair removal", "duration_minutes": 60,
             "required_equipment": ["laser"], "staff_required": 1},
            {"id": "pico", "name": "Pico laser (pigmentation)", "duration_minutes": 60,
             "required_equipment": ["picolaser"], "staff_required": 1},
            {"id": "hifu", "name": "HIFU lifting", "duration_minutes": 90,
             "required_equipment": ["hifu"], "staff_required": 2},
            {"id": "hydrafacial", "name": "Hydrafacial", "duration_minutes": 60,
             "required_equipment": ["hydrafacial"], "staff_required": 1},
            {"id": "rf_needling", "name": "RF microneedling", "duration_minutes": 90,
             "required_equipment": ["rf"], "staff_required": 1},
        ],
        "doctors": [
            {"id": "lim", "name": "Dr Lim",
             "treatments": ["consult", "botox", "filler", "laser_hair", "pico"]},
            {"id": "patel", "name": "Dr Patel",
             "treatments": ["consult", "botox", "filler", "hifu", "rf_needling"]},
            {"id": "okafor", "name": "Dr Okafor",
             "treatments": ["consult", "laser_hair", "pico", "hydrafacial", "rf_needling"]},
        ],
        "staff": [{"id": f"s{i}", "name": n} for i, n in
                  enumerate(["Ana", "Ben", "Chloe", "Dev", "Elif", "Farah"], 1)],
    }
    clinic = Clinic.from_dict(data)

    staff_ids = list(clinic.staff)
    for offset in range(days):
        day = start + timedelta(days=offset)
        for doctor_id, roster in DOCTOR_ROSTER.items():
            for loc_id, s, e in roster.get(day.weekday(), []):
                clinic.add_shift("doctor", doctor_id, loc_id, day, parse_time(s), parse_time(e))
        # staff are interchangeable: rotate them around the open locations, 2 per site
        open_locs = [l for l in clinic.locations.values() if l.hours_on(day)]
        rotated = staff_ids[day.toordinal() % len(staff_ids):] + staff_ids[:day.toordinal() % len(staff_ids)]
        for i, loc in enumerate(open_locs):
            for sid in rotated[2 * i:2 * i + 2]:
                o, c = loc.hours_on(day)
                clinic.add_shift("staff", sid, loc.id, day, o, c)

    # one example booking on the first open weekday
    day = next(start + timedelta(days=i) for i in range(7) if (start + timedelta(days=i)).weekday() == 0)
    clinic.book("botox", "central", day, parse_time("10:00"), client="Example client")
    return clinic
