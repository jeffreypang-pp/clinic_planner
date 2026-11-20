"""Tiny stdlib HTTP server: JSON API + the single-page UI.

    python -m clinic.server [--data clinic.json] [--port 8000]

Every change is persisted to the data file immediately, so the saved file is
always the whole clinic (setup + shifts + bookings).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .model import Clinic, ClinicError, NotFound, WEEKDAYS, format_slot, parse_date, parse_time
from .sample import build_sample

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        if os.path.exists(path):
            self.clinic = Clinic.load(path)
        else:
            self.clinic = build_sample()
            self.clinic.save(path)

    def persist(self) -> None:
        self.clinic.save(self.path)

    # ---- handlers (each returns JSON-able data) --------------------------
    def get_clinic(self, q, body):
        return {"clinic": self.clinic.to_dict(), "issues": self.clinic.issues(), "weekdays": WEEKDAYS}

    def put_clinic(self, q, body):
        self.clinic = Clinic.from_dict(body)  # validates before replacing anything
        self.persist()
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
                             body.get("staff_ids"))
        self.persist()
        return {"booking": self.clinic.describe_booking(b)}

    def delete_booking(self, q, body, bid):
        self.clinic.cancel_booking(bid)
        self.persist()
        return {"ok": True}

    def post_shifts(self, q, body):
        """Add one shift per date in ``dates`` (all-or-nothing)."""
        dates = body.get("dates") or [_need(body, "date")]
        start, end = parse_time(_need(body, "start")), parse_time(_need(body, "end"))
        added = []
        try:
            for d in dates:
                added.append(self.clinic.add_shift(_need(body, "role"), _need(body, "person_id"),
                                                   _need(body, "location_id"), d, start, end))
        except ClinicError:
            for s in added:
                self.clinic.remove_shift(s.id)
            raise
        self.persist()
        return {"added": len(added)}

    def get_shifts(self, q, body):
        day = parse_date(_arg(q, "date"))
        c = self.clinic
        return {"shifts": [{
            "id": s.id, "role": s.role, "person_id": s.person_id,
            "person": c._people(s.role)[s.person_id].name,
            "location_id": s.location_id, "location": c.locations[s.location_id].name,
            "start": format_slot(s.start), "end": format_slot(s.end),
        } for s in sorted(c.shifts.values(), key=lambda s: (s.role, s.person_id, s.start)) if s.date == day]}

    def delete_shift(self, q, body, sid):
        self.clinic.remove_shift(sid)
        self.persist()
        return {"ok": True, "issues": self.clinic.issues()}

    def put_availability(self, q, body):
        """Paint a time range available/unavailable for a doctor, staff member or room.

        Body: kind (doctor|staff|room), id, date, start, end, available (bool),
        location_id (doctor/staff only), repeat_until (optional date: repeat every 7 days).
        """
        kind, ident = _need(body, "kind"), _need(body, "id")
        if kind not in ("doctor", "staff", "room"):
            raise ClinicError(f"Unknown kind {kind!r}; expected doctor, staff or room")
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
            if kind == "room":
                self.clinic.set_room_availability(ident, d, start, end, body["available"])
            else:
                self.clinic.set_person_availability(kind, ident, _need(body, "location_id"), d,
                                                    start, end, body["available"])
        self.persist()
        return {"days": len(dates), "issues": self.clinic.issues()}

    def put_hours(self, q, body, lid):
        hours = {}
        for wd, span in _need(body, "hours").items():
            if wd not in WEEKDAYS:
                raise ClinicError(f"Unknown weekday {wd!r}")
            hours[WEEKDAYS.index(wd)] = None if span is None else (parse_time(span[0]), parse_time(span[1]))
        self.clinic.set_opening_hours(lid, hours)
        self.persist()
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
    ("PUT", r"/api/clinic", "put_clinic"),
    ("GET", r"/api/timetable", "get_timetable"),
    ("GET", r"/api/availability", "get_availability"),
    ("GET", r"/api/slots", "get_slots"),
    ("GET", r"/api/bookings", "get_bookings"),
    ("POST", r"/api/bookings", "post_booking"),
    ("DELETE", r"/api/bookings/([\w-]+)", "delete_booking"),
    ("GET", r"/api/shifts", "get_shifts"),
    ("POST", r"/api/shifts", "post_shifts"),
    ("DELETE", r"/api/shifts/([\w-]+)", "delete_shift"),
    ("PUT", r"/api/availability", "put_availability"),
    ("PUT", r"/api/locations/([\w-]+)/hours", "put_hours"),
]


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            for m, pattern, name in ROUTES:
                match = re.fullmatch(pattern, url.path)
                if m == method and match:
                    break
            else:
                return self._send(404, {"error": f"No route {method} {url.path}"})
            try:
                body = None
                if method in ("POST", "PUT"):
                    raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    try:
                        body = json.loads(raw or b"{}")
                    except json.JSONDecodeError as e:
                        raise ClinicError(f"Invalid JSON: {e}") from None
                with app.lock:
                    result = getattr(app, name)(parse_qs(url.query), body, *match.groups())
                self._send(200, result)
            except NotFound as e:
                self._send(404, {"error": str(e)})
            except ClinicError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # pragma: no cover - surfaced to the UI for a prototype
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")
        def do_DELETE(self): self._dispatch("DELETE")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="clinic.json", help="where the clinic is saved")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    app = App(args.data)
    print(f"Clinic planner on http://{args.host}:{args.port}  (data: {args.data})")
    ThreadingHTTPServer((args.host, args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
