"""A tiny ``.env`` reader (no dependencies) so settings such as ``CLINIC_PASSWORD`` can live in a
file that git and Vercel ignore instead of on the command line.

Format: one ``KEY=value`` per line; blank lines and ``# comments`` are ignored; an optional
``export `` prefix is allowed; values may be wrapped in single quotes (taken literally) or
double quotes (``\\n``, ``\\"`` and ``\\\\`` are understood); an unquoted value ends at `` #``.

Only ``CLINIC_*`` settings are read, and a variable that is already set in the real environment
is never overridden.  Values are never printed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PREFIX = "CLINIC_"
ROOT = Path(__file__).resolve().parent.parent
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a dict (later lines win)."""
    out: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            continue
        value = value.strip()
        if value[:1] == '"':
            body, closed = _read_double_quoted(value[1:])
            value = body if closed else value
        elif value[:1] == "'":
            end = value.find("'", 1)
            value = value[1:end] if end != -1 else value
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def _read_double_quoted(rest: str) -> tuple[str, bool]:
    chars, i = [], 0
    while i < len(rest):
        c = rest[i]
        if c == "\\" and i + 1 < len(rest) and rest[i + 1] in _ESCAPES:
            chars.append(_ESCAPES[rest[i + 1]])
            i += 2
            continue
        if c == '"':
            return "".join(chars), True
        chars.append(c)
        i += 1
    return "".join(chars), False


def find_dotenv(path: str | os.PathLike | None = None) -> Path | None:
    """The file to read: ``path`` if given, else ``.env`` in the current directory, else in the project root."""
    if path:
        return Path(path) if Path(path).is_file() else None
    for candidate in (Path.cwd() / ".env", ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: str | os.PathLike | None = None, environ=None) -> tuple[Path | None, list[str]]:
    """Copy ``CLINIC_*`` settings from the ``.env`` file into the environment.

    Existing environment variables win, and empty values are skipped (so ``CLINIC_PASSWORD=`` in
    the example file means "not set").  Returns ``(file used or None, names that were set)``.
    """
    environ = os.environ if environ is None else environ
    file = find_dotenv(path)
    if file is None:
        return None, []
    applied = []
    for key, value in parse_dotenv(file.read_text(encoding="utf-8")).items():
        if key.startswith(PREFIX) and value != "" and key not in environ:
            environ[key] = value
            applied.append(key)
    return file, applied
