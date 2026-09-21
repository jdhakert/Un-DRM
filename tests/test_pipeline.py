#!/usr/bin/env python3
"""End-to-end and unit tests for the Un-DRM scripts.

Run:  python3 -m unittest discover -s tests -v
The pipeline test drives undrm_capture.py against tests/fake_peekaboo.py (no Mac
needed) and then assembles the result; it requires Pillow (fixtures) and pypdf
(PDF verification) - both are skipped with a message when missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import undrm_assemble as asm  # noqa: E402
import undrm_capture as cap  # noqa: E402


class RectParsing(unittest.TestCase):
    def test_forms(self):
        want = {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
        self.assertEqual(cap.rect_from_json({"x": 1, "y": 2, "width": 3, "height": 4}), want)
        self.assertEqual(cap.rect_from_json([[1, 2], [3, 4]]), want)
        self.assertEqual(cap.rect_from_json([1, 2, 3, 4]), want)
        self.assertEqual(cap.rect_from_json({"origin": {"x": 1, "y": 2}, "size": {"width": 3, "height": 4}}), want)
        self.assertIsNone(cap.rect_from_json(None))
        self.assertIsNone(cap.rect_from_json("junk"))
        self.assertEqual(asm.size_from_json([5, 6]), {"width": 5.0, "height": 6.0})

    def test_envelope_parsing(self):
        self.assertEqual(cap.parse_envelope('{"success": true, "data": {}}')["success"], True)
        self.assertEqual(cap.parse_envelope('note: something\n{"success": false, "error": {"code": "X"}}')["error"]["code"], "X")
        self.assertIsNone(cap.parse_envelope(""))
        self.assertIsNone(cap.parse_envelope("not json at all"))


class Layout(unittest.TestCase):
    @staticmethod
    def line(text, x, y, w=200, h=12, conf=0.9):
        return asm.Line(text, conf, x, y, w, h)

    def page(self, lines):
        page = asm.Page(1, Path("x.png"), {"x": 0, "y": 0, "width": 600, "height": 800}, lines, [], "test")
        asm.layout_page(page, keep_lines=False, xy_cut=True, dehyphenate=True,
                        col_gap_factor=0.8, row_gap_factor=1.0, para_gap_factor=0.6)
        return page

    def test_two_columns_read_left_then_right(self):
        lines = []
        for i in range(6):
            lines.append(self.line("L%d" % i, 20, 100 + i * 16, w=200))
        for i in range(6):
            lines.append(self.line("R%d" % i, 300, 100 + i * 16, w=200))
        page = self.page(lines[::-1])  # shuffled order
        self.assertEqual(page.text.replace("\n\n", " "), "L0 L1 L2 L3 L4 L5 R0 R1 R2 R3 R4 R5")

    def test_header_spanning_columns_comes_first(self):
        lines = [self.line("HEADER", 20, 20, w=480)]
        for i in range(4):
            lines.append(self.line("L%d" % i, 20, 100 + i * 16, w=200))
            lines.append(self.line("R%d" % i, 300, 100 + i * 16, w=200))
        page = self.page(lines)
        paragraphs = page.text.split("\n\n")
        self.assertEqual(paragraphs[0], "HEADER")
        self.assertEqual(" ".join(paragraphs[1:]), "L0 L1 L2 L3 R0 R1 R2 R3")

    def test_paragraph_gap_and_dehyphenation(self):
        lines = [self.line("The court gave consider-", 20, 100),
                 self.line("ation to it.", 20, 116),
                 self.line("New paragraph here.", 20, 160)]
        page = self.page(lines)
        self.assertEqual(page.text, "The court gave consideration to it.\n\nNew paragraph here.")

    def test_fragments_on_one_row_are_joined(self):
        lines = [self.line("world", 120, 100, w=60), self.line("hello", 20, 101, w=60)]
        page = self.page(lines)
        self.assertEqual(page.text, "hello world")

    def test_numbered_list_labels_stay_with_their_items(self):
        lines = []
        for i in range(4):
            y = 100 + i * 32
            lines.append(self.line("%d." % (i + 1), 40, y, w=14))
            lines.append(self.line("item %d text goes here" % (i + 1), 70, y, w=220))
            lines.append(self.line("continuation of item %d" % (i + 1), 70, y + 16, w=200))
        page = self.page(lines)
        self.assertEqual(page.text.split("\n\n")[0], "1. item 1 text goes here continuation of item 1")
        self.assertIn("4. item 4 text goes here continuation of item 4", page.text)

    def test_speaker_labels_and_toc_page_numbers(self):
        dialogue = [self.line("HAMLET:", 40, 100, w=70), self.line("To be or not to be", 130, 100, w=200),
                    self.line("OPHELIA:", 40, 116, w=70), self.line("My lord, how fares", 130, 116, w=200)]
        page = self.page(dialogue)
        self.assertEqual(page.text, "HAMLET: To be or not to be OPHELIA: My lord, how fares")
        toc = []
        for i in range(5):
            toc.append(self.line("Chapter %d Title" % i, 40, 100 + i * 16, w=200))
            toc.append(self.line(str(i * 10), 500, 100 + i * 16, w=20))
        page = self.page(toc)
        self.assertTrue(page.text.startswith("Chapter 0 Title 0 Chapter 1 Title 10"), page.text)

    def test_tight_two_column_gutter_still_splits(self):
        lines = []
        for i in range(6):
            lines.append(self.line("L%d" % i, 20, 100 + i * 16, w=200))
            lines.append(self.line("R%d" % i, 240, 100 + i * 16, w=200))  # 20 pt gutter, 12 pt lines
        page = self.page(lines)
        self.assertEqual(page.text.replace("\n\n", " "), "L0 L1 L2 L3 L4 L5 R0 R1 R2 R3 R4 R5")

    def test_hanging_indent_entries(self):
        lines = [self.line("Smith, J. (2001). A very long title of a paper", 20, 100, w=300),
                 self.line("that wraps onto the next line.", 50, 116, w=200),
                 self.line("Jones, K. (2002). Another paper title", 20, 132, w=300),
                 self.line("also wrapping.", 50, 148, w=120)]
        page = self.page(lines)
        self.assertEqual(page.text, "Smith, J. (2001). A very long title of a paper that wraps onto the next line."
                                    "\n\nJones, K. (2002). Another paper title also wrapping.")

    def test_superscript_fragment_stays_on_its_line(self):
        lines = [self.line("text", 0, 100, w=50, h=12), self.line("2", 50, 97, w=4, h=6)]
        rows = asm.merge_rows(lines)
        self.assertEqual([[l.text for l in r] for r in rows], [["text", "2"]])

    def test_jpeg_info_tolerates_fill_bytes(self):
        sof = b"\xff\xc0" + b"\x00\x11" + b"\x08" + b"\x00\x20" + b"\x00\x10" + b"\x03" + b"\x00" * 9
        data = b"\xff\xd8" + b"\xff\xff\xdb\x00\x04\x00\x00" + sof + b"\xff\xd9"
        self.assertEqual(asm.jpeg_info(data), (16, 32, 3))

    def test_pdf_string_escaping(self):
        self.assertEqual(asm.pdf_string("a(b)\\c"), b"(a\\(b\\)\\\\c)")
        self.assertTrue(asm.pdf_text_string("Résumé").startswith(b"<FEFF"))
        self.assertAlmostEqual(asm.helvetica_width(b"ii"), 0.444, places=3)


def have(module):
    try:
        __import__(module)
        return True
    except Exception:  # broken installs count as missing
        return False


@unittest.skipUnless(have("PIL"), "Pillow is needed to render the fake viewer's pages")
class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="undrm-test-"))
        self.state = self.tmp / "state.json"
        self.doc = self.tmp / "Contracts Outline.pdf"
        self.doc.write_bytes(b"%PDF-1.4 fake")
        self.env = dict(os.environ, FAKE_PB_STATE=str(self.state))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_capture(self, *extra, expect=0):
        out = self.tmp / "capture"
        cmd = [sys.executable, str(ROOT / "undrm_capture.py"), "--peekaboo", str(ROOT / "tests" / "fake_peekaboo.py"),
               "--profile", str(ROOT / "tests" / "preview-optional.json"), "--doc", str(self.doc), "--out", str(out),
               "--settle-interval", "0.01", "--end-grace", "0.02", "--retry-delay", "0.01", "--advance-delay", "0",
               "--setup-settle", "0", *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env, cwd=str(self.tmp))
        self.assertEqual(proc.returncode, expect, proc.stderr)
        return out, proc

    def test_capture_and_assemble(self):
        out, proc = self.run_capture()
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete", proc.stderr)
        self.assertEqual(manifest["page_count"], 6)
        self.assertEqual([p["index"] for p in manifest["pages"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(manifest["pages"][4].get("duplicate_of"), 4)
        self.assertEqual(manifest["window"]["id"], 777)
        self.assertEqual(manifest["window"]["bounds"]["width"], 1000)
        self.assertGreater(manifest["pages"][0]["chars"], 100)
        self.assertEqual(manifest["pages"][3]["chars"], 0)  # blank page
        self.assertIn("only 0 characters recognized", manifest["pages"][3]["warnings"][0])
        for p in manifest["pages"]:
            self.assertTrue((out / p["image"]).exists(), p["image"])
            self.assertTrue((out / p["see_json"]).exists())
        self.assertIn("menu item 'View > Hide Bogus Panel' not present", proc.stderr)
        self.assertFalse((out / ".tmp").exists())
        state = json.loads(self.state.read_text())
        log = state["log"]
        self.assertTrue(any(l.startswith("clean --snapshot") for l in log))
        self.assertEqual(state["snapshots"], [], "every snapshot the fake published must be cleaned")
        self.assertTrue(any("View > Content Only" in l for l in log))
        self.assertTrue(any("set-bounds" in l for l in log))
        self.assertTrue(any(l.startswith("menu click") and "Go > Next Page" in l for l in log))

        # assemble
        cmd = [sys.executable, str(ROOT / "undrm_assemble.py"), str(out), "--title", "Contracts"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        txt = (out / "document.txt").read_text()
        self.assertIn("Contracts\n", txt)
        self.assertIn("No interest is good unless it must vest", txt)
        self.assertIn("careful consideration to the question", txt)  # dehyphenated
        self.assertIn("supported by consideration.", txt)
        left = txt.index("Left column first line")
        right = txt.index("Right column begins")
        self.assertLess(left, right)
        self.assertLess(txt.index("Second left paragraph"), right)
        self.assertEqual(txt.count("\f"), 5)
        md = (out / "document.md").read_text()
        self.assertIn("## Page 6", md)
        self.assertIn("![Page 1](pages/page-0001.png)", md)
        self.assertIn("_Identical to page 4._", md)
        data = json.loads((out / "document.json").read_text())
        self.assertEqual(data["page_count"], 6)
        self.assertEqual(data["pages"][5]["text"], "The End\n\nThis is the last page of the document.\n\n6")
        self.assertTrue((out / "document.pdf").exists())
        if have("pypdf"):
            import pypdf
            reader = pypdf.PdfReader(str(out / "document.pdf"))
            self.assertEqual(len(reader.pages), 6)
            self.assertEqual(reader.metadata.title, "Contracts")
            text = reader.pages[0].extract_text()
            self.assertIn("Rule Against Perpetuities", text)
            self.assertIn("twenty-one years", text)
            self.assertGreater(reader.pages[0]["/Resources"]["/XObject"]["/Im0"]["/Width"], 100)
        self.assertFalse((out / ".assemble").exists())

    def test_explicit_page_count_and_key_fallback(self):
        out, proc = self.run_capture("--pages", "2", "--advance", "key")
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["page_count"], 2)
        self.assertEqual(manifest["stop_reason"], "requested page count reached")
        self.assertEqual(manifest["advance"]["mode"], "key-foreground")  # background press was refused
        self.assertIn("switching to foreground key presses", proc.stderr)

    def test_page_limit_holds_when_a_duplicate_is_recorded(self):
        out, _ = self.run_capture("--pages", "5")
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["page_count"], 5)
        self.assertEqual(manifest["pages"][-1].get("duplicate_of"), 4)
        self.assertEqual(manifest["stop_reason"], "requested page count reached")
        out2, _ = self.run_capture("--max-pages", "5", "--force")
        manifest = json.loads((out2 / "manifest.json").read_text())
        self.assertEqual(manifest["page_count"], 5)
        self.assertEqual(manifest["stop_reason"], "--max-pages reached")

    def test_background_press_stays_in_the_background(self):
        self.env["FAKE_PB_ALLOW_BACKGROUND_PRESS"] = "1"
        out, proc = self.run_capture("--advance", "key")
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["page_count"], 6)
        self.assertEqual(manifest["advance"]["mode"], "key")
        self.assertNotIn("switching to foreground", proc.stderr)
        presses = [l for l in json.loads(self.state.read_text())["log"] if l.startswith("press")]
        self.assertTrue(presses)
        for line in presses:
            self.assertIn("--window-id 777", line)
            self.assertNotIn("--foreground", line)

    def test_blank_page_in_viewer_without_accessibility_is_recorded(self):
        self.env["FAKE_PB_NO_AX"] = "1"
        out, proc = self.run_capture()
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete", proc.stderr)
        self.assertEqual(manifest["page_count"], 6)
        blank = manifest["pages"][3]
        self.assertEqual(blank["chars"], 0)
        self.assertEqual(blank["engine"], "peekaboo")
        self.assertTrue(any("ACCESSIBILITY_INCOMPLETE" in w for w in blank["warnings"]), blank["warnings"])
        self.assertTrue((out / blank["image"]).exists())
        self.assertEqual(manifest["pages"][5]["chars"] > 0, True)

    def test_never_settling_window_stops_when_text_repeats(self):
        self.env["FAKE_PB_NOISE"] = "1"
        out, proc = self.run_capture("--settle-timeout", "0.05")
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete", proc.stderr)
        self.assertIn("never settles", manifest["stop_reason"])
        texts = [p["text_hash"] for p in manifest["pages"]]
        self.assertEqual(len(texts), len(set(texts)), "no page should be captured twice")
        self.assertEqual(manifest["page_count"], 5)  # the blank duplicate page cannot be told apart
        self.assertFalse((out / "pages" / "page-0006.png").exists())

    def test_daemon_held_snapshots_stop_clean_calls(self):
        self.env["FAKE_PB_DAEMON_SNAPSHOTS"] = "1"
        out, _ = self.run_capture("--pages", "3")
        cleans = [l for l in json.loads(self.state.read_text())["log"] if l.startswith("clean")]
        self.assertEqual(len(cleans), 1)

    def test_skip_setup_keeps_explicit_setup_menus(self):
        out, _ = self.run_capture("--pages", "1", "--skip-setup", "--setup-menu", "View > Thumbnails")
        log = json.loads(self.state.read_text())["log"]
        self.assertTrue(any("View > Thumbnails" in l for l in log))
        self.assertFalse(any("View > Single Page" in l for l in log))

    def test_refuses_to_overwrite(self):
        out, _ = self.run_capture("--pages", "1")
        _, proc = self.run_capture("--pages", "1", expect=1)
        self.assertIn("already contains a capture", proc.stderr)


if __name__ == "__main__":
    unittest.main()
