#!/usr/bin/env python3
"""
A stand-in for the `peekaboo` CLI used by the test-suite (and handy for dry runs on
non-Mac machines). It emulates just enough of Peekaboo 4.1+'s JSON envelopes for
undrm_capture.py: a viewer app ("Preview") showing a six-page document.

Simulated behavior:
  * the first probe after a page turn returns a transition frame (animation);
    FAKE_PB_TRANSITION_FRAMES=n changes how many
  * page 5 is a blank page identical to page 4 (exercises --end-retries)
  * background `press` is refused with a pre-dispatch refusal envelope unless
    FAKE_PB_ALLOW_BACKGROUND_PRESS=1 (exercises the foreground fallback)
  * every `see` publishes a snapshot id that `clean --snapshot` must remove; unknown
    ids answer not_found like the real CLI. FAKE_PB_DAEMON_SNAPSHOTS=1 answers
    not_found for every id (daemon-held snapshots)
  * FAKE_PB_NO_AX=1: the viewer exposes no accessibility elements, so `see --ocr`
    on a page with no recognizable text fails with ACCESSIBILITY_INCOMPLETE after
    writing the PNG (Peekaboo's real semantics for blank pages)
  * FAKE_PB_NOISE=1: every capture differs by a trailer, like a window that never
    settles

State lives in $FAKE_PB_STATE (JSON); rendered pages go next to it.
"""
from __future__ import annotations

import json
import os
import sys
import hashlib
from pathlib import Path

STATE_PATH = Path(os.environ.get("FAKE_PB_STATE", "/tmp/fake-peekaboo-state.json"))
RENDER_DIR = STATE_PATH.parent / "fake-peekaboo-pages"
WINDOW_ID = 777
PID = 4242
WIN_W, WIN_H = 1000, 1300
WIN_X, WIN_Y = 100, 80

PAGES = [
    {"title": "The Rule Against Perpetuities", "columns": [[
        "No interest is good unless it must vest, if at all, not later than",
        "twenty-one years after some life in being at the creation of the",
        "interest. The rule applies to contingent remainders, executory",
        "interests, and vested remainders subject to open.",
        "",
        "California has adopted the Uniform Statutory Rule Against",
        "Perpetuities, which adds a ninety-year wait-and-see period.",
    ]]},
    {"title": "Two Column Page", "columns": [[
        "Left column first line about the",
        "first topic that continues here",
        "and ends on this line.",
        "",
        "Second left paragraph starts",
        "after a gap.",
    ], [
        "Right column begins after the",
        "left one is finished, and the",
        "reader should get it last.",
    ]]},
    {"title": "Hyphenation", "columns": [[
        "The court gave careful consider-",
        "ation to the question of whether",
        "the contract was supported by con-",
        "sideration. It was.",
    ]]},
    {"title": "", "columns": [[]]},
    {"title": "", "columns": [[]]},
    {"title": "The End", "columns": [["This is the last page of the document."]]},
]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"launched": False, "page": 1, "transition": 0,
            "bounds": {"x": WIN_X, "y": WIN_Y, "width": 800, "height": 600}, "doc": None,
            "log": [], "snapshots": [], "see_calls": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def out(data, success=True, error=None, extra=None):
    envelope = {"success": success, "data": data, "debug_logs": []}
    if error:
        envelope["error"] = error
    if extra:
        envelope.update(extra)
    print(json.dumps(envelope))
    sys.exit(0 if success else 1)


def fail(code, message, hint=None, refused=False):
    error = {"code": code, "message": message, "hint": hint}
    extra = None
    if refused:
        error.update({"mutation_dispatched": False, "retry_safe": True})
        extra = {"effect": "refused"}
    out(None, success=False, error=error, extra=extra)


def publish_snapshot(state, seed):
    snap = snapshot_id(seed)
    state.setdefault("snapshots", []).append(snap)
    save_state(state)
    return snap


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def layout(page_no: int):
    """Return (lines, positions): text lines and their local boxes."""
    page = PAGES[page_no - 1]
    boxes = []
    margin, top, lh, fs = 80, 120, 34, 22
    if page["title"]:
        boxes.append((page["title"], margin, top, 30 * len(page["title"]) * 0.55, 34))
    ncols = len(page["columns"])
    col_w = (WIN_W - 2 * margin - (ncols - 1) * 60) / ncols
    for c, column in enumerate(page["columns"]):
        x = margin + c * (col_w + 60)
        y = top + 80
        for text in column:
            if text:
                boxes.append((text, x, y, min(col_w, fs * 0.55 * len(text)), fs + 6))
            y += lh
    return boxes


