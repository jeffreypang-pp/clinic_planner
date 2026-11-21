"""Saved lists of treatments: explicit saves only, validated, separate from the clinic data files."""
import json
import os
import tempfile
import unittest

from clinic.lists import FILE_NAME, LIST_FILE_KIND, TreatmentLists, build_list, render_list_file, safe_filename
from clinic.model import ClinicError, Conflict, NotFound
from tests.test_clinic import small_clinic


def item(**kw):
    base = dict(name="Thing", numbing_minutes=15, doctor_minutes=30, staff_minutes=15,
                room_id="a_plain", start="10:00", client="C")
    return dict(base, **kw)


class ListStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "data")
        self.lists = TreatmentLists(self.dir)
        self.c = small_clinic()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_exists_until_a_list_is_saved(self):
        self.assertEqual(self.lists.all(self.c), [])
        self.assertFalse(os.path.exists(self.dir))

    def test_save_load_roundtrip_keeps_order_and_fields(self):
        items = [item(start="09:00"), item(name="Facial", numbing_minutes=0, doctor_minutes=0, staff_minutes=30,
                                           start="11:15", room_id="a_laser"), item(doctor_id="d1", treatment_id="laser", room_id="a_laser")]
        saved = self.lists.save(self.c, "Morning rush", "a", items)
        self.assertEqual(saved["location_id"], "a")
        got = self.lists.all(self.c)
        self.assertEqual([(x["name"], x["location"], x["count"]) for x in got], [("Morning rush", "A", 3)])
        self.assertEqual([i["start"] for i in got[0]["items"]], ["09:00", "11:15", "10:00"])
        self.assertEqual(got[0]["items"][1]["staff_minutes"], 30)
        self.assertEqual((got[0]["items"][2]["doctor_id"], got[0]["items"][2]["treatment_id"]), ("d1", "laser"))
        with open(os.path.join(self.dir, FILE_NAME)) as f:
            self.assertEqual(json.load(f)["lists"]["Morning rush"]["items"], got[0]["items"])

    def test_existing_name_needs_overwrite_and_only_that_entry_changes(self):
        self.lists.save(self.c, "One", "a", [item()])
        self.lists.save(self.c, "Two", "b", [item(room_id="b_plain")])
        with self.assertRaises(Conflict):
            self.lists.save(self.c, "One", "a", [item(start="12:00")])
        self.assertEqual(self.lists.all(self.c)[0]["items"][0]["start"], "10:00")        # unchanged
        self.lists.save(self.c, "One", "a", [item(start="12:00"), item()], overwrite=True)
        by = {x["name"]: x for x in self.lists.all(self.c)}
        self.assertEqual((by["One"]["count"], by["One"]["items"][0]["start"]), (2, "12:00"))
        self.assertEqual(by["Two"]["count"], 1)                                             # neighbour untouched

    def test_delete(self):
        self.lists.save(self.c, "One", "a", [item()])
        self.lists.delete("One")
        self.assertEqual(self.lists.all(self.c), [])
        with self.assertRaises(NotFound):
            self.lists.delete("One")

    def test_validation_rejects_bad_lists_without_writing(self):
        bad = [("", [item()]), ("   ", [item()]), ("x" * 81, [item()]), ("ok", []), ("ok", None), ("ok", ["nope"]),
               ("ok", [item(doctor_minutes=0)]),                       # numbing without a doctor treatment
               ("ok", [item(), item(numbing_minutes=0, doctor_minutes=0, staff_minutes=0)]),
               ("ok", [item(numbing_minutes=10)]), ("ok", [item(start="")]), ("ok", [item(start="10:10")]),
               ("ok", [item(room_id="b_plain")]),                      # room at another location
               ("ok", [item(room_id="ghost")]), ("ok", [item(doctor_id="ghost")]), ("ok", [item(treatment_id="ghost")])]
        for name, items in bad:
            with self.assertRaises(ClinicError, msg=str((name, items))):
                self.lists.save(self.c, name, "a", items)
        with self.assertRaises(NotFound):
            self.lists.save(self.c, "ok", "nowhere", [item()])
        self.assertFalse(os.path.exists(os.path.join(self.dir, FILE_NAME)))
        with self.assertRaises(ClinicError) as cm:
            self.lists.save(self.c, "ok", "a", [item(), item(doctor_minutes=0)])
        self.assertIn("Treatment 2", str(cm.exception))              # says which row is wrong

    def test_named_staff_are_saved_per_step_and_validated(self):
        items = [item(staff={"numbing": "s1", "doctor": None, "staff": "s2"}),
                 item(numbing_minutes=0, doctor_minutes=0, staff_minutes=30, staff={"staff": "s1"})]
        self.lists.save(self.c, "named", "a", items)
        got = self.lists.all(self.c)[0]["items"]
        self.assertEqual(got[0]["staff"], {"numbing": "s1", "doctor": None, "staff": "s2"})
        self.assertEqual(got[1]["staff"], {"staff": "s1"})                       # only the steps it has
        self.lists.save(self.c, "later", "a", [item()])                          # nothing named = assign later
        later = next(x for x in self.lists.all(self.c) if x["name"] == "later")
        self.assertEqual(later["items"][0]["staff"], {"numbing": None, "doctor": None, "staff": None})
        for bad, text in ((dict(staff={"numbing": "ghost"}), "ghost"),
                          (dict(numbing_minutes=0, staff={"numbing": "s1"}), "numbing step"),
                          (dict(staff={"bogus": "s1"}), "Unknown step"),
                          (dict(staff=["s1"]), "staff as")):
            with self.assertRaises(ClinicError, msg=str(bad)) as cm:
                self.lists.save(self.c, "bad", "a", [item(**bad)])
            self.assertIn(text, str(cm.exception))
            self.assertIn("Treatment 1", str(cm.exception))

    def test_a_row_without_a_room_can_be_saved(self):
        self.lists.save(self.c, "norooms", "a", [item(room_id=None)])
        self.assertIsNone(self.lists.all(self.c)[0]["items"][0]["room_id"])

    def test_corrupt_file_is_reported_not_overwritten(self):
        os.makedirs(self.dir)
        path = os.path.join(self.dir, FILE_NAME)
        with open(path, "w") as f:
            f.write("{nope")
        with self.assertRaises(ClinicError):
            self.lists.all(self.c)
        with self.assertRaises(ClinicError):
            self.lists.save(self.c, "x", "a", [item()])
        with open(path) as f:
            self.assertEqual(f.read(), "{nope")

    def test_names_with_odd_characters(self):
        for name in ("Morning / rush & co", "ünï", "a b  c"):
            self.lists.save(self.c, name, "a", [item()])
        self.assertEqual({x["name"] for x in self.lists.all(self.c)}, {"Morning / rush & co", "ünï", "a b  c"})


