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

## Password protection

One shared password protects the app (`clinic/auth.py`). Keep it in a **`.env` file** in the project folder, which git and
Vercel both ignore, so it is never committed:

```bash
cp .env.example .env        # then edit .env and set CLINIC_PASSWORD=...
python -m clinic.server     # reads .env at startup and prints which settings it used (never their values)
```

`.env` is read from the current directory, else the project root (or pass `--env-file path`). One `KEY=value` per line;
`#` comments, `export KEY=...`, and `'single'` / `"double"` quotes are fine (quote a password containing `#` or spaces).
Only `CLINIC_*` settings are read, an empty value counts as "not set", and a variable already set in the real environment
wins over the file. Other settings work the same way: `CLINIC_SESSION_HOURS`, `CLINIC_SECRET`, `CLINIC_REQUIRE_PASSWORD`.

**On Vercel the `.env` file is not deployed** (`.vercelignore`): set `CLINIC_PASSWORD` under Project Settings →
Environment Variables and redeploy.

* **Every API call needs a login**, so no clinic data is served without one. The page itself contains no data; it just shows
  a login screen first (and requests nothing until you are in).
* A successful login sets a **signed, `HttpOnly`, `SameSite=Strict` cookie** (`Secure` over HTTPS) valid for 12 hours
  (`CLINIC_SESSION_HOURS` to change). **Log out** is in the page header. Changing the password logs everyone out.
  If the session ends mid-use the login reappears and your work in progress is kept; on Vercel, logging out also clears the
  copy of your data kept in the browser.
* **Wrong guesses are slowed down**, and after 5 a client is locked out for 10 minutes (per IP, per server process:
  best effort on serverless, so add a Vercel Firewall rate-limit rule on `/api/login` if you want a hard limit).
* **Fail closed on Vercel:** if `CLINIC_PASSWORD` isn't set there (Vercel sets `VERCEL`; or set `CLINIC_REQUIRE_PASSWORD=1`
  anywhere), the data endpoints return 503 and the page says the password isn't set up.
* Locally with no `CLINIC_PASSWORD` the app is open, as before.
* It is a single shared password with no user accounts, and the password travels to the server on login, so use HTTPS
  (Vercel always does) and a strong, unique password. For stricter control also turn on Vercel's *Deployment Protection*.

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

