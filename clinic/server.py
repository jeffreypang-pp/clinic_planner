"""Tiny stdlib HTTP server: JSON API + the single-page UI.

    python -m clinic.server [--data clinic_data] [--port 8000]
    python -m clinic.server --stateless      # same API as the Vercel deployment (see clinic/stateless.py)

Nothing is written to disk until you explicitly save (POST /api/save, behind a confirmation in
the UI).  Changes live in memory; the three JSON files (resources.json, planning.json,
schedule.json) are only rewritten when saved, and only the ones that changed.  If a file was
changed on disk behind our back (e.g. git pull) saving it is refused unless forced.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import Auth, denied, handle_auth_route, mode_info
from .env import load_dotenv
from .lists import render_list_file, safe_filename
from .model import Clinic, ClinicError, Conflict, GROUP_FILES, GROUPS, NotFound, WEEKDAYS, parse_date, parse_time
from .sample import build_sample

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "public"   # the page; Vercel serves this folder as static files too


class App:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.reload()

    def reload(self) -> None:
        """(Re)load from disk, dropping any unsaved changes."""
        # raw = exact file text as of this load / last save (to spot changes made outside the app)
        # canon = canonical rendering of what we loaded (so a hand-formatted file isn't "unsaved")
        self.raw = Clinic.read_files(self.path)
        if Clinic.dir_has_data(self.path):
            self.clinic = Clinic.load_dir(self.path)
            self.canon = self.clinic.render_files()
        else:  # nothing saved yet: start from the sample, all of it unsaved until you save
            self.clinic = build_sample()
            self.canon = dict(self.raw)

    def unsaved(self) -> list[dict]:
        return self.clinic.pending_changes(self.canon)

    def external(self) -> list[str]:
        """Files that changed on disk since we loaded/saved them (e.g. after a git pull)."""
        disk = Clinic.read_files(self.path)
        return [n for n in disk if disk[n] != self.raw[n]]

    def get_status(self, q, body):
        return {"unsaved": self.unsaved(), "external": self.external()}

    # ---- handlers (each returns JSON-able data) --------------------------
    def get_clinic(self, q, body):
        return {"clinic": self.clinic.to_dict(), "groups": self.clinic.to_groups(),
                "issues": self.clinic.issues(), "weekdays": WEEKDAYS,
                "unsaved": self.unsaved(), "external": self.external(), "data_dir": self.path}

    def post_save(self, q, body):
        """Write the unsaved files to disk. Refuses (409) if one of them changed on disk since
        it was loaded, unless ``{"force": true}``.  Files we didn't edit are never touched."""
        names = {u["file"] for u in self.unsaved()}
        disk = Clinic.read_files(self.path)
        conflicts = sorted(n for n in names if disk[n] != self.raw[n])
        if conflicts and not (body or {}).get("force"):
            raise Conflict(f"{', '.join(conflicts)} changed on disk since it was loaded "
                           "(for example by a git pull)")
        written = self.clinic.save_dir(self.path, only=names)
        self.raw = Clinic.read_files(self.path)
        self.canon = self.clinic.render_files()
        return {"saved": written, **self.get_clinic(q, None)}

    def post_discard(self, q, body):
        """Throw away unsaved changes and reload everything from disk."""
        self.reload()
        return self.get_clinic(q, None)

    # ---- save a copy to / load from a directory the user names (local server only) ------
    @staticmethod
    def _directory(raw) -> Path:
        """The directory the user typed: ``~`` expanded, relative paths taken from where the server
        was started, shown back in full so the confirmation says exactly where."""
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise ClinicError("Enter a directory")
        path = Path(os.path.expanduser(raw.strip())).resolve()
        if path.exists() and not path.is_dir():
            raise ClinicError(f"{path} is a file, not a directory")
        return path

    @staticmethod
    def _counts(clinic: Clinic) -> dict:
        return {"locations": len(clinic.locations), "rooms": len(clinic.rooms), "doctors": len(clinic.doctors),
                "staff": len(clinic.staff), "treatments": len(clinic.treatments), "bookings": len(clinic.bookings)}

    # ---- the in-page folder chooser (local server only) ----------------------------------
    MAX_BROWSE_ENTRIES = 500

    def get_browse(self, q, body):
        """List the sub-folders of a directory so the page can offer a folder chooser.

        ``path`` (default: home); if it doesn't exist we show the nearest folder that does and say
        so.  Directories are listed (hidden ones with ``hidden=1``); each is flagged when it already
        holds clinic data files.  With ``files=1`` the ``.json`` files in the folder are listed too
        (name, size, and whether it is a treatment-list file), for the Save-as / Open dialogs; no
        other kind of file is ever listed.  Local server only: the server is on the user's computer."""
        raw = (q.get("path", [""])[0] or "").strip()
        if "\0" in raw:
            raise ClinicError("Bad path")
        show_hidden = q.get("hidden", [""])[0] == "1"
        requested = Path(os.path.expanduser(raw)).resolve() if raw else None
        here = requested or Path.home().resolve()
        while not here.exists() and here != here.parent:
            here = here.parent
        if here.is_file():
            here = here.parent
        want_files = q.get("files", [""])[0] == "1"
        try:
            with os.scandir(here) as it:
                found = [e for e in it if show_hidden or not e.name.startswith(".")]
        except OSError as e:
            raise ClinicError(f"Can't read {here}: {e.strerror or e}") from None
        subdirs = sorted((e for e in found if self._is_dir(e)), key=lambda e: e.name.lower())
        json_files = sorted((e for e in found if want_files and e.name.lower().endswith(".json") and self._is_file(e)),
                            key=lambda e: e.name.lower())
        names = list(GROUP_FILES.values())
        entries = [{"name": e.name, "path": str(Path(e.path)),
                    "clinic": all(os.path.isfile(os.path.join(e.path, n)) for n in names)}
                   for e in subdirs[:self.MAX_BROWSE_ENTRIES]]
        files = [{"name": e.name, "path": str(Path(e.path)), "size": self._size(e),
                  "list": self._is_list_file(e.path)} for e in json_files[:self.MAX_BROWSE_ENTRIES]]
        shortcuts = [{"label": "Home", "path": str(Path.home())},
                     {"label": "Data folder", "path": str(Path(self.path).resolve())},
                     {"label": "Working directory", "path": str(Path.cwd())}]
        if os.name == "nt":      # Windows: also offer the drives
            shortcuts += [{"label": f"{c}:", "path": f"{c}:\\"} for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                          if os.path.exists(f"{c}:\\")]
        return {"current": {"path": str(here), "parent": str(here.parent) if here.parent != here else None,
                            "files": [n for n in names if (here / n).is_file()],
                            "writable": os.access(here, os.W_OK)},
                "requested_missing": str(requested) if requested is not None and requested != here else None,
                "entries": entries, "files": files,
                "truncated": len(subdirs) > self.MAX_BROWSE_ENTRIES or len(json_files) > self.MAX_BROWSE_ENTRIES,
                "shortcuts": shortcuts, "separator": os.sep}

    @staticmethod
    def _is_dir(entry) -> bool:
        try:
            return entry.is_dir()
        except OSError:
            return False

    @staticmethod
    def _is_file(entry) -> bool:
        try:
            return entry.is_file()
        except OSError:
            return False

    @staticmethod
    def _size(entry) -> int:
        try:
            return entry.stat().st_size
        except OSError:
            return 0

    MAX_LIST_FILE_BYTES = 2_000_000

    @classmethod
    def _is_list_file(cls, path: str) -> bool:
        """Cheap peek: does this ``.json`` file start like a treatment-list file?  (The kind marker is
        written near the top; nothing is parsed or returned.)"""
        try:
            if os.path.getsize(path) > cls.MAX_LIST_FILE_BYTES:
                return False
            with open(path, "rb") as f:
                return b'"kind": "clinic-treatment-list"' in f.read(2048)
        except OSError:
            return False

    def post_mkdir(self, q, body):
        """Create a new folder ``name`` inside the existing directory ``path`` (for the chooser)."""
        parent = self._directory(_need(body, "path"))
        if not parent.is_dir():
            raise ClinicError(f"{parent} doesn't exist")
        name = _need(body, "name")
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 100
                or name.strip() in (".", "..") or any(c in name for c in '/\\\0')):
            raise ClinicError("Give the new folder a plain name (no slashes), at most 100 characters")
        target = parent / name.strip()
        if target.exists():
            raise ClinicError(f"{target} already exists")
        try:
            target.mkdir()
        except OSError as e:
            raise ClinicError(f"Can't create {target}: {e.strerror or e}") from None
        return {"path": str(target.resolve())}

    def get_files(self, q, body):
        """The exact text of the three data files for the current working data (nothing is written)."""
        return {"files": self.clinic.render_files(), "counts": self._counts(self.clinic)}

    def post_export(self, q, body):
        """Save a *copy* of the clinic data (resources.json, planning.json, schedule.json) into any
        directory.  The data folder and the unsaved-changes state are left alone.  ``dry_run`` only
        reports what would be written (and which files would be overwritten)."""
        target = self._directory(_need(body, "directory"))
        texts = self.clinic.render_files()
        existing = Clinic.read_files(str(target)) if target.is_dir() else {n: None for n in texts}
        info = {"directory": str(target), "exists": target.is_dir(),
                "files": [{"name": n, "exists": existing[n] is not None, "identical": existing[n] == texts[n]}
                          for n in texts]}
        if (body or {}).get("dry_run"):
            return info
        return {**info, "written": self.clinic.save_dir(str(target))}

    def post_import(self, q, body):
        """Load the clinic data from a directory, replacing the working data.  Like any other edit
        it only changes memory: differences from the data folder show up as unsaved changes.
        All three files must be there and valid.  ``dry_run`` validates and summarises only."""
        target = self._directory(_need(body, "directory"))
        if not target.is_dir():
            raise ClinicError(f"{target} doesn't exist")
        missing = [n for n, text in Clinic.read_files(str(target)).items() if text is None]
        if missing:
            raise ClinicError(f"{target} is missing {', '.join(missing)} (a clinic folder holds "
                              "resources.json, planning.json and schedule.json)")
        try:
            clinic = Clinic.load_dir(str(target))
        except ClinicError as e:
            raise ClinicError(f"{target}: {e}") from None
        info = {"directory": str(target), "summary": self._counts(clinic), "current": self._counts(self.clinic)}
        if (body or {}).get("dry_run"):
            return info
        self.clinic = clinic
        return {**info, **self.get_clinic(q, None)}

    def put_clinic(self, q, body):
        """Replace one or more data groups, e.g. ``{"planning": {...}}``; the rest is kept.
        Everything is validated together before anything is replaced or saved."""
        if not isinstance(body, dict) or not body:
            raise ClinicError(f"Send an object with one or more of {list(GROUPS)}")
        self.clinic = Clinic.from_groups({**self.clinic.to_groups(), **body})
        return self.get_clinic(q, None)

    def get_timetable(self, q, body):
        return self.clinic.timetable(_arg(q, "location"), _arg(q, "date"))

    def get_availability(self, q, body):
        tid, day, start = _arg(q, "treatment"), _arg(q, "date"), parse_time(_arg(q, "start"))
        loc = q.get("location", [""])[0]
        locs = [loc] if loc else list(self.clinic.locations)
        return {"results": [self.clinic.check_availability(tid, l, day, start) | {
            "location": self.clinic.locations[l].name} for l in locs]}

    def get_slots(self, q, body):
        return {"slots": self.clinic.find_slots(_arg(q, "treatment"), _arg(q, "date"),
                                                q.get("location", [""])[0] or None)}

    def get_bookings(self, q, body):
        day = parse_date(_arg(q, "date"))
        return {"bookings": [self.clinic.describe_booking(b)
                             for b in sorted(self.clinic._bookings_on(day), key=lambda b: b.start)]}

    def post_booking(self, q, body):
        b = self.clinic.book(_need(body, "treatment_id"), _need(body, "location_id"), _need(body, "date"),
                             parse_time(_need(body, "start")), body.get("client", ""),
                             body.get("doctor_id") or None, body.get("room_id") or None,
                             staff=body.get("staff"))
        return {"booking": self.clinic.describe_booking(b)}

    def post_book_list(self, q, body):
        """Go down a list of ``items`` ({treatment_id, start, client?}) in order, booking each at
        ``location_id`` on ``date``; stops at the first one that can't be carried out.
        ``dry_run`` reports what would happen without booking anything."""
        items = _need(body, "items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ClinicError("'items' must be a list of {treatment_id, start} objects")
        return self.clinic.book_list(_need(body, "location_id"), _need(body, "date"), items,
                                     bool(body.get("dry_run")))

    # ---- lists of treatments (the Timetable tab's "Add treatments" list), saved as files -----------
    def post_list_file(self, q, body):
        """Validate a list and return it as a stand-alone file (``filename`` + ``text``) for the browser
        to download or write where the user chose.  With ``lenient`` it instead validates a list that was
        read from a file for loading: ids that no longer exist are dropped with ``warnings``.
        Touches no files, so it works on every deployment."""
        return render_list_file(self.clinic, body.get("name"), _need(body, "location_id"), body.get("items"),
                                bool((body or {}).get("lenient")))

    def post_list_open(self, q, body):
        """Read a list from a ``.json`` file the user picked (local server only; the browser-side path
        is ``post_list_file``).  Only a real treatment-list file is accepted, and only its (validated,
        lenient) list is returned, never raw content of other files."""
        raw = str(_need(body, "path")).strip()
        if "\0" in raw:
            raise ClinicError("Bad path")
        path = Path(os.path.expanduser(raw)).resolve()
        if not path.is_file():
            raise ClinicError(f"{path} isn't a file")
        if path.suffix.lower() != ".json":
            raise ClinicError(f"{path.name} isn't a .json file")
        if path.stat().st_size > self.MAX_LIST_FILE_BYTES:
            raise ClinicError(f"{path.name} is too large to be a treatment list")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ClinicError(f"{path.name} isn't valid JSON: {e}") from None
        if not isinstance(data, dict) or data.get("kind") != "clinic-treatment-list" or not isinstance(data.get("items"), list):
            raise ClinicError(f"{path.name} isn't a treatment list file (the kind made by \"Save to file…\")")
        r = render_list_file(self.clinic, data.get("name") or path.stem, data.get("location_id"), data["items"], True)
        return {**{k: r[k] for k in ("name", "location_id", "items", "warnings")},
                "path": str(path), "filename": path.name, "directory": str(path.parent)}

    def post_list_export(self, q, body):
        """Write one list as ``<directory>/<name>.json`` (local server only; the browser-side path is
        ``post_list_file``).  Refuses (409) to replace an existing file unless ``overwrite``;
        ``dry_run`` only reports the full path and whether it exists."""
        rendered = render_list_file(self.clinic, body.get("name"), _need(body, "location_id"), body.get("items"))
        directory = self._directory(_need(body, "directory"))
        filename = safe_filename(body.get("filename") or rendered["name"])
        path = directory / filename
        if path.is_dir():
            raise ClinicError(f"{path} is a folder")
        info = {"path": str(path), "directory": str(directory), "filename": filename,
                "exists": path.exists(), "directory_exists": directory.is_dir()}
        if (body or {}).get("dry_run"):
            return info
        if path.exists() and not body.get("overwrite"):
            raise Conflict(f"{path} already exists")
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(rendered["text"])
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return {**info, "written": True}

    def post_clear_day(self, q, body):
        """Remove all bookings and blocks for a location on a date (``dry_run`` previews)."""
        return self.clinic.clear_day(_need(body, "location_id"), _need(body, "date"),
                                     bool(body.get("dry_run")))

    def put_booking_staff(self, q, body, bid):
        """Name (or clear) who does which step of an existing booking: ``{"staff": {step: id | null}}``."""
        b = self.clinic.set_booking_staff(bid, _need(body, "staff"))
        return {"booking": self.clinic.describe_booking(b), "issues": self.clinic.issues()}

    def delete_booking(self, q, body, bid):
        self.clinic.cancel_booking(bid)
        return {"ok": True}

    def put_presence(self, q, body):
        """Tick / untick doctors and staff: ``location_id`` null = not in any location.
        ``dates`` (list) or ``date`` (one day). One location per person per day."""
        role, pid = _need(body, "role"), _need(body, "person_id")
        loc = body.get("location_id") or None
        dates = [parse_date(d) for d in (body.get("dates") or [_need(body, "date")])]
        for d in dates:
            self.clinic.set_presence(role, pid, d, loc)
        return {"days": len(dates), "issues": self.clinic.issues()}

    def get_roster(self, q, body):
        return self.clinic.roster(_arg(q, "start"), int(q.get("days", ["7"])[0]))

    def put_availability(self, q, body):
        """Block/unblock a time range for a doctor, staff member or room.

        Body: kind (doctor|staff|room), id, date, start, end, available (bool; false = block),
        repeat_until (optional date: repeat every 7 days).
        """
        kind, ident = _need(body, "kind"), _need(body, "id")
        if not isinstance(body.get("available"), bool):
            raise ClinicError("Field 'available' must be true or false")
        day = parse_date(_need(body, "date"))
        start, end = parse_time(_need(body, "start")), parse_time(_need(body, "end"))
        until = parse_date(body["repeat_until"]) if body.get("repeat_until") else day
        if until < day:
            raise ClinicError("repeat_until is before date")
        if (until - day).days > 730:
            raise ClinicError("Can repeat for at most 2 years")
        dates = [day + timedelta(days=7 * i) for i in range((until - day).days // 7 + 1)]
        for d in dates:
            self.clinic.set_blocked(kind, ident, d, start, end, not body["available"])
        return {"days": len(dates), "issues": self.clinic.issues()}

    def put_hours(self, q, body, lid):
        hours = {}
        for wd, span in _need(body, "hours").items():
            if wd not in WEEKDAYS:
                raise ClinicError(f"Unknown weekday {wd!r}")
            hours[WEEKDAYS.index(wd)] = None if span is None else (parse_time(span[0]), parse_time(span[1]))
        self.clinic.set_opening_hours(lid, hours)
        return self.get_clinic(q, None)


def _arg(q, key):
    v = q.get(key, [""])[0]
    if not v:
        raise ClinicError(f"Missing query parameter {key!r}")
    return v


def _need(body, key):
    if not isinstance(body, dict) or body.get(key) in (None, ""):
        raise ClinicError(f"Missing field {key!r}")
    return body[key]


ROUTES = [
    ("GET", r"/api/clinic", "get_clinic"),
    ("GET", r"/api/status", "get_status"),
    ("PUT", r"/api/clinic", "put_clinic"),
    ("POST", r"/api/save", "post_save"),
    ("GET", r"/api/files", "get_files"),
    ("GET", r"/api/browse", "get_browse"),
    ("POST", r"/api/mkdir", "post_mkdir"),
    ("POST", r"/api/export", "post_export"),
    ("POST", r"/api/import", "post_import"),
    ("POST", r"/api/discard", "post_discard"),
    ("GET", r"/api/timetable", "get_timetable"),
    ("GET", r"/api/availability", "get_availability"),
    ("GET", r"/api/slots", "get_slots"),
    ("GET", r"/api/bookings", "get_bookings"),
    ("POST", r"/api/bookings", "post_booking"),
    ("POST", r"/api/book-list", "post_book_list"),
    ("POST", r"/api/list-file", "post_list_file"),
    ("POST", r"/api/list-export", "post_list_export"),
    ("POST", r"/api/list-open", "post_list_open"),
    ("POST", r"/api/clear-day", "post_clear_day"),
    ("DELETE", r"/api/bookings/([\w-]+)", "delete_booking"),
    ("PUT", r"/api/bookings/([\w-]+)/staff", "put_booking_staff"),
    ("PUT", r"/api/presence", "put_presence"),
    ("GET", r"/api/roster", "get_roster"),
    ("PUT", r"/api/availability", "put_availability"),
    ("PUT", r"/api/locations/([\w-]+)/hours", "put_hours"),
]


def dispatch(app: "App", method: str, path: str, query: dict, body) -> tuple[int, dict]:
    """Run one API request against ``app``; returns ``(http_status, json_payload)``.
    Shared by the local server and the stateless (Vercel) functions."""
    for m, pattern, name in ROUTES:
        match = re.fullmatch(pattern, path)
        if m == method and match:
            break
    else:
        return 404, {"error": f"No route {method} {path}"}
    try:
        with app.lock:
            return 200, getattr(app, name)(query, body, *match.groups())
    except NotFound as e:
        return 404, {"error": str(e)}
    except Conflict as e:
        return 409, {"error": str(e)}
    except ClinicError as e:
        return 400, {"error": str(e)}
    except Exception as e:  # pragma: no cover - surfaced to the UI for a prototype
        return 500, {"error": f"{type(e).__name__}: {e}"}


def make_handler(app: App | None, stateless_dir: str | None = None, auth: Auth | None = None):
    """HTTP handler for the local server.  With ``stateless_dir`` it serves the stateless API
    (/api/mode, /api/initial, /api/call) instead of the stateful one, exactly like Vercel.
    With an enabled ``auth`` every API call needs the password login (see ``clinic.auth``)."""
    auth = auth or Auth()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status, payload, ctype="application/json", headers=()):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            raw = b""
            if method in ("POST", "PUT"):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ip = self.client_address[0]
            if stateless_dir is not None:
                from .stateless import route
                status, payload, extra = route(method, url.path, raw, stateless_dir, self.headers, auth, ip)
                return self._send(status, payload, headers=extra)
            response = handle_auth_route(auth, method, url.path, raw, self.headers, ip)
            if response is None and method == "GET" and url.path == "/api/mode":
                response = 200, {"mode": "server", **mode_info(auth, self.headers)}, []
            if response is None:
                response = denied(auth, self.headers)
            if response:
                return self._send(response[0], response[1], headers=response[2])
            body = None
            if method in ("POST", "PUT"):
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError as e:
                    return self._send(400, {"error": f"Invalid JSON: {e}"})
            self._send(*dispatch(app, method, url.path, parse_qs(url.query), body))

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")
        def do_DELETE(self): self._dispatch("DELETE")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="clinic_data", help="directory holding the 3 data files")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--env-file", default=None,
                    help="read CLINIC_* settings (e.g. CLINIC_PASSWORD) from this file instead of ./.env")
    ap.add_argument("--stateless", action="store_true",
                    help="serve the stateless API used on Vercel: the browser keeps the working data, "
                         "the server never writes files (--data is then read-only)")
    args = ap.parse_args()
    env_file, applied = load_dotenv(args.env_file)
    if args.env_file and env_file is None:
        ap.error(f"--env-file {args.env_file}: no such file")
    if env_file:
        print(f"Settings from {env_file}: {', '.join(applied) if applied else 'nothing new'}")
    auth = Auth.from_env()
    print("Password protection:", "ON (CLINIC_PASSWORD)" if auth.enabled else
          "OFF (put CLINIC_PASSWORD=... in .env to require a login)")
    if args.stateless:
        print(f"Clinic planner (stateless, like Vercel) on http://{args.host}:{args.port}  (read-only data: {args.data})")
        handler = make_handler(None, stateless_dir=args.data, auth=auth)
    else:
        print(f"Clinic planner on http://{args.host}:{args.port}  (data: {args.data})")
        handler = make_handler(App(args.data), auth=auth)
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
