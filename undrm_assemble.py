#!/usr/bin/env python3
"""
undrm_assemble.py - step 3 of the Un-DRM pipeline.

Turns a capture directory written by undrm_capture.py (manifest.json + pages/*.png +
pages/*.json) into readable, searchable output. This step never talks to Peekaboo;
it only reads the saved captures and their OCR JSON.

Outputs (default: written next to the capture, override with --out):

    document.txt    plain text, one form feed between pages
    document.md     Markdown: one section per page with the page image and its text
    document.json   structured lines (text, confidence, bounds) per page
    document.pdf    searchable PDF: the page images with an invisible OCR text layer,
                    so Preview/Acrobat can find, select and copy the recognized text

Reading order is reconstructed from the OCR line boxes with a recursive XY-cut
(columns first, then rows), lines are merged into paragraphs, and end-of-line
hyphenation is undone.

Optional: --reocr re-runs Apple Vision in *accurate* mode on the saved PNGs via
tools/vision-ocr.swift (needs the Swift toolchain). Peekaboo's own `see --ocr`
uses Vision's fast mode, which is good on crisp Retina captures but not the best
Vision can do.

Example:
    ./undrm_assemble.py captures/outline-20260921-101500 --title "Outline"
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
VISION_TOOL_SOURCE = SCRIPT_DIR / "tools" / "vision-ocr.swift"


def vision_build_dir() -> Path:
    """Where the compiled Vision helper lives: next to its source when writable, else a cache dir."""
    candidate = VISION_TOOL_SOURCE.parent
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return candidate
    return Path.home() / "Library" / "Caches" / "undrm"


class UndrmError(Exception):
    pass


def log(msg: str, level: str = "info") -> None:
    prefix = {"info": "  ", "warn": "!!", "error": "xx"}.get(level, "  ")
    print("%s %s" % (prefix, msg), file=sys.stderr, flush=True)


# ------------------------------------------------------------------ geometry helpers


def rect_from_json(value: Any) -> Optional[Dict[str, float]]:
    """Accept {x,y,width,height}, [[x,y],[w,h]] (Swift CGRect) or [x,y,w,h]."""
    if value is None:
        return None
    try:
        if isinstance(value, dict):
            if "x" in value and "width" in value:
                return {"x": float(value["x"]), "y": float(value["y"]),
                        "width": float(value["width"]), "height": float(value["height"])}
            if "origin" in value and "size" in value:
                o, s = value["origin"], value["size"]
                if isinstance(o, dict):
                    return {"x": float(o["x"]), "y": float(o["y"]),
                            "width": float(s["width"]), "height": float(s["height"])}
                return {"x": float(o[0]), "y": float(o[1]), "width": float(s[0]), "height": float(s[1])}
        if isinstance(value, (list, tuple)):
            if len(value) == 2 and all(isinstance(v, (list, tuple)) for v in value):
                return {"x": float(value[0][0]), "y": float(value[0][1]),
                        "width": float(value[1][0]), "height": float(value[1][1])}
            if len(value) == 4:
                return {"x": float(value[0]), "y": float(value[1]),
                        "width": float(value[2]), "height": float(value[3])}
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def size_from_json(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    try:
        if isinstance(value, dict):
            return {"width": float(value["width"]), "height": float(value["height"])}
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return {"width": float(value[0]), "height": float(value[1])}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def is_ocr_element(element: dict) -> bool:
    ident = str(element.get("id") or "")
    desc = str(element.get("description") or "").lower()
    return desc == "ocr" or ident.lower().startswith("ocr_")


# ---------------------------------------------------------------------- data model


class Line:
    """One recognized text line in window-relative logical points (top-left origin)."""

    __slots__ = ("text", "conf", "x0", "y0", "x1", "y1")

    def __init__(self, text: str, conf: float, x: float, y: float, w: float, h: float):
        self.text = text
        self.conf = conf
        self.x0, self.y0 = x, y
        self.x1, self.y1 = x + w, y + h

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def as_json(self) -> dict:
        return {"text": self.text, "confidence": round(self.conf, 3),
                "bbox": {"x": round(self.x0, 2), "y": round(self.y0, 2),
                         "width": round(self.w, 2), "height": round(self.h, 2)}}


class Page:
    def __init__(self, index: int, image: Path, logical: Dict[str, float], lines: List[Line],
                 warnings: List[str], engine: str, duplicate_of: Optional[int] = None):
        self.index = index
        self.image = image
        self.logical = logical
        self.lines = lines
        self.warnings = warnings
        self.engine = engine
        self.duplicate_of = duplicate_of
        self.blocks: List[List[str]] = []   # paragraphs -> lines of text
        self.text = ""


def load_page(entry: dict, capture_dir: Path, manifest: dict, min_conf: float) -> Page:
    image = capture_dir / entry["image"]
    json_path = capture_dir / entry["see_json"]
    with open(json_path) as fh:
        envelope = json.load(fh)
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    ctx = data.get("coordinate_context") or {}
    logical = (rect_from_json(ctx.get("logical_bounds")) or rect_from_json(entry.get("logical_bounds"))
               or rect_from_json((manifest.get("window") or {}).get("bounds")))
    if not logical:
        raise UndrmError("page %s: no logical bounds in %s" % (entry.get("index"), json_path))
    lines: List[Line] = []
    for element in data.get("ui_elements") or []:
        if not is_ocr_element(element):
            continue
        text = (element.get("label") or "").strip()
        conf = float(element.get("confidence") or 0.0)
        box = rect_from_json(element.get("bounds"))
        if not text or not box or conf < min_conf:
            continue
        if box["width"] <= 0 or box["height"] <= 0:
            continue
        lines.append(Line(text, conf, box["x"] - logical["x"], box["y"] - logical["y"],
                          box["width"], box["height"]))
    engine = entry.get("engine") or data.get("ocr_engine") or "peekaboo"
    return Page(int(entry["index"]), image, logical, lines, list(entry.get("warnings") or []),
                engine, entry.get("duplicate_of"))


# ------------------------------------------------------------------- reading order


def median(values: Sequence[float], default: float) -> float:
    vals = [v for v in values if v > 0]
    return statistics.median(vals) if vals else default


def _largest_gap(items: List[Line], axis: str, min_gap: float) -> Optional[int]:
    """Index i such that items[:i] and items[i:] are separated by the widest projection gap."""
    if axis == "x":
        items.sort(key=lambda l: (l.x0, l.x1))
        lo, hi = (lambda l: l.x0), (lambda l: l.x1)
    else:
        items.sort(key=lambda l: (l.y0, l.y1))
        lo, hi = (lambda l: l.y0), (lambda l: l.y1)
    best: Optional[Tuple[float, int]] = None
    reach = hi(items[0])
    for i in range(1, len(items)):
        gap = lo(items[i]) - reach
        if gap >= min_gap and (best is None or gap > best[0]):
            best = (gap, i)
        reach = max(reach, hi(items[i]))
    return best[1] if best else None


def segment(lines: List[Line], col_gap: float, row_gap: float, med_h: float,
            depth: int = 0) -> List[List[Line]]:
    """Recursive XY-cut: split on the widest vertical gap (columns), else the widest
    horizontal gap (rows); leaves are returned in reading order.

    A vertical cut is only accepted when both sides are plausible text columns
    (wider than ~8 line heights and 20% of the block); otherwise a column of list
    markers, speaker names or table-of-contents page numbers would be peeled off
    the text it belongs to."""
    if len(lines) <= 1 or depth > 40:
        return [lines]
    items = list(lines)
    i = _largest_gap(items, "x", col_gap)
    if i is not None and i >= 2 and len(items) - i >= 2:
        block_w = max(l.x1 for l in items) - min(l.x0 for l in items)
        min_ext = max(8.0 * med_h, 0.2 * block_w)
        ext_left = max(l.x1 for l in items[:i]) - min(l.x0 for l in items[:i])
        ext_right = max(l.x1 for l in items[i:]) - min(l.x0 for l in items[i:])
        if min(ext_left, ext_right) >= min_ext:
            return (segment(items[:i], col_gap, row_gap, med_h, depth + 1)
                    + segment(items[i:], col_gap, row_gap, med_h, depth + 1))
    i = _largest_gap(items, "y", row_gap)
    if i is not None:
        return (segment(items[:i], col_gap, row_gap, med_h, depth + 1)
                + segment(items[i:], col_gap, row_gap, med_h, depth + 1))
    return [items]


def merge_rows(lines: List[Line]) -> List[List[Line]]:
    """Group fragments that share a baseline into rows, left to right.

    Membership is by vertical overlap of the boxes (at least half of the smaller
    box), so superscripts and other small fragments stay with their line."""
    rows: List[List[Line]] = []
    for line in sorted(lines, key=lambda l: (l.yc, l.x0)):
        placed = False
        for row in rows:
            ref = row[-1]
            small = min(ref.h, line.h)
            overlap = min(ref.y1, line.y1) - max(ref.y0, line.y0)
            same_row = (overlap >= 0.5 * small) if small > 0 else (abs(ref.yc - line.yc) <= 2.0)
            if same_row:
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda l: l.x0)
    rows.sort(key=lambda r: statistics.median([l.yc for l in r]))
    return rows


HYPHEN_END = re.compile(r"(\w)[-‐‑]$")


def join_paragraph(texts: List[str], dehyphenate: bool = True) -> str:
    out = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if not out:
            out = text
            continue
        if dehyphenate and HYPHEN_END.search(out) and text[:1].islower():
            out = out[:-1] + text
        else:
            out += " " + text
    return out


def layout_page(page: Page, keep_lines: bool, xy_cut: bool, dehyphenate: bool,
                col_gap_factor: float, row_gap_factor: float, para_gap_factor: float) -> None:
    lines = page.lines
    if not lines:
        page.blocks, page.text = [], ""
        return
    med_h = median([l.h for l in lines], 12.0)
    col_gap = max(8.0, col_gap_factor * med_h)
    row_gap = max(6.0, row_gap_factor * med_h)
    groups = segment(lines, col_gap, row_gap, med_h) if xy_cut else [lines]
    blocks: List[List[str]] = []
    for group in groups:
        rows = merge_rows(group)
        row_texts = [" ".join(l.text for l in row) for row in rows]
        row_tops = [min(l.y0 for l in row) for row in rows]
        row_bottoms = [max(l.y1 for l in row) for row in rows]
        row_lefts = [min(l.x0 for l in row) for row in rows]
        pitches = [row_tops[i] - row_tops[i - 1] for i in range(1, len(rows))]
        pitch = median(pitches, med_h * 1.3)
        left_edge = min(row_lefts)
        indented = [rl - left_edge > 1.0 * med_h for rl in row_lefts]
        flush = [rl - left_edge <= 0.3 * med_h for rl in row_lefts]
        n_ind, n_fl = sum(indented), sum(flush)
        # Hanging indent (bibliographies, outlines, wrapped list items): indented rows are
        # continuations and each flush row starts an entry. Ties go by the first row: a
        # group that opens flush is hanging, one that opens indented uses first-line indents.
        hanging = n_ind > n_fl or (n_ind == n_fl and n_ind > 0 and flush[0])
        current: List[str] = []
        for i, text in enumerate(row_texts):
            new_para = False
            if i > 0:
                gap = row_tops[i] - row_bottoms[i - 1]
                if gap > para_gap_factor * pitch or row_tops[i] - row_tops[i - 1] > 1.8 * pitch:
                    new_para = True
                elif hanging:
                    new_para = flush[i]
                elif indented[i] and flush[i - 1]:
                    new_para = True
            if new_para and current:
                blocks.append(current)
                current = []
            current.append(text)
        if current:
            blocks.append(current)
    page.blocks = blocks
    if keep_lines:
        page.text = "\n\n".join("\n".join(b) for b in blocks)
    else:
        page.text = "\n\n".join(join_paragraph(b, dehyphenate) for b in blocks)


# ---------------------------------------------------------------- optional re-OCR


def reocr_with_vision(page: Page, min_conf: float, languages: Optional[List[str]], build_dir: Path) -> None:
    swiftc = shutil.which("swiftc")
    if not swiftc or not VISION_TOOL_SOURCE.exists():
        raise UndrmError("--reocr needs tools/vision-ocr.swift and the Swift toolchain (xcode-select --install)")
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "vision-ocr"
    if not binary.exists() or binary.stat().st_mtime < VISION_TOOL_SOURCE.stat().st_mtime:
        log("compiling vision-ocr (one-time)")
        proc = subprocess.run([swiftc, "-O", "-o", str(binary), str(VISION_TOOL_SOURCE)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise UndrmError("swiftc failed:\n" + proc.stderr)
    cmd = [str(binary)]
    if languages:
        cmd += ["--languages", ",".join(languages)]
    cmd.append(str(page.image))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise UndrmError("vision-ocr failed on %s: %s" % (page.image, proc.stderr.strip()))
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    lw, lh = page.logical["width"], page.logical["height"]
    lines: List[Line] = []
    for obs in result.get("observations") or []:
        conf = float(obs.get("confidence") or 0.0)
        text = (obs.get("text") or "").strip()
        box = obs.get("bbox") or {}
        if not text or conf < min_conf:
            continue
        lines.append(Line(text, conf, float(box.get("x", 0)) * lw, float(box.get("y", 0)) * lh,
                          float(box.get("width", 0)) * lw, float(box.get("height", 0)) * lh))
    page.lines = lines
    page.engine = "vision-accurate"


# ------------------------------------------------------------------------ writers


def write_txt(pages: List[Page], path: Path, title: str) -> None:
    parts = []
    for page in pages:
        parts.append("%s\n" % page.text.rstrip() if page.text.strip() else "[page %d: no text recognized]\n" % page.index)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(title + "\n\n")
        fh.write("\f\n".join(parts))


def write_md(pages: List[Page], path: Path, title: str, manifest: dict, image_base: Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# %s\n\n" % title)
        app = (manifest.get("app") or {}).get("name") or "a document viewer"
        fh.write("_Captured from %s on %s with Peekaboo (%d pages)._\n\n"
                 % (app, (manifest.get("started") or "")[:10], len(pages)))
        for page in pages:
            fh.write("## Page %d\n\n" % page.index)
            rel = os.path.relpath(page.image, image_base).replace(os.sep, "/")
            fh.write("![Page %d](%s)\n\n" % (page.index, rel.replace(" ", "%20")))
            if page.duplicate_of:
                fh.write("_Identical to page %d._\n\n" % page.duplicate_of)
            if page.warnings:
                fh.write("> Capture notes: %s\n\n" % "; ".join(page.warnings))
            if page.text.strip():
                fh.write(page.text.rstrip() + "\n\n")
            else:
                fh.write("_No text recognized on this page._\n\n")


def write_json(pages: List[Page], path: Path, title: str, manifest: dict) -> None:
    doc = {
        "title": title,
        "source": manifest.get("document"),
        "app": manifest.get("app"),
        "captured": manifest.get("started"),
        "page_count": len(pages),
        "pages": [{
            "index": p.index,
            "image": str(p.image),
            "engine": p.engine,
            "duplicate_of": p.duplicate_of,
            "warnings": p.warnings,
            "logical_size": {"width": p.logical["width"], "height": p.logical["height"]},
            "text": p.text,
            "paragraphs": [" ".join(b) for b in p.blocks],
            "lines": [l.as_json() for l in p.lines],
        } for p in pages],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)


# --------------------------------------------------------------------- PDF writer

# Helvetica glyph widths (per 1000 em) for WinAnsi codes 32..126; used to size the
# invisible text layer so search highlights and selections line up with the image.
HELVETICA_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]


def helvetica_width(data: bytes) -> float:
    total = 0
    for b in data:
        total += HELVETICA_WIDTHS[b - 32] if 32 <= b <= 126 else 556
    return total / 1000.0


def _winansi_encodable(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def pdf_string(text: str) -> bytes:
    data = text.encode("cp1252", errors="replace")
    data = bytes(b for b in data if b >= 32 or b in (9,))
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def pdf_text_string(text: str) -> bytes:
    try:
        text.encode("ascii")
        return pdf_string(text)
    except UnicodeEncodeError:
        return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def jpeg_info(data: bytes) -> Tuple[int, int, int]:
    """(width, height, components) from the JPEG SOF marker."""
    if data[:2] != b"\xff\xd8":
        raise UndrmError("not a JPEG file")
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte before a marker
            i += 1
            continue
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            comps = data[i + 9]
            return width, height, comps
        i += 2 + seg_len
    raise UndrmError("no SOF marker found in JPEG")


def png_info(data: bytes) -> Tuple[int, int, int, int, int]:
    """(width, height, bit_depth, color_type, interlace) from the IHDR chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise UndrmError("not a PNG file")
    width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    return width, height, depth, ctype, interlace


