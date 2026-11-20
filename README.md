# Clinic Planner (prototype)

Timetable model for a multi-location aesthetics clinic. Python 3.10+ standard library only.

```bash
python -m clinic.server            # http://127.0.0.1:8000, saves to clinic.json
python -m unittest discover -s tests -t .
```

The clinic lives in `clinic.json` (committed; pass `--data` to use another file). Every change in the UI
is written back to it. If the file doesn't exist, a sample clinic (3 locations, 3 doctors, 6 staff,
8 treatments, 4 weeks of roster) is generated instead.

## Model (`clinic/model.py`)

| Concept | Notes |
|---|---|
| Time | 15-minute slots (`00:00` = slot 0, `24:00` = slot 96). All times must sit on a 15-min boundary. |
| Location | Weekly opening hours (editable) + per-date overrides (`null` = closed that day). Owns rooms. |
| Room | Belongs to one location; has equipment tags (`laser`, `hifu`, ...). |
| Treatment | Duration (multiple of 15 min), required equipment, number of staff (0+). |
| Doctor | Has a set of treatments they can perform. Needed for the whole treatment. |
| Staff | Interchangeable: any free, rostered staff member can assist any treatment. |
| Shift | A person at **one location** for a span on a date. Shuffling = back-to-back shifts at different sites. Shifts of one person can't overlap. |
| Room block | Time span on a date when a room can't be used (`room_blocks` in the JSON). |
| Booking | Treatment + location + date + start; uses 1 doctor, N staff, 1 room for its full duration. |

A treatment can start at a slot at a location iff, for the **whole** duration:
the location is open; a qualified doctor is rostered *at that location* and not in another booking;
enough staff are rostered there and free; and a room there with the required equipment is free.
`Clinic.check_availability()` returns the free doctors/staff/rooms, or the specific reasons it can't.

Editing the roster or opening hours never deletes bookings; any booking that no longer fits is
reported in `Clinic.issues()` (shown as a banner in the UI).

## UI tabs

* **Timetable** - per location and date, one row per doctor / staff / room, one cell per 15 min
  (free, booked, away at another location, not rostered, closed, room blocked). Click a free cell to check availability from that time.
  **Edit availability**: drag across a row, then *Available here* / *Not available here* for a doctor or staff member
  (their shifts are split/merged automatically; "available here" moves them from another location for that span),
  or *Block* / *Unblock* for a room (maintenance etc.). Optionally repeat weekly until a date.
* **Availability & booking** - choose treatment / location (or all) / start time; see who and what is free
  or why not; "find open slots" for the day; book with auto-assignment.
* **Roster** - add/remove shifts (date ranges + weekdays), view and cancel bookings.
* **Setup & save** - edit opening hours per weekday; edit the *whole clinic* as JSON (rooms, treatments,
  doctors & skills, staff, shifts, bookings), validate and save; download/upload JSON.

## API

`GET /api/clinic`, `PUT /api/clinic` (replace whole clinic, validated first),
`GET /api/timetable?location=&date=`, `GET /api/availability?treatment=&date=&start=HH:MM[&location=]`,
`GET /api/slots?treatment=&date=[&location=]`, `GET|POST /api/bookings`, `DELETE /api/bookings/<id>`,
`GET|POST /api/shifts`, `DELETE /api/shifts/<id>`, `PUT /api/availability` (paint a doctor/staff/room range on or off, optional weekly repeat),
`PUT /api/locations/<id>/hours`.

## Not modelled yet

Travel time between locations, per-person shift limits/breaks, recurring weekly roster templates,
client records, doctor-only vs doctor-and-staff-for-part-of-the-treatment, auth / multi-user editing.
