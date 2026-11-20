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

A fourth file, `treatment_lists.json`, holds **saved lists of treatments** (see below). It is separate from the three
clinic data files above, and only written when you save a list.

**Nothing is saved automatically.** Edits (ticks, bookings, blocks, hours, JSON edits) live in memory on the
server. The top bar shows `● Unsaved: schedule.json (+3 −1)`; press **Save…** and confirm the dialog to write
the files, or **Discard** to throw the changes away and reload from disk. Only the files that changed are written
(making a booking touches only `schedule.json`). Restarting the server drops unsaved changes.

Safe with `git pull`: if a data file changes on disk while the app is open, the bar shows
`⚠ Changed on disk: …`; saving a file that changed behind the app's back is refused unless you confirm
an explicit overwrite, and **Reload from disk** picks up the pulled version. Files you didn't edit are never
touched by a save. The files reference each other by id and are validated together. If `clinic_data/` has
no files, a sample clinic is loaded (unsaved until you save it).

## Deploying to Vercel

The repo is ready to import into Vercel as-is (`vercel.json`, `public/index.html`, `api/*.py`; standard library only,
so there is nothing to install or build):

```bash
npm i -g vercel
vercel          # preview deployment; `vercel --prod` for production
```

or push to GitHub and "Import Project" in the Vercel dashboard (leave the framework as *Other*; `vercel.json` sets the rest).

**Vercel has no long-running server and no writable disk**, so the deployment runs in *stateless mode*
(`clinic/stateless.py`) instead of keeping the data in server memory and writing files:

* The **browser holds your working data** (and keeps it in `localStorage`, so it survives a reload) and sends it with each
  request; three small Python functions (`api/mode.py`, `api/initial.py`, `api/call.py`) run exactly the same model code and
  send the result back. The server stores nothing.
* The files in `clinic_data/` are the **read-only starting point** shipped with each deployment.
* **Save (download)…** (confirmed every time) can't write to your repository, so it **downloads the changed files**
  (`resources.json`, `planning.json`, `schedule.json`). Put them in `clinic_data/` and commit; pushing redeploys with the new data.
  If a newer deployment's data differs from what your browser started from, the bar shows `⚠ Newer data deployed`, saving over
  it is refused unless you confirm, and **Reload deployed data** picks it up.
* **Saved treatment lists** live in the browser only (per browser, not shared). A committed `clinic_data/treatment_lists.json`
  is loaded as the starting set.