**Set the password** (see *Password protection* below): add `CLINIC_PASSWORD` under Project Settings → Environment
Variables, then redeploy. A Vercel deployment **refuses to serve any data until a password is set**, because `clinic_data/`
(staff and doctor names, bookings, ...) would otherwise be readable by anyone with the link.

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
| Staff | Interchangeable: any staff member can assist any treatment. Each step either **names** who does it or is left on **assign later** (the default); an unnamed step only counts as one staff member needed. |
| Presence | Per **day**, a doctor/staff member is at **one location or none** (the tick box). Stored in `schedule.json` as `presence[date][doctors\|staff][id] = location_id`, so "at most one location per day" holds by construction. Once present they are **free all day unless booked or blocked**. |
| Block | A time span on a date when a doctor, staff member or room can't be used (lunch, leave, maintenance). Optional. |
| Booking | A treatment (its three times, the room, a start, optional name/client/template; unnamed = called by its room's name) at a location and date. Holds the room for the whole treatment; the doctor is busy only during the doctor step. Each step has a staff member named or *assign later* (`staff` in `schedule.json`: `{"numbing": null, "doctor": "kai_ping", "staff": null}`, null = assign later); the doctor is assigned automatically unless you pick one. Stored in `schedule.json` with its own `numbing_minutes` / `doctor_minutes` / `staff_minutes`. |

A treatment can start at a slot at a location iff:
the location is open for the whole treatment; the doctor step has a doctor ticked in there (qualified, if a template is used)
who is neither booked nor blocked during it; the chosen room (at that location, with the template's equipment if one is used)
is free and not blocked for the whole treatment; and the two **staff rules** hold:

1. **Total staff:** in every 15-minute block of every step, the staff needed by all the treatments at that location (this one
   included; named or *assign later*, every step needs one) must not exceed the staff available there (ticked in and not blocked
   in that block). The timetable's **Total staff** row, under the staff rows, shows the number needed in each block.
2. **Named staff:** a step that names a staff member needs that person ticked in at the location, not blocked, and not named in
   an overlapping step of another treatment (the same person can do consecutive steps of one treatment).

`Clinic.check_custom()` (three times + room + optional named staff) and `Clinic.check_availability()` (a template, any suitable
room) return the free doctors and rooms, per step how many more staff can be used (and who could be named), or the specific
reasons it can't. Rule 1 only counts people per block; it doesn't check that a step left on *assign later* can later be given
to one person for its whole length (name staff afterwards, from the booked row, to find out).

### Adding treatments one at a time (`Clinic.book_custom`, `Clinic.book_list`)

On the Timetable tab, add treatments to a list one at a time: give the numbing, doctor treatment and staff treatment
minutes and the room, then **Book next** and watch the grid. **Book all** goes down the list in order; each treatment
sees what the ones above it used. The first one that can't be carried out for lack of a doctor, staff member or room
is reported as *cannot be performed* and the list **stops there**: those above it stay booked, those below are
*not attempted*. `dry_run` / **Check all** reports the same outcome without booking anything. The doctor and room are chosen
greedily and never reshuffled afterwards (first free doctor; first suitable room unless you choose one), so a failure means
"no free resource given what's already booked". Staff are only counted, never auto-assigned: name them, or leave *assign later*.

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
  The treatment **name** is optional: leave it empty and the treatment is called by its room's name (shown as the box's placeholder
  and used on the grid and in lists; a template's name is used if you pick a template, and a name you type always wins).
  The **Start** choices only offer times inside the location's opening hours for the selected date (one-off `date_overrides`
  included) at which the treatment still ends by closing time; a new row starts at opening time, and a start you picked that
  no longer fits (longer treatment, another day or location) is kept but flagged "outside opening hours".
  Each row also has a **staff** pick for every step that needs one: *assign later* (default) or a named person (only people ticked in here).
  Under the staff rows, **Total staff** shows how many staff the treatments need in each 15-minute block (grey = none,
  amber = every available staff member in use, red = a shortage). Name or change who does a step afterwards with the dropdown on a
  booked row (or in **Roster & bookings**). A staff member's own row only shows the steps that name them.
  **Tick box** on each doctor/staff row: ticked = in this location that day; unticking = not in any location;
  ticking someone who is at another location moves them here.
  **Edit availability**: drag across a row (doctor, staff or room), then *Block* / *Unblock*; optionally repeat weekly.
  **Save to directory…**: choose any folder (the same chooser as in Setup & save) and save a copy of the clinic data there, bookings included,
  after the usual confirmation listing which files are new / identical / overwritten. It copies the current working data (unsaved changes
  included) and leaves the data folder and the unsaved-changes state alone; the chooser reopens in the folder you picked last. On the Vercel
  deployment it uses the browser's folder picker.
  **Clear day…**: cancels every booking and removes every blocked time for this location on this date
  (rooms here, plus doctors/staff ticked in here), after showing what will go and asking to confirm.
  Tick boxes are left alone, and like all edits it only changes memory until you Save…
* **Roster & bookings** - a weekly grid to set each person's location per day (same data as the tick box),
  a form to set a location across a date range / weekdays, and the day's bookings (cancel here).
* **Setup & save** - **save a copy to / load from a directory you choose**, edit opening hours per weekday, and edit each of the three
  data files as JSON (add rooms, treatments, doctors, skills...), validate and apply (then Save… in the top bar); download/upload a file.

  **Choose the directory** by typing a path or pressing **Browse…**, which opens a folder chooser: click through your folders
  (Up, Home, the data folder, the working directory; Windows drives too), type a path and press Enter, show hidden folders,
  create a **New folder…**, or pick a path that doesn't exist yet (it is created when you save). Folders already holding clinic files are
  tagged, and the dialog says which clinic files the current folder contains. Only folders are listed, never files. The chooser needs
  the server to list folders, so it is local-server only (on the Vercel deployment the browser's own folder picker is used instead).

  *Directory save/load* covers `resources.json`, `planning.json` and `schedule.json` (not the saved treatment lists, which have their
  own Save list / Load). **Save** writes a *copy*: type any directory (`~` and relative paths work; it is created if needed), you are
  shown exactly which files are new, identical (left alone) or will be overwritten, and the data folder and your unsaved changes are
  untouched. **Load** replaces the working data with the three files in a directory (all three must be present and valid; nothing is
  changed if they aren't); like any edit it stays in memory, shown as unsaved changes, until you Save… in the top bar, and Discard goes
  back. Both ask for confirmation first. On the **Vercel deployment** the server can't see your computer (and must not read or write
  its own paths), so these buttons use your browser's folder picker instead: Chrome/Edge write straight into the folder you pick;
  other browsers download the files and let you choose the folder's files to load.

## API

`GET /api/clinic` (merged view + the 3 groups + issues + unsaved/external files), `GET /api/status`, `GET /api/files` (the exact text of the three files),
`POST /api/export` / `POST /api/import` (`directory`, optional `dry_run`; local server only: save a copy to / load from a directory),
`GET /api/browse?path=[&hidden=1]` / `POST /api/mkdir` (`path`, `name`) (local server only: the folder chooser's listing and New folder),
`POST /api/save` (`{"force": true}` to overwrite files changed on disk), `POST /api/discard`, `PUT /api/clinic` (`{"resources": {...}}` etc.;
replaces the given groups in memory, validated together with the rest first),
`GET /api/timetable?location=&date=`, `GET /api/roster?start=&days=`,
`PUT /api/presence` (`role`, `person_id`, `location_id` or null, `date`/`dates`),
`PUT /api/bookings/<id>/staff` (`{"staff": {step: staff_id | null}}`: name or clear who does a step),
`PUT /api/availability` (block/unblock a doctor/staff/room range, optional weekly repeat),
`PUT /api/locations/<id>/hours`,
`GET /api/availability?treatment=&date=&start=HH:MM[&location=]`, `GET /api/slots?treatment=&date=[&location=]` (look-ups; the page no longer has a tab for them),
`GET|PUT|DELETE /api/lists` (saved treatment lists; `PUT` takes `name`, `location_id`, `items`, `overwrite`; `DELETE ?name=`),
`GET|POST /api/bookings`, `DELETE /api/bookings/<id>`, `POST /api/book-list` (`location_id`, `date`, `items`, optional `dry_run`; an item is `{numbing_minutes, doctor_minutes, staff_minutes, room_id, start, name?, client?, doctor_id?, staff?: {step: id | null}, treatment_id?}`, or a template `{treatment_id, start, client?}`), `POST /api/clear-day` (`location_id`, `date`, optional `dry_run`).

## Not modelled yet

Travel time between locations, someone being at two locations in one day, breaks as a default,
recurring weekly roster templates (use the roster's "many days" form), client records,
doctor-only vs doctor-and-staff-for-part-of-the-treatment, auth / multi-user editing.
