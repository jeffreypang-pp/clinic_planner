"""Saved lists of treatments: explicit saves only, validated, separate from the clinic data files."""
import json
import os
import tempfile
import unittest

from clinic.lists import FILE_NAME, TreatmentLists
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


if __name__ == "__main__":
    unittest.main()