def is_blank(page_no: int) -> bool:
    page = PAGES[page_no - 1]
    return not page["title"] and not any(page["columns"])


def render(page_no: int, transition: bool = False) -> bytes:
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    cache = RENDER_DIR / ("page-%d%s.png" % (page_no, "-transition" if transition else ""))
    if cache.exists():
        return cache.read_bytes()
    from PIL import Image, ImageDraw, ImageFont  # test dependency only
    img = Image.new("RGB", (WIN_W, WIN_H), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 22)
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 30)
    except OSError:
        font = title_font = ImageFont.load_default()
    for i, (text, x, y, w, h) in enumerate(layout(page_no)):
        draw.text((x, y), text, fill="black", font=title_font if (i == 0 and PAGES[page_no - 1]["title"]) else font)
    if is_blank(page_no):
        pass  # truly blank pages carry no page number, so consecutive blanks look identical
    else:
        draw.text((WIN_W / 2 - 10, WIN_H - 60), str(page_no), fill="gray", font=font)
    if transition:
        draw.rectangle([0, WIN_H // 2, WIN_W, WIN_H // 2 + 40], fill="lightgray")
    img.save(cache, "PNG")
    return cache.read_bytes()


def ocr_elements(page_no: int, bounds):
    elements = [{"id": "G1", "role": "group", "ax_role": "AXGroup", "label": None, "bounds":
                 {"x": bounds["x"], "y": bounds["y"], "width": bounds["width"], "height": bounds["height"]},
                 "is_actionable": False},
                {"id": "B1", "role": "button", "label": "Zoom", "bounds": {"x": bounds["x"] + 10, "y": bounds["y"] + 10, "width": 40, "height": 20}, "is_actionable": True}]
    sx = bounds["width"] / WIN_W
    sy = bounds["height"] / WIN_H
    n = 0
    boxes = layout(page_no)
    # emit out of order to make sure the assembler sorts
    for text, x, y, w, h in reversed(boxes):
        n += 1
        elements.append({
            "id": "ocr_%d" % n, "role": "staticText", "ax_role": None, "title": None, "label": text,
            "value": None, "description": "ocr", "confidence": 0.93,
            "bounds": {"x": bounds["x"] + x * sx, "y": bounds["y"] + y * sy, "width": w * sx, "height": h * sy},
            "is_actionable": False, "is_enabled": True,
        })
    if is_blank(page_no):
        return elements
    n += 1
    elements.append({"id": "ocr_%d" % n, "role": "staticText", "label": str(page_no), "description": "ocr",
                     "confidence": 0.5, "bounds": {"x": bounds["x"] + (WIN_W / 2 - 10) * sx, "y": bounds["y"] + (WIN_H - 60) * sy,
                                                   "width": 20 * sx, "height": 26 * sy}, "is_actionable": False})
    return elements


def snapshot_id(seed: str) -> str:
    return "ps1_" + hashlib.md5(seed.encode()).hexdigest()


def main(argv):
    state = load_state()
    if not argv:
        print("usage: peekaboo <command>")
        return 2
    if argv[0] == "--version":
        print("Peekaboo 4.4.0 (fake)")
        return 0
    cmd = argv[0]
    args = argv[1:]
    sub = args[0] if args and not args[0].startswith("-") else None
    state.setdefault("log", []).append(" ".join(argv))
    save_state(state)

    if cmd == "see" and "--help" in args:
        print("OPTIONS:\n  --ocr   Add host-local Vision OCR text to the accessibility element map\n")
        return 0

    if cmd == "permissions":
        out({"source": "local", "permissions": [
            {"name": "Screen Recording", "isRequired": True, "isGranted": True, "grantInstructions": ""},
            {"name": "Accessibility", "isRequired": True, "isGranted": True, "grantInstructions": ""},
            {"name": "Event Synthesizing", "isRequired": False, "isGranted": False, "grantInstructions": ""}]})

    if cmd == "screen":
        out({"screens": [{"index": 0, "name": "Built-in Display", "resolution": {"width": 1728, "height": 1117},
                          "position": {"x": 0, "y": 0}, "bounds": {"x": 0, "y": 0, "width": 1728, "height": 1117},
                          "visibleArea": {"width": 1728, "height": 1080}, "isPrimary": True, "scaleFactor": 2,
                          "displayID": 1}], "primaryIndex": 0})

    if cmd == "app":
        if sub == "list":
            apps = []
            if state["launched"]:
                apps.append({"name": "Preview", "bundle_id": "com.apple.Preview", "pid": PID, "is_active": True})
            out({"count": len(apps), "apps": apps, "warnings": [], "schema_capabilities": ["processStartIdentityDecimal"]})
        if sub == "launch":
            if "--foreground" not in args and "--open" in args:
                fail("INTERACTION_FAILED", "Background URL or document delivery is refused before dispatch")
            state["launched"] = True
            state["doc"] = opt(args, "--open")
            state["page"] = 1
            save_state(state)
            out({"action": "launch", "app_name": "Preview", "bundle_id": "com.apple.Preview", "pid": PID,
                 "is_ready": True, "window_count": 1, "window_ready": True, "window_ids": [WINDOW_ID],
                 "window_identity": "exact", "new_instance": False}, extra={"effect": "confirmed"})
        fail("INVALID_INPUT", "unsupported app subcommand")

    if cmd == "window":
        if not state["launched"]:
            fail("APP_NOT_FOUND", "Application not running")
        title = Path(state["doc"]).name if state.get("doc") else "Untitled"
        if sub == "list":
            out({"windows": [{"window_title": title, "window_id": WINDOW_ID, "window_index": 0,
                              "bounds": state["bounds"], "is_on_screen": True, "is_frontmost": True, "is_key": True,
                              "layer": 0, "observation_capability": "combined_eligible"}],
                 "target_application_info": {"app_name": "Preview", "bundle_id": "com.apple.Preview", "pid": PID},
                 "inventory_completeness": "complete", "inventory_warnings": []})
        if sub == "set-bounds":
            state["bounds"] = {"x": int(opt(args, "-x")), "y": int(opt(args, "-y")),
                               "width": int(opt(args, "-w")), "height": int(opt(args, "--height"))}
            save_state(state)
            out({"action": "set-bounds", "app_name": "Preview", "window_title": title, "new_bounds": state["bounds"]},
                extra={"effect": "confirmed"})
        if sub == "focus":
            out({"action": "focus", "app_name": "Preview", "window_title": title}, extra={"effect": "confirmed"})
        fail("INVALID_INPUT", "unsupported window subcommand")

    if cmd == "menu":
        if sub == "list":
            out({"app": "Preview", "menu_structure": [
                {"title": "View", "enabled": True, "items": [
                    {"title": "Content Only", "enabled": True, "shortcut": "⌥⌘1"},
                    {"title": "Thumbnails", "enabled": True, "shortcut": "⌥⌘2"},
                    {"title": "Continuous Scroll", "enabled": True},
                    {"title": "Single Page", "enabled": True},
                    {"title": "Two Pages", "enabled": True},
                    {"title": "Zoom to Fit", "enabled": True, "shortcut": "⌘9"}]},
                {"title": "Go", "enabled": True, "items": [
                    {"title": "Back", "enabled": True},
                    {"title": "Next Page", "enabled": True, "shortcut": "→"},
                    {"title": "Previous Page", "enabled": True}]}]})
        if sub == "click":
            path = opt(args, "--path") or opt(args, "--item")
            if path in ("View > Single Page", "View > Zoom to Fit", "View > Continuous Scroll",
                        "View > Content Only", "View > Thumbnails"):
                out({"action": "menu_click", "app": "Preview", "menu_path": path, "clicked_item": path.split(">")[-1].strip()},
                    extra={"effect": "confirmed"})
            if path == "Go > Next Page":
                if state["page"] < len(PAGES):
                    state["page"] += 1
                    state["transition"] = int(os.environ.get("FAKE_PB_TRANSITION_FRAMES", "1"))
                    save_state(state)
                out({"action": "menu_click", "app": "Preview", "menu_path": path, "clicked_item": "Next Page"},
                    extra={"effect": "confirmed"})
            fail("MENU_ITEM_NOT_FOUND", "Menu item not found: %s" % path)

    if cmd == "press":
        if "--foreground" not in args and os.environ.get("FAKE_PB_ALLOW_BACKGROUND_PRESS") != "1":
            fail("INTERACTION_FAILED", "This automation host does not support focused exact-window background hotkeys.",
                 "Update the Peekaboo host and retry with a fresh exact-window target.", refused=True)
        key = args[0].lower()
        if key in ("right", "down", "pagedown", "space"):
            if state["page"] < len(PAGES):
                state["page"] += 1
                state["transition"] = int(os.environ.get("FAKE_PB_TRANSITION_FRAMES", "1"))
                save_state(state)
        out({"keys": [key], "totalPresses": 1, "count": 1, "deliveryMode": "foreground" if "--foreground" in args else "background",
             "targetPID": PID, "executionTime": 0.01}, extra={"effect": "unverifiable"})

    if cmd == "see":
        if not state["launched"]:
            fail("APP_NOT_FOUND", "Application not running")
        path = opt(args, "--path")
        if not path:
            fail("INVALID_INPUT", "--path required in the fake")
        bounds = state["bounds"]
        transition = state.get("transition", 0) > 0
        if transition:
            state["transition"] -= 1
            save_state(state)
        png = render(state["page"], transition)
        state["see_calls"] = state.get("see_calls", 0) + 1
        save_state(state)
        if os.environ.get("FAKE_PB_NOISE") == "1":
            png += b"\x00" + str(state["see_calls"]).encode()  # trailing bytes after IEND: unique, still decodable
        Path(path).write_bytes(png)
        logical = [[bounds["x"], bounds["y"]], [bounds["width"], bounds["height"]]]  # Swift CGRect encoding
        if "--no-elements" in args:
            out({"files": [{"path": path, "window_title": "doc", "window_id": WINDOW_ID, "window_index": 0, "mime_type": "image/png"}],
                 "observations": [{"spans": [], "warnings": [], "coordinates": {
                     "coordinate_space": "global_display_points", "logical_bounds": logical,
                     "image_size_pixels": {"width": WIN_W, "height": WIN_H}, "scale_factor": 1}}],
                 "snapshot_id": publish_snapshot(state, path + str(state["see_calls"]))})
        if "--ocr" in args:
            elements = ocr_elements(state["page"], bounds)
            if os.environ.get("FAKE_PB_NO_AX") == "1":
                elements = [e for e in elements if e.get("description") == "ocr"]
                if not elements:
                    # Real Peekaboo: no AX elements and no OCR text -> evidence policy refuses after
                    # writing the raster to --path.
                    fail("ACCESSIBILITY_INCOMPLETE", "Exact window %d returned no usable Accessibility elements." % WINDOW_ID)
            out({"snapshot_id": publish_snapshot(state, path + "ocr" + str(state["see_calls"])), "snapshot_reusable": True, "semantic_scope": "exact_or_requested",
                 "mutation_targeting_available": True, "screenshot_raw": path, "screenshot_annotated": "",
                 "ui_map": "/tmp/ui_map.json", "application_name": "Preview", "window_title": "doc", "is_dialog": False,
                 "element_count": 0, "interactable_count": 1, "capture_mode": "window", "execution_time": 0.4,
                 "ui_elements": elements,
                 "observation": {"spans": [], "warnings": []},
                 "coordinate_context": {"version": 1, "logical_space": "global_display_points", "origin": "top_left",
                                        "logical_bounds": logical, "delivered_image_size": [WIN_W, WIN_H],
                                        "output_scale": 1, "window": {"window_id": WINDOW_ID, "title": "doc", "index": 0}}})
        fail("INVALID_INPUT", "unsupported see form in the fake")

    if cmd == "clean":
        snap = opt(args, "--snapshot")
        if os.environ.get("FAKE_PB_DAEMON_SNAPSHOTS") == "1" or snap not in state.get("snapshots", []):
            out({"snapshotsRemoved": 0, "bytesFreed": 0, "snapshotDetails": [], "dryRun": False, "not_found": True,
                 "executionTime": 0.001})
        state["snapshots"].remove(snap)
        save_state(state)
        out({"snapshotsRemoved": 1, "bytesFreed": 1000, "snapshotDetails": [snap], "dryRun": False,
             "executionTime": 0.001})

    fail("INVALID_INPUT", "unsupported command: %s" % cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
