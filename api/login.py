"""Vercel serverless function: POST /api/login  (see clinic/auth.py, clinic/stateless.py)."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clinic.stateless import respond  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        respond(self, "POST", "login")

    def log_message(self, fmt, *args):  # keep the function logs quiet
        pass