def png_idat(data: bytes) -> bytes:
    pos = 8
    chunks = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if ctype == b"IDAT":
            chunks.append(data[pos + 8:pos + 8 + length])
        elif ctype == b"IEND":
            break
        pos += 12 + length
    return b"".join(chunks)


def flatten_with_pillow(png: Path):
    """Open a PNG with Pillow and composite any transparency onto white. Returns an RGB image."""
    from PIL import Image  # type: ignore
    with Image.open(png) as im:
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            return flat
        return im.convert("RGB")


def convert_to_jpeg(png: Path, out: Path, quality: int) -> None:
    sips = shutil.which("sips")
    if sips:
        proc = subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                               str(png), "--out", str(out)], capture_output=True, text=True)
        if proc.returncode == 0 and out.exists():
            return
        log("sips failed (%s); trying Pillow" % proc.stderr.strip(), "warn")
    try:
        flat = flatten_with_pillow(png)
    except ImportError:
        raise UndrmError("cannot convert %s to JPEG: neither sips (macOS) nor Pillow is available. "
                         "Use --pdf-image png or --no-pdf." % png.name)
    flat.save(out, "JPEG", quality=quality)


class PDFImage:
    def __init__(self, width: int, height: int, colorspace: str, filter_name: str,
                 data: bytes, decode_parms: Optional[bytes] = None):
        self.width, self.height = width, height
        self.colorspace, self.filter_name = colorspace, filter_name
        self.data, self.decode_parms = data, decode_parms


