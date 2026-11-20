# Clinic Planner (prototype)

Timetable model for a multi-location aesthetics clinic. Python 3.10+ standard library only.

```bash
python -m clinic.server            # http://127.0.0.1:8000, saves into clinic_data/
python -m unittest discover -s tests -t .
```

## Data: three files, grouped by how often they change (`clinic_data/`)

| File | Changes | Contains |
|---|---|---|
| `resources.json` | rarely | locations, **rooms** (with equipment) per location, **doctors**, **staff** |
| `planning.json` | regularly | opening hours (+ one-off date overrides), **treatments**, **doctor skills** |
| `schedule.json` | constantly | **presence** (who is in which location each day), **blocked times** (per doctor / staff member / room), **bookings** |

**Nothing is saved automatically.** Edits (ticks, bookings, blocks, hours, JSON edits) live in memory on the
server. The top bar shows `● Unsaved: schedule.json (+3 −1)`; press **Save…** and confirm the dialog to write
the files, or **Discard** to throw the changes away and reload from disk. Only the files that changed are written
(making a booking touches only `schedule.json`). Restarting the server drops unsaved changes.

Safe with `git pull`: if a data file changes on disk while the app is open, the bar shows
`⚠ Changed on disk: …`; saving a file that changed behind the app's back is refused unless you confirm
an explicit overwrite, and **Reload from disk** picks up the pulled version. Files you didn't edit are never
touched by a save. The files reference each other by id and are validated together. If `clinic_data/` has
no files, a sample clinic is loaded (unsaved until you save it).

## Model (`clinic/model.py`)

| Concept | Notes |
|---|---|
| Time | 15-minute slots (`00:00` = slot 0, `24:00` = slot 96). All times must sit on a 15-min boundary. |
| Location | Weekly opening hours (editable) + per-date overrides (`null` = closed that day). Owns rooms. |
| Room | Belongs to one location; has equipment tags (`laser`, `hifu`, ...). |
| Treatment | Duration (multiple of 15 min), required equipment, number of staff (0+). |
| Doctor | Has a set of treatments they can perform (skills). Needed for the whole treatment. |
| Staff | Interchangeable: any free, present staff member can assist any treatment. |
| Presence | Per **day**, a doctor/staff member is at **one location or none** (the tick box). Stored in `schedule.json` as `presence[date][doctors\|staff][id] = location_id`, so "at most one location per day" holds by construction. Once present they are **free all day unless booked or blocked**. |
| Block | A time span on a date when a doctor, staff member or room can't be used (lunch, leave, maintenance). Optional. |
| Booking | Treatment + location + date + start; uses 1 doctor, N staff, 1 room for its full duration. |

A treatment can start at a slot at a location iff, for the **whole** duration:
the location is open; a qualified doctor is ticked in *at that location that day* and neither booked nor blocked;
enough staff are ticked in there and free; and a room there with the required equipment is free and not blocked.
`Clinic.check_availability()` returns the free doctors/staff/rooms, or the specific reasons it can't.

Editing presence, blocks or opening hours never deletes bookings; any booking that no longer fits is
reported in `Clinic.issues()` (shown as a banner in the UI).

## UI tabs

* **Timetable** - per location and date, one row per doctor / staff / room, one cell per 15 min
  (free, booked, away at another location, not in any location today, closed, blocked).
  **Tick box** on each doctor/staff row: ticked = in this location that day; unticking = not in any location;
  ticking someone who is at another location moves them here.
  Click a free cell to check availability from that time.
  **Edit availability**: drag across a row (doctor, staff or room), then *Block* / *Unblock*; optionally repeat weekly.
* **Availability & booking** - choose treatment / location (or all) / start time; see who and what is free
  or why not; "find open slots" for the day; book with auto-assignment.
* **Roster & bookings** - a weekly grid to set each person's location per day (same data as the tick box),
  a form to set a location across a date range / weekdays, and the day's bookings (cancel here).
* **Setup & save** - edit opening hours per weekday; edit each of the three data files as JSON
  (add rooms, treatments, doctors, skills...), validate and apply (then Save… in the top bar); download/upload a file.

## API

`GET /api/clinic` (merged view + the 3 groups + issues + unsaved/external files), `GET /api/status`,
`POST /api/save` (`{"force": true}` to overwrite files changed on disk), `POST /api/discard`, `PUT /api/clinic` (`{"resources": {...}}` etc.;
replaces the given groups in memory, validated together with the rest first),
`GET /api/timetable?location=&date=`, `GET /api/roster?start=&days=`,
`PUT /api/presence` (`role`, `person_id`, `location_id` or null, `date`/`dates`),
`PUT /api/availability` (block/unblock a doctor/staff/room range, optional weekly repeat),
`PUT /api/locations/<id>/hours`,
`GET /api/availability?treatment=&date=&start=HH:MM[&location=]`, `GET /api/slots?treatment=&date=[&location=]`,
`GET|POST /api/bookings`, `DELETE /api/bookings/<id>`.

## Not modelled yet

Travel time between locations, someone being at two locations in one day, breaks as a default,
recurring weekly roster templates (use the roster's "many days" form), client records,
doctor-only vs doctor-and-staff-for-part-of-the-treatment, auth / multi-user editing.
