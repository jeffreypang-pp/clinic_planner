"""The .env reader, and the guarantees that make keeping a password in it safe."""
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from clinic.auth import Auth
from clinic.env import find_dotenv, load_dotenv, parse_dotenv
from clinic.server import App, make_handler

REPO = Path(__file__).resolve().parent.parent


class ParseTests(unittest.TestCase):
    def test_basic_lines_comments_and_export(self):
        text = """# a comment
CLINIC_PASSWORD=hunter2
  CLINIC_A =  spaced out   
export CLINIC_B=exported
export\tCLINIC_C=tabbed

not a setting
=novalue
1BAD=x
CLINIC_EMPTY=
"""
        self.assertEqual(parse_dotenv(text), {"CLINIC_PASSWORD": "hunter2", "CLINIC_A": "spaced out",
                                              "CLINIC_B": "exported", "CLINIC_C": "tabbed", "CLINIC_EMPTY": ""})

    def test_quotes_and_inline_comments(self):
        text = '''CLINIC_P1="pass word # not a comment"
CLINIC_P2='single $quoted \\n literal'
CLINIC_P3="esc \\" quote and \\\\ backslash\\nnewline"
CLINIC_P4=plain value # trailing comment
CLINIC_P5=has#hash
CLINIC_P6="unterminated
CLINIC_P7=a=b=c
'''
        got = parse_dotenv(text)
        self.assertEqual(got["CLINIC_P1"], "pass word # not a comment")
        self.assertEqual(got["CLINIC_P2"], "single $quoted \\n literal")        # single quotes are literal
        self.assertEqual(got["CLINIC_P3"], 'esc " quote and \\ backslash\nnewline')
        self.assertEqual(got["CLINIC_P4"], "plain value")
        self.assertEqual(got["CLINIC_P5"], "has#hash")                          # only " #" starts a comment
        self.assertEqual(got["CLINIC_P6"], '"unterminated')
        self.assertEqual(got["CLINIC_P7"], "a=b=c")

    def test_bom_crlf_and_later_lines_win(self):
        self.assertEqual(parse_dotenv("﻿CLINIC_X=1\r\nCLINIC_X=2\r\n"), {"CLINIC_X": "2"})
        self.assertEqual(parse_dotenv(""), {})


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_sets_only_clinic_settings_and_never_overrides_the_real_environment(self):
        self.file.write_text("CLINIC_PASSWORD=from-file\nCLINIC_SESSION_HOURS=3\nPATH=/evil\nHOME=/evil\nCLINIC_BLANK=\n")
        env = {"CLINIC_SESSION_HOURS": "99", "PATH": "/usr/bin"}
        used, applied = load_dotenv(self.file, env)
        self.assertEqual(used, self.file)
        self.assertEqual(applied, ["CLINIC_PASSWORD"])
        self.assertEqual(env, {"CLINIC_SESSION_HOURS": "99", "PATH": "/usr/bin", "CLINIC_PASSWORD": "from-file"})

    def test_missing_file_is_fine(self):
        env = {}
        self.assertEqual(load_dotenv(self.file, env), (None, []))
        self.assertEqual(env, {})
        self.assertIsNone(find_dotenv(self.file))

    def test_find_prefers_the_current_directory(self):
        old = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            self.file.write_text("CLINIC_PASSWORD=here\n")
            self.assertEqual(find_dotenv().resolve(), self.file.resolve())
        finally:
            os.chdir(old)

    def test_password_from_the_file_turns_protection_on(self):
        self.file.write_text("CLINIC_PASSWORD='s3cret value'\nCLINIC_SESSION_HOURS=2\n")
        env = {}
        load_dotenv(self.file, env)
        auth = Auth.from_env(env)
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.verify_password("s3cret value"))
        self.assertFalse(auth.verify_password("s3cret"))
        self.assertEqual(auth.ttl, 7200)
        # the example file (empty password) leaves the app open, like having no .env at all
        env = {}
        load_dotenv(REPO / ".env.example", env)
        self.assertFalse(Auth.from_env(env).enabled)


class SecretsStayOutTests(unittest.TestCase):
    """A password in .env must not end up in git, in a Vercel deployment, or on the web."""

    def lines(self, name):
        return [l.strip() for l in (REPO / name).read_text().splitlines() if l.strip() and not l.startswith("#")]

    def test_env_files_are_ignored_by_git_and_vercel(self):
        for ignore in (".gitignore", ".vercelignore"):
            rules = self.lines(ignore)
            self.assertIn(".env", rules, ignore)
            self.assertIn(".env.*", rules, ignore)
        self.assertIn("!.env.example", self.lines(".gitignore"))              # the template stays committed

    def test_example_file_contains_no_real_password(self):
        text = (REPO / ".env.example").read_text()
        self.assertIn("CLINIC_PASSWORD=\n", text)

    def test_the_server_never_serves_env_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(os.path.join(tmp, "d"))))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                for path in ("/.env", "/.env.example", "/../.env", "/%2e%2e/.env", "/clinic/env.py", "/vercel.json"):
                    with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                        urllib.request.urlopen(base + path)
                    self.assertEqual(cm.exception.code, 404, path)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_vercel_function_bundle_does_not_pull_in_env_files(self):
        import json
        patterns = json.loads((REPO / "vercel.json").read_text())["functions"]["api/*.py"]["includeFiles"]
        self.assertNotIn(".env", patterns)


if __name__ == "__main__":
    unittest.main()