def load_pdf_image(png: Path, mode: str, quality: int, work_dir: Path) -> PDFImage:
    raw = png.read_bytes()
    if mode == "png":
        width, height, depth, ctype, interlace = png_info(raw)
        if not (depth == 8 and interlace == 0 and ctype in (0, 2)):
            # Window captures usually carry an alpha channel; flatten to RGB losslessly when
            # Pillow is available so the PNG path stays lossless.
            try:
                flat = flatten_with_pillow(png)
                work_dir.mkdir(parents=True, exist_ok=True)
                flat_png = work_dir / (png.stem + "-rgb.png")
                flat.save(flat_png, "PNG")
                raw = flat_png.read_bytes()
                width, height, depth, ctype, interlace = png_info(raw)
            except ImportError:
                pass
        if depth == 8 and interlace == 0 and ctype in (0, 2):
            colors = 3 if ctype == 2 else 1
            parms = b"<< /Predictor 15 /Colors %d /BitsPerComponent 8 /Columns %d >>" % (colors, width)
            return PDFImage(width, height, "/DeviceRGB" if colors == 3 else "/DeviceGray",
                            "/FlateDecode", png_idat(raw), parms)
        log("%s is not 8-bit RGB/gray PNG (type %d) and Pillow is not available to flatten it; "
            "embedding as JPEG instead" % (png.name, ctype), "warn")
    work_dir.mkdir(parents=True, exist_ok=True)
    jpg = work_dir / (png.stem + ".jpg")
    convert_to_jpeg(png, jpg, quality)
    data = jpg.read_bytes()
    width, height, comps = jpeg_info(data)
    cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(comps)
    if cs is None:
        raise UndrmError("unsupported JPEG component count %d in %s" % (comps, jpg))
    return PDFImage(width, height, cs, "/DCTDecode", data)


