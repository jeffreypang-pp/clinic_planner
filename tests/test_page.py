"""Cheap guards on public/index.html for UI bugs that only show up in a browser."""
import re
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parent.parent / "public" / "index.html").read_text()
CSS = re.search(r"<style>(.*?)</style>", PAGE, re.S).group(1)
JS = re.search(r"<script>(.*)</script>", PAGE, re.S).group(1)


class SelectionClassTests(unittest.TestCase):
    def test_the_timetable_drag_overlay_class_is_scoped_to_timetable_rows(self):
        """`.sel` was global (position:absolute, full height, translucent blue), so any other element given
        that class, such as a file picked in the chooser, became a screen-sized blue band."""
        bare = [m for m in re.findall(r"(?m)^\s*([^{}\n]*\.sel\b[^{}\n]*)\{", CSS)
                if not re.match(r"\s*\.tt-row\s*>\s*\.sel\b", m)]
        self.assertEqual(bare, [], f"unscoped .sel rules: {bare}")

    def test_the_chooser_does_not_reuse_the_overlay_class(self):
        chooser = JS[JS.index("function openPicker"):JS.index("const openDirPicker")]
        self.assertNotIn('"sel"', chooser)
        self.assertNotIn("'sel'", chooser)
        self.assertIn("picked", chooser)

    def test_every_class_the_chooser_adds_has_a_scoped_style(self):
        chooser = JS[JS.index("function openPicker"):JS.index("const openDirPicker")]
        for cls in set(re.findall(r'classList\.add\("([\w-]+)"\)', chooser)):
            self.assertRegex(CSS, rf"#dirpick[^{{}}]*\.{cls}\b", cls)

    def test_elements_hidden_with_the_hidden_attribute_are_not_overridden_by_display_rules(self):
        """`el.hidden = true` is ignored if a rule such as `#x { display:flex }` wins; each id/class that the
        page toggles with `.hidden` and that sets `display` needs a matching `[hidden]` rule."""
        for sel in ("#dirpick", "#login", "#dirpick .dp-fname"):
            self.assertIn(f"{sel}[hidden]", CSS.replace("\n", " "), sel)


if __name__ == "__main__":
    unittest.main()
