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
| Treatment | Three sequential steps, each a multiple of 15 min (0 = skipped): **numbing** (1 staff) → **doctor treatment** (a qualified doctor + 1 staff) → **staff treatment** (1 staff). `doctor_minutes: 0` makes it a **staff-only** treatment (just `staff_minutes`). Required room equipment. One room is held for the whole treatment. |
| Doctor | Has a set of treatments they can perform (skills). Needed for the whole treatment. |
| Staff | Interchangeable: any free, present staff member can assist any treatment. |
| Presence | Per **day**, a doctor/staff member is at **one location or none** (the tick box). Stored in `schedule.json` as `presence[date][doctors\|staff][id] = location_id`, so "at most one location per day" holds by construction. Once present they are **free all day unless booked or blocked**. |
| Block | A time span on a date when a doctor, staff member or room can't be used (lunch, leave, maintenance). Optional. |
| Booking | Treatment + location + date + start. Holds 1 room for the whole treatment; the doctor is busy only during the doctor step, and each step has its own staff member (kept the same person from step to step where possible). |

A treatment can start at a slot at a location iff:
the location is open for the whole treatment; **each step** has a free staff member ticked in there for that step's window;
the doctor step has a qualified doctor ticked in there and neither booked nor blocked during it; and a room there with the
required equipment is free and not blocked for the whole treatment (so a doctor can still be numbing-/staff-stepped around).
`Clinic.check_availability()` returns the free staff per step, the free doctors and rooms, or the specific reasons it can't.

### Booking a list of treatments (`Clinic.book_list`)

Go down a list of treatments with proposed start times. Each is booked in turn and sees what the ones above it used.
The first one that can't be carried out for lack of a doctor, staff member or room is reported as *cannot be performed*
and the list **stops there**: those above it stay booked, those below are *not attempted*. `dry_run` reports the same
outcome without booking anything. Assignment is greedy and never reshuffled afterwards (first free qualified doctor,
first free suitable room, first free staff per step), so a failure means "no free resource given what's already booked".

Treatment format (in `planning.json`): `{"id", "name", "numbing_minutes", "doctor_minutes", "staff_minutes", "required_equipment"}`;
the old `duration_minutes` / `staff_required` are rejected with a message.

Editing presence, blocks or opening hours never deletes bookings; any booking that no longer fits is
reported in `Clinic.issues()` (shown as a banner in the UI).

## UI tabs

* **Timetable** - per location and date, one row per doctor / staff / room, one cell per 15 min
  (free, booked per step, away at another location, not in any location today, closed, blocked).
  **This is where you book.** The **Add treatments** panel beside the grid takes treatments one after another:
  pick a treatment and start time (or click a free cell to use that time), press **Book next**, and the grid updates so you can
  watch how rooms, doctors and staff fill up (the latest booking is ringed). A treatment that can't be carried out is reported
  with the reasons and nothing is booked for it. **Book all** goes down the list and stops at the first treatment that can't be
  carried out; **Check all** is a dry run; a booked row can be cancelled to try something else.
  **Tick box** on each doctor/staff row: ticked = in this location that day; unticking = not in any location;
  ticking someone who is at another location moves them here.
  **Edit availability**: drag across a row (doctor, staff or room), then *Block* / *Unblock*; optionally repeat weekly.
  **Clear day…**: cancels every booking and removes every blocked time for this location on this date
  (rooms here, plus doctors/staff ticked in here), after showing what will go and asking to confirm.
  Tick boxes are left alone, and like all edits it only changes memory until you Save…
* **Availability check** - look-up only: choose treatment / location (or all) / start time to see the free staff per step,
  doctors and rooms, or why not; "find open slots" for the day; *Add to Timetable* sends a result to the booking panel.
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
`GET|POST /api/bookings`, `DELETE /api/bookings/<id>`, `POST /api/book-list` (`location_id`, `date`, `items: [{treatment_id, start, client?}]`, optional `dry_run`), `POST /api/clear-day` (`location_id`, `date`, optional `dry_run`).

## Not modelled yet

Travel time between locations, someone being at two locations in one day, breaks as a default,
recurring weekly roster templates (use the roster's "many days" form), client records,
doctor-only vs doctor-and-staff-for-part-of-the-treatment, auth / multi-user editing.