class PDFWriter:
    def __init__(self) -> None:
        self.objects: List[bytes] = []

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def reserve(self) -> int:
        self.objects.append(b"")
        return len(self.objects)

    def set(self, obj_id: int, body: bytes) -> None:
        self.objects[obj_id - 1] = body

    @staticmethod
    def stream(dictionary: bytes, data: bytes) -> bytes:
        return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"

    def build(self, root_id: int, info_id: int) -> bytes:
        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(self.objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n" % (len(self.objects) + 1)
        out += b"0000000000 65535 f \n"
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(self.objects) + 1, root_id, info_id, xref)
        return bytes(out)


def write_pdf(pages: List[Page], path: Path, title: str, page_width: float, image_mode: str,
              quality: int, work_dir: Path, visible_text: bool = False) -> None:
    pdf = PDFWriter()
    catalog_id = pdf.reserve()
    pages_id = pdf.reserve()
    font_id = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: List[int] = []
    unencodable = 0
    for page in pages:
        lw, lh = page.logical["width"], page.logical["height"]
        if lw <= 0 or lh <= 0:
            continue
        scale = page_width / lw
        pw, ph = page_width, lh * scale
        image = load_pdf_image(page.image, image_mode, quality, work_dir)
        img_dict = b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter %s" % (
            image.width, image.height, image.colorspace.encode(), image.filter_name.encode())
        if image.decode_parms:
            img_dict += b" /DecodeParms " + image.decode_parms
        img_id = pdf.add(PDFWriter.stream(img_dict, image.data))
        content = bytearray()
        content += b"q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q\n" % (pw, ph)
        content += b"BT %d Tr\n" % (0 if visible_text else 3)
        if visible_text:
            content += b"1 0 0 rg\n"
        for line in page.lines:
            fs = max(1.0, line.h * scale)
            unencodable += sum(1 for ch in line.text if not _winansi_encodable(ch))
            encoded = pdf_string(line.text)
            natural = helvetica_width(line.text.encode("cp1252", errors="replace")) * fs
            box_w = max(0.5, line.w * scale)
            tz = 100.0 * box_w / natural if natural > 0 else 100.0
            tz = min(max(tz, 10.0), 600.0)
            x = line.x0 * scale
            y = ph - line.y1 * scale + 0.22 * fs
            content += b"/F1 %.2f Tf %.2f Tz 1 0 0 1 %.2f %.2f Tm %s Tj\n" % (fs, tz, x, y, encoded)
        content += b"ET\n"
        content_id = pdf.add(PDFWriter.stream(b"", bytes(content)))
        page_id = pdf.add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.4f %.4f] /Resources << /Font << /F1 %d 0 R >> /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>" % (
            pages_id, pw, ph, font_id, img_id, content_id))
        page_ids.append(page_id)
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    pdf.set(pages_id, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    pdf.set(catalog_id, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    if unencodable:
        log("%d character(s) outside WinAnsi (e.g. Greek, math symbols, CJK) were written as '?' in the "
            "PDF text layer; document.txt/.md/.json keep them" % unencodable, "warn")
    now = dt.datetime.now().strftime("D:%Y%m%d%H%M%S")
    info_id = pdf.add(b"<< /Title " + pdf_text_string(title) + b" /Producer (Un-DRM undrm_assemble.py) /Creator (Peekaboo see --ocr) /CreationDate (" + now.encode() + b") >>")
    path.write_bytes(pdf.build(catalog_id, info_id))


# ------------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("capture_dir", help="directory written by undrm_capture.py (contains manifest.json)")
    p.add_argument("--out", help="output directory (default: the capture directory)")
    p.add_argument("--name", default="document", help="base name for output files (default: document)")
    p.add_argument("--title", help="document title (default: from the manifest)")
    p.add_argument("--formats", default="txt,md,json,pdf", help="comma list of txt,md,json,pdf (default: all)")
    p.add_argument("--no-pdf", action="store_true", help="skip the PDF")
    p.add_argument("--min-confidence", type=float, default=0.0, help="drop OCR lines below this confidence (0-1)")
    p.add_argument("--keep-lines", action="store_true", help="keep OCR line breaks instead of reflowing paragraphs")
    p.add_argument("--no-dehyphenate", action="store_true", help="do not join words hyphenated across lines")
    p.add_argument("--no-xy-cut", action="store_true", help="disable column detection; order lines top to bottom only")
    p.add_argument("--col-gap", type=float, default=0.8, help="column gap threshold in line heights (default 0.8; lower it for tight gutters)")
    p.add_argument("--row-gap", type=float, default=1.0, help="row split threshold in line heights (default 1.0)")
    p.add_argument("--para-gap", type=float, default=0.6, help="paragraph break threshold in line pitches (default 0.6)")
    p.add_argument("--pdf-page-width", type=float, default=612.0, help="PDF page width in points (default 612 = 8.5in)")
    p.add_argument("--pdf-image", choices=["jpeg", "png"], default="jpeg", help="how to embed page images: jpeg via sips/Pillow (default) or lossless png (captures with an alpha channel are flattened with Pillow first)")
    p.add_argument("--pdf-quality", type=int, default=85, help="JPEG quality for the PDF (default 85)")
    p.add_argument("--pdf-visible-text", action="store_true", help="debug: draw the OCR text layer visibly in red")
    p.add_argument("--reocr", action="store_true", help="re-run Apple Vision in accurate mode on the PNGs (needs swiftc)")
    p.add_argument("--languages", type=lambda s: [x for x in s.split(",") if x], help="languages for --reocr, e.g. en-US")
    p.add_argument("--pages", help="only these pages, e.g. 1-10,15")
    return p


def parse_page_selection(spec: Optional[str]) -> Optional[set]:
    if not spec:
        return None
    chosen: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            chosen.update(range(int(a), int(b) + 1))
        else:
            chosen.add(int(part))
    return chosen


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        capture_dir = Path(args.capture_dir).expanduser().resolve()
        manifest_path = capture_dir / "manifest.json"
        if not manifest_path.exists():
            raise UndrmError("no manifest.json in %s (run undrm_capture.py first)" % capture_dir)
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        entries = manifest.get("pages") or []
        if not entries:
            raise UndrmError("the manifest lists no pages")
        selection = parse_page_selection(args.pages)
        out_dir = Path(args.out).expanduser().resolve() if args.out else capture_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir = out_dir / ".assemble"
        title = args.title or manifest.get("title") or capture_dir.name
        formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
        if args.no_pdf:
            formats.discard("pdf")

        pages: List[Page] = []
        for entry in entries:
            if selection and int(entry["index"]) not in selection:
                continue
            page = load_page(entry, capture_dir, manifest, args.min_confidence)
            if args.reocr:
                log("re-OCR page %d" % page.index)
                reocr_with_vision(page, max(args.min_confidence, 0.3), args.languages, vision_build_dir())
            layout_page(page, args.keep_lines, not args.no_xy_cut, not args.no_dehyphenate,
                        args.col_gap, args.row_gap, args.para_gap)
            pages.append(page)
        if not pages:
            raise UndrmError("no pages selected")
        log("%d page(s), %d recognized lines, %d characters" % (
            len(pages), sum(len(p.lines) for p in pages), sum(len(p.text) for p in pages)))
        empty = [p.index for p in pages if not p.text.strip()]
        if empty:
            log("pages with no recognized text: %s" % ", ".join(str(i) for i in empty), "warn")

        base = out_dir / args.name
        if "txt" in formats:
            write_txt(pages, base.with_suffix(".txt"), title)
            log("wrote %s" % base.with_suffix(".txt"))
        if "md" in formats:
            write_md(pages, base.with_suffix(".md"), title, manifest, out_dir)
            log("wrote %s" % base.with_suffix(".md"))
        if "json" in formats:
            write_json(pages, base.with_suffix(".json"), title, manifest)
            log("wrote %s" % base.with_suffix(".json"))
        if "pdf" in formats:
            write_pdf(pages, base.with_suffix(".pdf"), title, args.pdf_page_width, args.pdf_image,
                      args.pdf_quality, work_dir, args.pdf_visible_text)
            log("wrote %s" % base.with_suffix(".pdf"))
        shutil.rmtree(work_dir, ignore_errors=True)
        return 0
    except UndrmError as exc:
        log(str(exc), "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
