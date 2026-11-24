"""Sample clinic used to seed a fresh install: 3 locations, 3 doctors, 6 staff."""
from __future__ import annotations

from datetime import date, timedelta

from .model import Clinic, WEEKDAYS, parse_time

# weekday (0=Mon) -> the one location the doctor is at that day (missing = not working)
DOCTOR_ROSTER = {
    "tan": {0: "central", 1: "central", 2: "riverside", 3: "central", 4: "northgate", 5: "central"},
    "patel": {0: "riverside", 1: "northgate", 2: "central", 3: "riverside", 4: "central", 5: "northgate"},
    "okafor": {0: "central", 1: "riverside", 2: "northgate", 3: "northgate", 4: "riverside", 5: "riverside"},
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
        # numbing (1 staff) -> doctor treatment (doctor + 1 staff) -> staff treatment (1 staff);
        # a 0 step is skipped; doctor_minutes 0 = carried out by staff alone
        "treatments": [
            {"id": "consult", "name": "Consultation", "doctor_minutes": 30},
            {"id": "botox", "name": "Botox", "numbing_minutes": 15, "doctor_minutes": 15},
            {"id": "filler", "name": "Dermal filler", "numbing_minutes": 15, "doctor_minutes": 30,
             "staff_minutes": 15},
            {"id": "laser_hair", "name": "Laser hair removal", "numbing_minutes": 15, "doctor_minutes": 30,
             "staff_minutes": 15, "required_equipment": ["laser"]},
            {"id": "pico", "name": "Pico laser (pigmentation)", "numbing_minutes": 15, "doctor_minutes": 30,
             "staff_minutes": 15, "required_equipment": ["picolaser"]},
            {"id": "hifu", "name": "HIFU lifting", "numbing_minutes": 30, "doctor_minutes": 45,
             "staff_minutes": 15, "required_equipment": ["hifu"]},
            {"id": "rf_needling", "name": "RF microneedling", "numbing_minutes": 30, "doctor_minutes": 45,
             "staff_minutes": 15, "required_equipment": ["rf"]},
            {"id": "hydrafacial", "name": "Hydrafacial (staff only)", "staff_minutes": 60,
             "required_equipment": ["hydrafacial"]},
        ],
        "doctors": [
            {"id": "tan", "name": "Dr Tan",
             "treatments": ["consult", "botox", "filler", "laser_hair", "pico"]},
            {"id": "patel", "name": "Dr Patel",
             "treatments": ["consult", "botox", "filler", "hifu", "rf_needling"]},
            {"id": "okafor", "name": "Dr Okafor",
             "treatments": ["consult", "laser_hair", "pico", "rf_needling"]},
        ],
        "staff": [{"id": f"s{i}", "name": n} for i, n in
                  enumerate(["Ana", "Ben", "Chloe", "Dev", "Elif", "Farah"], 1)],
    }
    clinic = Clinic.from_dict(data)

    staff_ids = list(clinic.staff)
    for offset in range(days):
        day = start + timedelta(days=offset)
        for doctor_id, roster in DOCTOR_ROSTER.items():
            if day.weekday() in roster:
                clinic.set_presence("doctor", doctor_id, day, roster[day.weekday()])
        # staff are interchangeable: rotate them around the open locations, 2 per site
        open_locs = [l for l in clinic.locations.values() if l.hours_on(day)]
        rotated = staff_ids[day.toordinal() % len(staff_ids):] + staff_ids[:day.toordinal() % len(staff_ids)]
        for i, loc in enumerate(open_locs):
            for sid in rotated[2 * i:2 * i + 2]:
                clinic.set_presence("staff", sid, day, loc.id)

    # one example booking on the first open weekday
    day = next(start + timedelta(days=i) for i in range(7) if (start + timedelta(days=i)).weekday() == 0)
    clinic.book("botox", "central", day, parse_time("10:00"), client="Example client")
    return clinic