class ClinicPerTreatmentTests(unittest.TestCase):
    """Each treatment in a list belongs to its own clinic (location); the list's clinic is the default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lists = TreatmentLists(os.path.join(self.tmp.name, "data"))
        self.c = small_clinic()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_list_can_mix_clinics_and_validates_each_room_against_its_own_clinic(self):
        items = [item(location_id="a", room_id="a_plain"), item(location_id="b", room_id="b_plain", start="11:00"),
                 item(room_id="a_laser")]                                           # no clinic: the list's (a)
        saved = self.lists.save(self.c, "mixed", "a", items)
        self.assertEqual([i["location_id"] for i in saved["items"]], ["a", "b", "a"])
        got = self.lists.all(self.c)[0]
        self.assertEqual((got["location"], got["locations"]), ("A", ["A", "B"]))
        for bad, text in ((item(location_id="b", room_id="a_plain"), "not at B"),     # a room of the other clinic
                          (item(location_id="a", room_id="b_plain"), "not at A"),
                          (item(location_id="nowhere"), "nowhere")):
            with self.assertRaises(ClinicError, msg=str(bad)) as cm:
                self.lists.save(self.c, "bad", "a", [item(), bad])
            self.assertIn(text, str(cm.exception))
            self.assertIn("Treatment 2", str(cm.exception))                           # says which row is wrong

    def test_older_lists_without_a_clinic_per_item_still_work(self):
        path = os.path.join(self.tmp.name, "data", FILE_NAME)
        os.makedirs(os.path.dirname(path))
        legacy_item = {k: v for k, v in item().items()}
        with open(path, "w") as f:
            json.dump({"version": 1, "lists": {"old": {"location_id": "b", "saved": "2026-01-01T00:00:00",
                                                       "items": [dict(legacy_item, room_id="b_plain")]}}}, f)
        got = self.lists.all(self.c)[0]
        self.assertEqual(got["items"][0]["location_id"], "b")                        # filled in from the list's clinic
        self.assertEqual((got["location"], got["locations"]), ("B", ["B"]))
        with open(path) as f:
            self.assertNotIn("location_id", json.load(f)["lists"]["old"]["items"][0])   # reading never rewrites the file

    def test_files_carry_each_treatments_clinic_and_loading_is_lenient_about_removed_clinics(self):
        r = render_list_file(self.c, "mixed", "a", [item(location_id="b", room_id="b_plain"), item(room_id="a_plain")])
        self.assertEqual([i["location_id"] for i in json.loads(r["text"])["items"]], ["b", "a"])
        stale = [item(location_id="gone", room_id="a_plain"), item(location_id="b", room_id="a_plain")]
        r = render_list_file(self.c, "old", "a", stale, lenient=True)
        self.assertEqual((r["items"][0]["location_id"], r["items"][0]["room_id"]), ("a", "a_plain"))   # fell back to the list's clinic
        self.assertEqual((r["items"][1]["location_id"], r["items"][1]["room_id"]), ("b", None))        # room isn't at B any more
        self.assertTrue(any("clinic no longer exists" in w for w in r["warnings"]))
        self.assertTrue(any("isn't at B" in w for w in r["warnings"]))
        with self.assertRaises(NotFound):
            render_list_file(self.c, "old", "a", stale)                              # strict: still an error


class ListFileTests(unittest.TestCase):
    """A single list as a stand-alone file: validated like the app's own store, loadable leniently."""

    def setUp(self):
        self.c = small_clinic()

    def test_render_produces_a_portable_file(self):
        items = [item(staff={"numbing": "s1", "doctor": None, "staff": None}), item(start="11:15", room_id="a_laser")]
        r = render_list_file(self.c, "  Morning rush  ", "a", items)
        self.assertEqual((r["filename"], r["name"], r["warnings"]), ("Morning rush.json", "Morning rush", []))
        data = json.loads(r["text"])
        self.assertEqual((data["kind"], data["version"], data["name"], data["location_id"]),
                         (LIST_FILE_KIND, 1, "Morning rush", "a"))
        self.assertEqual(data["items"], r["items"])
        self.assertEqual([i["start"] for i in data["items"]], ["10:00", "11:15"])
        self.assertEqual(data["items"][0]["staff"], {"numbing": "s1", "doctor": None, "staff": None})
        self.assertTrue(r["text"].endswith("\n"))

    def test_strict_validation_matches_the_app_store(self):
        bad = [("", [item()]), ("ok", []), ("ok", [item(doctor_minutes=0)]), ("ok", [item(start="10:10")]),
               ("ok", [item(room_id="b_plain")]), ("ok", [item(room_id="ghost")]), ("ok", [item(doctor_id="ghost")]),
               ("ok", [item(staff={"numbing": "ghost"})])]
        for name, items in bad:
            with self.assertRaises(ClinicError, msg=str((name, items))):
                render_list_file(self.c, name, "a", items)
        with self.assertRaises(NotFound):
            render_list_file(self.c, "ok", "nowhere", [item()])

    def test_lenient_loading_drops_ids_that_no_longer_exist_and_says_so(self):
        gone = item(room_id="ghost_room", doctor_id="ghost_doc", treatment_id="ghost_tpl",
                    staff={"numbing": "ghost_staff", "doctor": "s2", "staff": None})
        r = render_list_file(self.c, "old list", "a", [gone, item(room_id="b_plain")], lenient=True)
        first, second = r["items"]
        self.assertEqual((first["room_id"], first["doctor_id"], first["treatment_id"]), (None, None, None))
        self.assertEqual(first["staff"], {"numbing": None, "doctor": "s2", "staff": None})   # the staff that exists is kept
        self.assertIsNone(second["room_id"])                                                    # a room at another location
        text = " ".join(r["warnings"])
        for needle in ("room", "doctor", "template", "staff member"):
            self.assertIn(needle, text)
        self.assertTrue(r["warnings"][0].startswith("Treatment 1"))
        # the things that can't be repaired are still errors, even when lenient
        for broken in (item(doctor_minutes=0), item(start="25:00"), item(start=""), "not an item"):
            with self.assertRaises(ClinicError, msg=str(broken)):
                render_list_file(self.c, "x", "a", [broken], lenient=True)
        with self.assertRaises(NotFound):
            render_list_file(self.c, "x", "gone location", [item()], lenient=True)         # a list for a location we don't have

    def test_safe_filenames_cannot_escape_or_upset_any_operating_system(self):
        cases = {"Morning rush": "Morning rush.json", "a/b\\c": "a_b_c.json", "../../etc/passwd": "_.._etc_passwd.json",
                 "CON": "_CON.json", "  .hidden ": "hidden.json", "x.json": "x.json", "a:b*c?": "a_b_c_.json",
                 "": "treatment list.json", "ünï  ": "ünï.json", "COM1.txt": "_COM1.txt.json"}
        for name, expected in cases.items():
            self.assertEqual(safe_filename(name), expected, repr(name))
        for nasty in ("../x", "a/../../b", "\\\\server\\share", "x\0y", "." * 5, " ", "x" * 500):
            out = safe_filename(nasty)
            self.assertTrue(out.endswith(".json") and len(out) <= 105, repr(nasty))
            self.assertFalse(any(ch in out for ch in '/\\\0'), repr(nasty))
            self.assertNotIn("..", out.replace(".json", "") if out.startswith(".") else "", repr(nasty))


if __name__ == "__main__":
    unittest.main()