* Each request carries the working data (well under Vercel's 4.5 MB request limit for a clinic this size).

**Privacy:** `clinic_data/` is served to anyone who can open the site (staff and doctor names, bookings, ...). Turn on
*Deployment Protection* (Vercel Authentication or a password) for the project before putting real data there.

Try the Vercel behaviour locally without Vercel: `python -m clinic.server --stateless` (serves the same API and page).

## Model (`clinic/model.py`)

| Concept | Notes |
|---|---|
| Time | 15-minute slots (`00:00` = slot 0, `24:00` = slot 96). All times must sit on a 15-min boundary. |
| Location | Weekly opening hours (editable) + per-date overrides (`null` = closed that day). Owns rooms. |
| Room | Belongs to one location; has equipment tags (`laser`, `hifu`, ...). |
| Treatment | A treatment is given by three sequential step times (each a multiple of 15 min, 0 = skipped) and a room: **numbing** (1 staff) → **doctor treatment** (a doctor + 1 staff) → **staff treatment** (1 staff). **The one rule: if the doctor treatment time is 0, the numbing time must be 0.** (Doctor 0 = carried out by staff alone.) One room is held for the whole treatment. |
| Treatment template | Optional entry in `planning.json` (`treatments`, with `numbing_minutes` / `doctor_minutes` / `staff_minutes` / `required_equipment`) plus doctor skills. Picking a template in the booking panel fills in the name and times; then its doctor skills and room equipment are enforced. Without one, any doctor and any room can be used. |
| Doctor | Has a set of treatments they can perform (skills). Needed for the whole treatment. |
| Staff | Interchangeable: any free, present staff member can assist any treatment. |
| Presence | Per **day**, a doctor/staff member is at **one location or none** (the tick box). Stored in `schedule.json` as `presence[date][doctors\|staff][id] = location_id`, so "at most one location per day" holds by construction. Once present they are **free all day unless booked or blocked**. |
| Block | A time span on a date when a doctor, staff member or room can't be used (lunch, leave, maintenance). Optional. |
| Booking | A treatment (its three times, the room, a start, optional name/client/template) at a location and date. Holds the room for the whole treatment; the doctor is busy only during the doctor step, and each step has its own staff member (kept the same person from step to step where possible). Doctor and staff are assigned automatically (optionally pick the doctor). Stored in `schedule.json` with its own `numbing_minutes` / `doctor_minutes` / `staff_minutes`. |

A treatment can start at a slot at a location iff:
the location is open for the whole treatment; **each step** has a free staff member ticked in there for that step's window;
the doctor step has a doctor ticked in there (qualified, if a template is used) who is neither booked nor blocked during it;
and the chosen room (at that location, with the template's equipment if one is used) is free and not blocked for the whole treatment.
`Clinic.check_custom()` (three times + room) and `Clinic.check_availability()` (a template, any suitable room) return the free
staff per step, doctors and rooms, or the specific reasons it can't.

### Adding treatments one at a time (`Clinic.book_custom`, `Clinic.book_list`)

On the Timetable tab, add treatments to a list one at a time: give the numbing, doctor treatment and staff treatment
minutes and the room, then **Book next** and watch the grid. **Book all** goes down the list in order; each treatment
sees what the ones above it used. The first one that can't be carried out for lack of a doctor, staff member or room
is reported as *cannot be performed* and the list **stops there**: those above it stay booked, those below are
*not attempted*. `dry_run` / **Check all** reports the same outcome without booking anything. Assignment is greedy and never
reshuffled afterwards (first free doctor, first free staff per step), so a failure means "no free resource given what's
already booked".

Template format (in `planning.json`): `{"id", "name", "numbing_minutes", "doctor_minutes", "staff_minutes", "required_equipment"}`;
the old `duration_minutes` / `staff_required` are rejected with a message.

Editing presence, blocks or opening hours never deletes bookings; any booking that no longer fits is
reported in `Clinic.issues()` (shown as a banner in the UI).

## UI tabs

* **Timetable** - per location and date, one row per doctor / staff / room, one cell per 15 min
  (free, booked per step, away at another location, not in any location today, closed, blocked).
  **This is where you book.** The **Add treatments** panel beside the grid takes treatments one at a time: for each, set the
  numbing / doctor / staff minutes (numbing locks to 0 when the doctor time is 0), the room and the start time (or click a free cell
  to use that time, and on a room row that room), then press **Book next**; the grid updates so you can watch how rooms, doctors and
  staff fill up (the latest booking is ringed). A treatment that can't be carried out is reported with the reasons and nothing is
  booked for it. **Book all** goes down the list and stops at the first treatment that can't be carried out; **Check all** is a dry run;
  a booked row can be cancelled to try something else. An optional template fills in the name and times.
  A copy of **Book next** sits in the panel's header, which stays pinned (and the panel scrolls itself to the next row to
  fill in), so you can keep booking without scrolling.
  **Save list… / Load / delete** keep named lists of the treatments in the panel (times, rooms, names, clients; not the bookings)
  in `treatment_lists.json`. Saving always asks for confirmation (and warns before replacing a name); a list belongs to a
  location, and loading one switches to that location and replaces the list in the panel (bookings already made stay).
  **Tick box** on each doctor/staff row: ticked = in this location that day; unticking = not in any location;
  ticking someone who is at another location moves them here.
  **Edit availability**: drag across a row (doctor, staff or room), then *Block* / *Unblock*; optionally repeat weekly.
  **Clear day…**: cancels every booking and removes every blocked time for this location on this date
  (rooms here, plus doctors/staff ticked in here), after showing what will go and asking to confirm.
  Tick boxes are left alone, and like all edits it only changes memory until you Save…
* **Availability check** - look-up only, for treatment templates: choose treatment / location (or all) / start time to see the free staff per step,
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
`GET|PUT|DELETE /api/lists` (saved treatment lists; `PUT` takes `name`, `location_id`, `items`, `overwrite`; `DELETE ?name=`),
`GET|POST /api/bookings`, `DELETE /api/bookings/<id>`, `POST /api/book-list` (`location_id`, `date`, `items`, optional `dry_run`; an item is `{numbing_minutes, doctor_minutes, staff_minutes, room_id, start, name?, client?, doctor_id?, treatment_id?}`, or a template `{treatment_id, start, client?}`), `POST /api/clear-day` (`location_id`, `date`, optional `dry_run`).

## Not modelled yet

Travel time between locations, someone being at two locations in one day, breaks as a default,
recurring weekly roster templates (use the roster's "many days" form), client records,
doctor-only vs doctor-and-staff-for-part-of-the-treatment, auth / multi-user editing.
