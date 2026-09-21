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
                        col_gap_factor=1.2, row_gap_factor=1.0, para_gap_factor=0.6)
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
               "--profile", "preview", "--doc", str(self.doc), "--out", str(out),
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
        self.assertIn("menu item 'View > Hide Sidebar' not present", proc.stderr)
        self.assertFalse((out / ".tmp").exists())
        log = json.loads(self.state.read_text())["log"]
        self.assertTrue(any(l.startswith("clean --snapshot") for l in log))
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

    def test_refuses_to_overwrite(self):
        out, _ = self.run_capture("--pages", "1")
        _, proc = self.run_capture("--pages", "1", expect=1)
        self.assertIn("already contains a capture", proc.stderr)

    def test_accessibility_incomplete_without_vision_is_explained(self):
        self.env["FAKE_PB_AX_INCOMPLETE"] = "1"
        self.env["PATH"] = os.path.dirname(sys.executable)  # python3 only: hides any swiftc
        out, proc = self.run_capture("--pages", "1", expect=1)
        self.assertIn("ACCESSIBILITY_INCOMPLETE", proc.stderr)
        self.assertIn("--ocr-engine vision", proc.stderr)
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "error")


if __name__ == "__main__":
    unittest.main()
