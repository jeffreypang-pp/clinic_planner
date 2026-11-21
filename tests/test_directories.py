"""Save a copy to / load from a directory the user names (local server); stateless refuses server paths."""
import json
import os
import tempfile
import unittest
from datetime import date

from clinic.model import Clinic, GROUP_FILES
from clinic.sample import build_sample
from clinic.server import App, dispatch
from clinic.stateless import handle_call, initial_payload

NAMES = sorted(GROUP_FILES.values())


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=14).save_dir(self.data)
        self.app = App(self.data)
        self.snapshot = Clinic.read_files(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return dispatch(self.app, method, path, {}, body)

    def listing(self, directory):
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else None

    # ---- save a copy ----------------------------------------------------------------
    def test_save_a_copy_creates_the_directory_and_writes_only_the_three_files(self):
        target = os.path.join(self.tmp.name, "backups", "monday")
        status, info = self.call("POST", "/api/export", {"directory": target, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual((info["directory"], info["exists"]), (os.path.realpath(target), False))
        self.assertEqual(sorted((f["name"], f["exists"]) for f in info["files"]), [(n, False) for n in NAMES])
        self.assertFalse(os.path.exists(target))                              # a dry run writes nothing
        status, done = self.call("POST", "/api/export", {"directory": target})
        self.assertEqual((status, sorted(done["written"])), (200, NAMES))
        self.assertEqual(self.listing(target), NAMES)                         # nothing else is written
        self.assertEqual(Clinic.read_files(target), self.snapshot)            # same text as the data folder
        self.assertEqual(Clinic.load_dir(target).to_dict(), self.app.clinic.to_dict())

    def test_a_copy_does_not_touch_the_data_folder_or_the_unsaved_state(self):
        self.app.clinic.set_presence("staff", "s1", "2026-11-23", None)       # an unsaved change
        before = self.app.unsaved()
        target = os.path.join(self.tmp.name, "copy")
        self.call("POST", "/api/export", {"directory": target})
        self.assertEqual(Clinic.read_files(self.data), self.snapshot)         # data folder unchanged
        self.assertEqual(self.app.unsaved(), before)                          # still unsaved: it was only a copy
        self.assertIn("schedule.json", json.dumps(self.call("GET", "/api/status")[1]["unsaved"]))
        copied = Clinic.load_dir(target)
        self.assertIsNone(copied.presence_at("staff", "s1", "2026-11-23"))    # the copy has the working (unsaved) data

    def test_dry_run_reports_what_would_be_overwritten(self):
        target = os.path.join(self.tmp.name, "copy")
        self.call("POST", "/api/export", {"directory": target})
        self.app.clinic.set_blocked("room", "central_laser_a", "2026-11-23", 40, 44, True)   # changes schedule.json only
        status, info = self.call("POST", "/api/export", {"directory": target, "dry_run": True})
        by = {f["name"]: f for f in info["files"]}
        self.assertEqual([(n, by[n]["exists"], by[n]["identical"]) for n in NAMES],
                         [("planning.json", True, True), ("resources.json", True, True), ("schedule.json", True, False)])
        status, done = self.call("POST", "/api/export", {"directory": target})
        self.assertEqual(done["written"], ["schedule.json"])                  # identical files aren't rewritten

    def test_directory_input_is_validated(self):
        for bad in (None, "", "   ", 5, "bad\0path"):
            self.assertEqual(self.call("POST", "/api/export", {"directory": bad})[0], 400, repr(bad))
        self.assertEqual(self.call("POST", "/api/export", {})[0], 400)
        a_file = os.path.join(self.tmp.name, "afile.txt")
        open(a_file, "w").close()
        status, r = self.call("POST", "/api/export", {"directory": a_file})
        self.assertEqual(status, 400)
        self.assertIn("is a file", r["error"])
        self.assertEqual(self.call("POST", "/api/import", {"directory": a_file})[0], 400)

    def test_tilde_and_relative_paths_are_resolved(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
        try:
            os.environ["HOME"] = home
            status, info = self.call("POST", "/api/export", {"directory": "~/saved", "dry_run": True})
            self.assertEqual(info["directory"], os.path.realpath(os.path.join(home, "saved")))
            os.chdir(self.tmp.name)
            status, info = self.call("POST", "/api/export", {"directory": "rel/dir", "dry_run": True})
            self.assertEqual(info["directory"], os.path.realpath(os.path.join(self.tmp.name, "rel", "dir")))
        finally:
            os.chdir(old_cwd)
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    # ---- load -------------------------------------------------------------------------
    def make_other(self, name="other", days=7):
        other = os.path.join(self.tmp.name, name)
        c = build_sample(date(2026, 11, 20), days=days)
        c.staff["s1"].name = "Loaded Person"
        c.save_dir(other)
        return other, c

    def test_load_replaces_the_working_data_and_shows_as_unsaved(self):
        other, c = self.make_other()
        status, info = self.call("POST", "/api/import", {"directory": other, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(info["summary"]["staff"], 6)
        self.assertEqual(self.app.clinic.staff["s1"].name, "Ana")             # a dry run changes nothing
        status, r = self.call("POST", "/api/import", {"directory": other})
        self.assertEqual(status, 200)
        self.assertEqual(self.app.clinic.staff["s1"].name, "Loaded Person")
        self.assertEqual(r["clinic"]["staff"][0]["name"], "Loaded Person")
        unsaved = [u["file"] for u in r["unsaved"]]
        self.assertIn("resources.json", unsaved)                              # differs from the data folder: unsaved
        self.assertEqual(Clinic.read_files(self.data), self.snapshot)         # ...and the data folder is untouched
        status, r = self.call("POST", "/api/discard", {})                     # Discard gets the data folder back
        self.assertEqual(self.app.clinic.staff["s1"].name, "Ana")

    def test_loading_the_data_folder_itself_is_a_no_op(self):
        status, r = self.call("POST", "/api/import", {"directory": self.data})
        self.assertEqual((status, r["unsaved"]), (200, []))

    def test_load_refuses_incomplete_or_invalid_folders(self):
        other, _ = self.make_other()
        os.remove(os.path.join(other, "planning.json"))
        status, r = self.call("POST", "/api/import", {"directory": other})
        self.assertEqual(status, 400)
        self.assertIn("missing planning.json", r["error"])
        status, r = self.call("POST", "/api/import", {"directory": os.path.join(self.tmp.name, "nope")})
        self.assertEqual(status, 400)
        self.assertIn("doesn't exist", r["error"])
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        self.assertEqual(self.call("POST", "/api/import", {"directory": empty})[0], 400)
        broken, _ = self.make_other("broken")
        with open(os.path.join(broken, "schedule.json"), "w") as f:
            f.write("{nope")
        status, r = self.call("POST", "/api/import", {"directory": broken})
        self.assertEqual(status, 400)
        self.assertIn("schedule.json", r["error"])
        invalid, _ = self.make_other("invalid")
        path = os.path.join(invalid, "planning.json")
        with open(path) as f:
            data = json.load(f)
        data["doctor_skills"]["ghost"] = []
        with open(path, "w") as f:
            json.dump(data, f)
        status, r = self.call("POST", "/api/import", {"directory": invalid})
        self.assertEqual(status, 400)
        self.assertIn("ghost", r["error"])
        self.assertEqual(self.app.clinic.staff["s1"].name, "Ana")             # every refusal left the working data alone

    def test_saved_treatment_lists_are_not_part_of_a_directory_copy(self):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "treatment_lists.json"), "w") as f:
            f.write('{"version": 1, "lists": {}}')
        target = os.path.join(self.tmp.name, "copy")
        self.call("POST", "/api/export", {"directory": target})
        self.assertNotIn("treatment_lists.json", os.listdir(target))
        other, _ = self.make_other()
        with open(os.path.join(other, "treatment_lists.json"), "w") as f:
            f.write("not even json")
        self.assertEqual(self.call("POST", "/api/import", {"directory": other})[0], 200)   # ignored, never read

    def test_files_endpoint_returns_the_exact_file_text(self):
        status, r = self.call("GET", "/api/files")
        self.assertEqual((status, r["files"]), (200, self.snapshot))
        self.assertEqual(r["counts"]["doctors"], 3)


class BrowseTests(DirectoryTests):
    """The in-page folder chooser: list folders, create one, never list files."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp.name, "root")
        for d in ("beta", "Alpha", "gamma/inner", ".hidden", "clinic_here"):
            os.makedirs(os.path.join(self.root, d))
        open(os.path.join(self.root, "a_file.txt"), "w").close()
        for n in NAMES:
            open(os.path.join(self.root, "clinic_here", n), "w").close()
        open(os.path.join(self.root, "gamma", "resources.json"), "w").close()    # only one of three: not a clinic folder

    def browse(self, path=None, hidden=False):
        q = {}
        if path is not None:
            q["path"] = [path]
        if hidden:
            q["hidden"] = ["1"]
        return dispatch(self.app, "GET", "/api/browse", q, None)

    def test_lists_only_folders_sorted_and_hides_dotfolders(self):
        status, r = self.browse(self.root)
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in r["entries"]], ["Alpha", "beta", "clinic_here", "gamma"])   # case-insensitive order
        self.assertEqual(r["current"]["path"], os.path.realpath(self.root))
        self.assertEqual(r["current"]["parent"], os.path.realpath(self.tmp.name))
        self.assertTrue(r["current"]["writable"])
        self.assertEqual(r["current"]["files"], [])
        self.assertIsNone(r["requested_missing"])
        status, r = self.browse(self.root, hidden=True)
        self.assertIn(".hidden", [e["name"] for e in r["entries"]])

    def test_clinic_folders_are_flagged_and_entries_carry_full_paths(self):
        status, r = self.browse(self.root)
        flags = {e["name"]: e["clinic"] for e in r["entries"]}
        self.assertEqual(flags, {"Alpha": False, "beta": False, "clinic_here": True, "gamma": False})
        self.assertEqual(r["entries"][0]["path"], os.path.join(os.path.realpath(self.root), "Alpha"))
        status, inside = self.browse(os.path.join(self.root, "clinic_here"))
        self.assertEqual(inside["current"]["files"], list(GROUP_FILES.values()))    # tells you what's in the current folder

    def test_nonexistent_paths_fall_back_to_the_nearest_existing_folder(self):
        wanted = os.path.join(self.root, "gamma", "new", "deeper")
        status, r = self.browse(wanted)
        self.assertEqual(status, 200)
        self.assertEqual(r["current"]["path"], os.path.join(os.path.realpath(self.root), "gamma"))
        self.assertEqual(r["requested_missing"], os.path.realpath(wanted))

    def test_a_file_path_shows_its_folder(self):
        status, r = self.browse(os.path.join(self.root, "a_file.txt"))
        self.assertEqual(r["current"]["path"], os.path.realpath(self.root))

    def test_defaults_tilde_and_shortcuts(self):
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = self.root
            status, r = self.browse()
            self.assertEqual(r["current"]["path"], os.path.realpath(self.root))              # no path = home
            status, r = self.browse("~/gamma")
            self.assertEqual(r["current"]["path"], os.path.join(os.path.realpath(self.root), "gamma"))
            labels = {x["label"]: x["path"] for x in r["shortcuts"]}
            self.assertEqual(labels["Data folder"], os.path.realpath(self.data))
            self.assertEqual(labels["Home"], os.path.realpath(self.root))
            self.assertIn("Working directory", labels)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    def test_the_filesystem_root_has_no_parent(self):
        status, r = self.browse(os.path.abspath(os.sep))
        self.assertEqual((status, r["current"]["parent"]), (200, None))

    def test_unreadable_folders_and_bad_paths_are_reported(self):
        import clinic.server as server
        real = server.os.scandir

        def denied(path):
            raise PermissionError(13, "Permission denied")
        server.os.scandir = denied
        try:
            status, r = self.browse(self.root)
        finally:
            server.os.scandir = real
        self.assertEqual(status, 400)
        self.assertIn("Can't read", r["error"])
        self.assertEqual(self.browse("bad\0path")[0], 400)

    def test_a_huge_folder_is_capped(self):
        big = os.path.join(self.tmp.name, "big")
        os.makedirs(big)
        for i in range(App.MAX_BROWSE_ENTRIES + 5):
            os.mkdir(os.path.join(big, f"d{i:04d}"))
        status, r = self.browse(big)
        self.assertEqual((len(r["entries"]), r["truncated"]), (App.MAX_BROWSE_ENTRIES, True))

    def test_it_never_changes_anything(self):
        self.browse(self.root)
        self.assertEqual(Clinic.read_files(self.data), self.snapshot)
        self.assertEqual(sorted(os.listdir(self.root)), sorted([".hidden", "Alpha", "a_file.txt", "beta", "clinic_here", "gamma"]))

    # ---- new folder ------------------------------------------------------------------
    def test_make_a_new_folder(self):
        status, r = dispatch(self.app, "POST", "/api/mkdir", {}, {"path": self.root, "name": "  backups 2026  "})
        self.assertEqual(status, 200)
        self.assertEqual(r["path"], os.path.join(os.path.realpath(self.root), "backups 2026"))
        self.assertTrue(os.path.isdir(r["path"]))
        self.assertEqual(os.listdir(r["path"]), [])                            # empty: it creates nothing else

    def test_new_folder_names_are_validated(self):
        for bad in ("", "   ", ".", "..", "a/b", "a\\b", "../escape", "x" * 101, "nul\0byte", None, 5):
            status, r = dispatch(self.app, "POST", "/api/mkdir", {}, {"path": self.root, "name": bad})
            self.assertEqual(status, 400, repr(bad))
        self.assertEqual(dispatch(self.app, "POST", "/api/mkdir", {}, {"path": self.root, "name": "beta"})[0], 400)   # exists
        self.assertEqual(dispatch(self.app, "POST", "/api/mkdir", {}, {"path": os.path.join(self.root, "nope"), "name": "x"})[0], 400)
        self.assertEqual(dispatch(self.app, "POST", "/api/mkdir", {}, {"path": os.path.join(self.root, "a_file.txt"), "name": "x"})[0], 400)
        self.assertEqual(dispatch(self.app, "POST", "/api/mkdir", {}, {"name": "x"})[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape")))      # nothing escaped the chosen folder

    # (the inherited directory tests run again here: harmless, they use the same fixtures)


class StatelessDirectoryTests(unittest.TestCase):
    """On Vercel the server can't see the user's computer, and must not read or write its own paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "clinic_data")
        build_sample(date(2026, 11, 20), days=7).save_dir(self.dir)
        init = initial_payload(self.dir)
        self.state = {"version": 1, "groups": init["groups"], "base": init["groups"], "lists": {}, "rawHash": init["hashes"]}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return handle_call({"method": method, "path": path, "query": {}, "body": body, "state": self.state}, self.dir)

    def test_server_directories_are_refused(self):
        target = os.path.join(self.tmp.name, "should-not-exist")
        out = handle_call({"method": "GET", "path": "/api/browse", "query": {"path": [self.tmp.name]}, "body": None, "state": self.state}, self.dir)
        self.assertEqual(out["status"], 400)                                    # the server's filesystem is never listed
        self.assertNotIn("entries", out["result"])
        out = self.call("POST", "/api/mkdir", {"path": self.tmp.name, "name": "x"})
        self.assertEqual(out["status"], 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x")))
        for path in ("/api/export", "/api/import"):
            out = self.call("POST", path, {"directory": target})
            self.assertEqual(out["status"], 400, path)
            self.assertIn("browser", out["result"]["error"])
        self.assertFalse(os.path.exists(target))
        out = self.call("POST", "/api/import", {"directory": self.dir})        # not even the deployed data is readable this way
        self.assertEqual(out["status"], 400)

    def test_files_endpoint_and_loading_through_put_clinic_work_for_the_folder_picker(self):
        files = self.call("GET", "/api/files")["result"]["files"]
        self.assertEqual(sorted(files), NAMES)
        groups = {g: json.loads(files[name]) for g, name in GROUP_FILES.items()}
        groups["resources"]["staff"][0]["name"] = "Edited in a folder"
        out = self.call("PUT", "/api/clinic", groups)                          # what "Load from folder" sends
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["state"]["groups"]["resources"]["staff"][0]["name"], "Edited in a folder")
        self.assertIn("resources.json", [u["file"] for u in out["result"]["unsaved"]])
        bad = dict(groups, planning={"doctor_skills": {"ghost": []}})
        self.assertEqual(self.call("PUT", "/api/clinic", bad)["status"], 400)


if __name__ == "__main__":
    unittest.main()
