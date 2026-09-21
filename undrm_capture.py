#!/usr/bin/env python3
"""
undrm_capture.py - steps 1 and 2 of the Un-DRM pipeline.

Drives a macOS document viewer with the Peekaboo CLI (https://github.com/jdhakert/Peekaboo):

  1. Opens the document in the viewer, resizes the window so one page is shown at a
     readable size, and applies the viewer's "single page" settings (via its menus).
  2. For every page: waits until the viewer has finished rendering (two consecutive
     identical window captures), captures the exact window with `peekaboo see --ocr`
     (PNG + Apple Vision text), saves both, and advances to the next page. The run
     stops when advancing no longer changes what is on screen (end of document) or
     when --pages is reached.

Result: a capture directory that undrm_assemble.py (step 3) turns into text,
Markdown, JSON and a searchable PDF.

    <out>/manifest.json           run metadata plus one entry per page
    <out>/pages/page-0001.png     exact-window capture (Retina scale by default)
    <out>/pages/page-0001.json    raw `peekaboo see --ocr --json` envelope

Requirements: macOS 15+, Peekaboo 4.4+ (`peekaboo see --ocr`), Python 3.9+,
Screen Recording + Accessibility granted to the Peekaboo host.

Example:
    ./undrm_capture.py --profile preview --doc ~/Documents/outline.pdf
    ./undrm_assemble.py captures/outline-20260921-101500
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / "profiles"
VISION_TOOL_SOURCE = SCRIPT_DIR / "tools" / "vision-ocr.swift"
MANIFEST_VERSION = 1

GENERIC_PROFILE: Dict[str, Any] = {
    "name": "generic",
    "app": None,
    "bundle_id": None,
    "opens_documents": False,
    "setup": [],
    "advance": {"mode": "key", "key": "right", "menu": None},
    "window": {"width": 1000, "height": None},
}


# --------------------------------------------------------------------------- utils


class UndrmError(Exception):
    """Fatal, user-facing error."""


class PeekabooError(UndrmError):
    def __init__(self, args: Sequence[str], code: Optional[str], message: str,
                 hint: Optional[str] = None, envelope: Optional[dict] = None, stderr: str = ""):
        self.cmd_args = list(args)
        self.code = code or "UNKNOWN"
        self.message = message
        self.hint = hint
        self.envelope = envelope
        self.stderr = stderr
        text = "peekaboo %s failed [%s]: %s" % (" ".join(self.cmd_args), self.code, message)
        if hint:
            text += "\n  hint: " + hint
        super().__init__(text)


def log(msg: str, level: str = "info") -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "  ", "warn": "!!", "error": "xx", "debug": ".."}.get(level, "  ")
    print("[%s] %s %s" % (ts, prefix, msg), file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_envelope(stdout: str) -> Optional[dict]:
    """Parse Peekaboo's JSON envelope, tolerating leading non-JSON lines."""
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    idx = text.find("{")
    attempts = 0
    while idx != -1 and attempts < 50:
        attempts += 1
        try:
            value, _ = decoder.raw_decode(text[idx:])
            if isinstance(value, dict) and ("success" in value or "data" in value):
                return value
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)
    return None


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


def ocr_char_count(elements: List[dict]) -> int:
    return sum(len((e.get("label") or "").strip()) for e in elements)


def version_tuple(text: str) -> Tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return (0,)
    return tuple(int(g) for g in m.groups() if g is not None)


# ------------------------------------------------------------------ peekaboo wrapper


class Peekaboo:
    def __init__(self, binary: str, verbose: bool = False):
        self.binary = binary
        self.verbose = verbose
        self.calls = 0

    def run(self, args: Sequence[str], timeout: float = 120.0, check: bool = True) -> dict:
        cmd = [self.binary, *args, "--json"]
        self.calls += 1
        if self.verbose:
            log("$ " + " ".join(shlex.quote(c) for c in cmd), "debug")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise UndrmError("peekaboo binary not found: %r. Install it with "
                             "`brew install openclaw/tap/peekaboo` or pass --peekaboo." % self.binary)
        except subprocess.TimeoutExpired:
            raise PeekabooError(args, "TIMEOUT", "no response within %.0fs" % timeout)
        envelope = parse_envelope(proc.stdout)
        if envelope is None:
            detail = (proc.stderr.strip() or proc.stdout.strip() or "exit status %d" % proc.returncode)
            raise PeekabooError(args, "NO_JSON", detail[:2000], stderr=proc.stderr)
        if check and not envelope.get("success", False):
            err = envelope.get("error") or {}
            raise PeekabooError(args, err.get("code"), err.get("message") or "unknown error",
                                err.get("hint"), envelope, proc.stderr)
        return envelope

    def data(self, args: Sequence[str], timeout: float = 120.0) -> dict:
        return self.run(args, timeout=timeout).get("data") or {}

    def text(self, args: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
        cmd = [self.binary, *args]
        self.calls += 1
        if self.verbose:
            log("$ " + " ".join(shlex.quote(c) for c in cmd), "debug")
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise UndrmError("peekaboo binary not found: %r. Install it with "
                             "`brew install openclaw/tap/peekaboo` or pass --peekaboo." % self.binary)


# -------------------------------------------------------------- optional Vision OCR


class VisionOCR:
    """Optional Apple Vision (accurate mode) OCR via tools/vision-ocr.swift.

    Used as a fallback when `peekaboo see --ocr` cannot observe a window, or on
    request with --ocr-engine vision. Needs the Swift toolchain (Xcode Command
    Line Tools)."""

    def __init__(self, build_dir: Path, verbose: bool = False):
        self.build_dir = build_dir
        self.verbose = verbose
        self.binary: Optional[Path] = None

    @staticmethod
    def available() -> bool:
        return VISION_TOOL_SOURCE.exists() and shutil.which("swiftc") is not None

    def ensure_built(self) -> Path:
        if self.binary and self.binary.exists():
            return self.binary
        if not VISION_TOOL_SOURCE.exists():
            raise UndrmError("missing %s" % VISION_TOOL_SOURCE)
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise UndrmError("swiftc not found; install Xcode Command Line Tools "
                             "(xcode-select --install) to use the Vision OCR engine")
        self.build_dir.mkdir(parents=True, exist_ok=True)
        binary = self.build_dir / "vision-ocr"
        if not binary.exists() or binary.stat().st_mtime < VISION_TOOL_SOURCE.stat().st_mtime:
            log("compiling %s (one-time)" % VISION_TOOL_SOURCE.name)
            cmd = [swiftc, "-O", "-o", str(binary), str(VISION_TOOL_SOURCE)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise UndrmError("swiftc failed:\n" + proc.stderr.strip())
        self.binary = binary
        return binary

    def recognize(self, png: Path, fast: bool = False, languages: Optional[List[str]] = None) -> dict:
        binary = self.ensure_built()
        cmd = [str(binary)]
        if fast:
            cmd.append("--fast")
        if languages:
            cmd += ["--languages", ",".join(languages)]
        cmd.append(str(png))
        if self.verbose:
            log("$ " + " ".join(shlex.quote(c) for c in cmd), "debug")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise UndrmError("vision-ocr failed: %s" % proc.stderr.strip())
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            raise UndrmError("vision-ocr produced no JSON: %r" % proc.stdout[:500])

    @staticmethod
    def elements_from(result: dict, logical_bounds: Dict[str, float], min_confidence: float = 0.3) -> List[dict]:
        lx, ly = logical_bounds["x"], logical_bounds["y"]
        lw, lh = logical_bounds["width"], logical_bounds["height"]
        elements = []
        for n, obs in enumerate(result.get("observations") or [], start=1):
            conf = float(obs.get("confidence") or 0.0)
            text = (obs.get("text") or "").strip()
            if conf < min_confidence or not text:
                continue
            box = obs.get("bbox") or {}
            elements.append({
                "id": "ocr_%d" % n,
                "role": "staticText",
                "label": text,
                "description": "ocr",
                "confidence": round(conf, 3),
                "bounds": {
                    "x": lx + float(box.get("x", 0.0)) * lw,
                    "y": ly + float(box.get("y", 0.0)) * lh,
                    "width": float(box.get("width", 0.0)) * lw,
                    "height": float(box.get("height", 0.0)) * lh,
                },
                "is_actionable": False,
                "source": "vision-" + str(result.get("level") or "accurate"),
            })
        return elements


# ------------------------------------------------------------------------ profiles


def load_profile(name_or_path: Optional[str]) -> Dict[str, Any]:
    profile = json.loads(json.dumps(GENERIC_PROFILE))
    if not name_or_path:
        return profile
    path = Path(name_or_path).expanduser()
    if not path.exists():
        candidate = PROFILE_DIR / ("%s.json" % name_or_path)
        if candidate.exists():
            path = candidate
        else:
            names = sorted(p.stem for p in PROFILE_DIR.glob("*.json"))
            raise UndrmError("profile %r not found; available: %s" % (name_or_path, ", ".join(names)))
    with open(path) as fh:
        data = json.load(fh)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(profile.get(key), dict):
            profile[key].update(value)
        else:
            profile[key] = value
    profile.setdefault("name", path.stem)
    return profile


# ------------------------------------------------------------------ capture session


class CaptureSession:
    def __init__(self, args: argparse.Namespace, profile: Dict[str, Any]):
        self.args = args
        self.profile = profile
        self.pb = Peekaboo(args.peekaboo, verbose=args.verbose)
        self.out = Path(args.out).expanduser().resolve()
        self.pages_dir = self.out / "pages"
        self.tmp_dir = self.out / ".tmp"
        self.app_name: Optional[str] = args.app or profile.get("app")
        self.bundle_id: Optional[str] = profile.get("bundle_id")
        self.pid: Optional[int] = None
        self.window_id: Optional[int] = None
        self.window_title: Optional[str] = None
        self.window_bounds: Optional[Dict[str, float]] = None
        self.retina = not args.no_retina
        self.ocr_engine = args.ocr_engine
        self.vision = VisionOCR(self.out / ".tools", verbose=args.verbose)
        self.pages: List[dict] = []
        self.warnings: List[str] = []
        self.snapshot_ids: List[str] = []
        self.status = "running"
        self.stop_reason: Optional[str] = None
        self.peekaboo_version: Optional[str] = None
        self.started = dt.datetime.now()
        adv = dict(profile.get("advance") or {})
        self.advance_mode = args.advance or adv.get("mode") or "key"
        self.next_menu = args.next_menu or adv.get("menu")
        self.next_key = args.next_key or adv.get("key") or "right"
        if self.advance_mode == "menu" and not self.next_menu:
            raise UndrmError("--advance menu needs --next-menu (or a profile that defines advance.menu)")

    # ---- preflight -----------------------------------------------------------

    def preflight(self) -> None:
        if platform.system() != "Darwin":
            log("not running on macOS; Peekaboo can only drive a Mac desktop (continuing for testing)", "warn")
        proc = self.pb.text(["--version"])
        self.peekaboo_version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "unknown"
        log("peekaboo: %s" % self.peekaboo_version)
        if version_tuple(self.peekaboo_version) < (4, 4) and not self.args.skip_checks:
            log("Peekaboo 4.4+ is required for `see --ocr` (found %s)" % self.peekaboo_version, "warn")
        if self.ocr_engine in ("peekaboo", "auto"):
            help_text = self.pb.text(["see", "--help"])
            combined = (help_text.stdout or "") + (help_text.stderr or "")
            if "--ocr" not in combined and not self.args.skip_checks:
                raise UndrmError("this Peekaboo build has no `see --ocr`; upgrade to 4.4+ "
                                 "(brew upgrade openclaw/tap/peekaboo) or use --ocr-engine vision")
        if self.ocr_engine == "vision" and not VisionOCR.available():
            raise UndrmError("--ocr-engine vision needs tools/vision-ocr.swift and the Swift toolchain "
                             "(xcode-select --install)")
        if self.args.skip_checks:
            return
        try:
            perms = self.pb.data(["permissions", "status"])
        except PeekabooError as exc:
            log("could not read permission status: %s" % exc, "warn")
            return
        entries = self._find_permission_entries(perms)
        missing = [e["name"] for e in entries
                   if e.get("isRequired", True) and not e.get("isGranted", False)
                   and e["name"] in ("Screen Recording", "Accessibility")]
        for entry in entries:
            log("permission %-18s %s" % (entry["name"] + ":", "granted" if entry.get("isGranted") else "NOT granted"))
        if missing:
            raise UndrmError("missing required permissions: %s. Run `peekaboo permissions grant` "
                             "(grant them to the host shown by `peekaboo permissions status`)." % ", ".join(missing))
        if self.advance_mode in ("key", "key-foreground"):
            ev = next((e for e in entries if e["name"] == "Event Synthesizing"), None)
            if ev is not None and not ev.get("isGranted", False):
                log("Event Synthesizing is not granted; key-based page advance may be refused. "
                    "Run `peekaboo permissions request event-synthesizing` or use --advance menu.", "warn")

    @staticmethod
    def _find_permission_entries(data: Any) -> List[dict]:
        found: List[dict] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                if node and all(isinstance(n, dict) and "name" in n and "isGranted" in n for n in node):
                    found.extend(node)
                    return
                for n in node:
                    walk(n)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(data)
        return found

    # ---- app + window ----------------------------------------------------------

    def running_apps(self) -> List[dict]:
        try:
            data = self.pb.data(["app", "list", "--include-hidden", "--include-background"])
        except PeekabooError as exc:
            log("app list failed: %s" % exc, "warn")
            return []
        return list(data.get("apps") or [])

    def find_running_app(self) -> Optional[dict]:
        wanted_name = (self.app_name or "").lower()
        wanted_bundle = (self.bundle_id or "").lower()
        for app in self.running_apps():
            if wanted_bundle and str(app.get("bundle_id") or "").lower() == wanted_bundle:
                return app
            if wanted_name and str(app.get("name") or "").lower() == wanted_name:
                return app
        return None

    def list_windows(self) -> List[dict]:
        if self.pid is None:
            return []
        data = self.pb.data(["window", "list", "--pid", str(self.pid)])
        return [w for w in (data.get("windows") or []) if w.get("window_id") is not None]

    def open_document(self) -> None:
        doc = self.args.doc
        if self.args.window_id is not None and self.pid is None and not self.app_name:
            raise UndrmError("--window-id needs --app (or a profile with an app) so the owner PID is known")
        before_ids: set = set()
        existing = self.find_running_app() if (self.app_name or self.bundle_id) else None
        if existing:
            self.pid = int(existing["pid"])
            before_ids = {int(w["window_id"]) for w in self.list_windows()}
            log("%s is running (pid %d, %d window(s))" % (existing.get("name"), self.pid, len(before_ids)))

        if doc and not self.args.no_open:
            doc_path = Path(doc).expanduser().resolve()
            if not doc_path.exists():
                raise UndrmError("document not found: %s" % doc_path)
            if not (self.app_name or self.bundle_id):
                raise UndrmError("--doc needs --app or a --profile that names the viewer application")
            launch_args = ["app", "launch"]
            if self.app_name:
                launch_args.append(self.app_name)
            else:
                launch_args += ["--bundle-id", self.bundle_id]
            launch_args += ["--open", str(doc_path), "--foreground", "--wait-ready", "--wait-for-window"]
            log("opening %s in %s" % (doc_path.name, self.app_name or self.bundle_id))
            try:
                data = self.pb.data(launch_args, timeout=90)
            except PeekabooError as exc:
                if exc.code == "APP_NOT_FOUND" and self.bundle_id and self.app_name:
                    log("app name lookup failed, retrying by bundle id %s" % self.bundle_id, "warn")
                    launch_args = ["app", "launch", "--bundle-id", self.bundle_id, "--open", str(doc_path),
                                   "--foreground", "--wait-ready", "--wait-for-window"]
                    data = self.pb.data(launch_args, timeout=90)
                else:
                    raise
            self.pid = int(data["pid"])
            self.app_name = data.get("app_name") or self.app_name
            log("launched %s (pid %d)" % (self.app_name, self.pid))
        elif self.pid is None:
            raise UndrmError("%s is not running; open the document there first, or pass --doc to open it"
                             % (self.app_name or self.bundle_id or "the viewer"))

        self.find_document_window(before_ids, Path(doc).expanduser() if doc else None)

    def find_document_window(self, before_ids: set, doc_path: Optional[Path]) -> None:
        deadline = time.monotonic() + self.args.window_timeout
        wanted_title = (self.args.window_title or "").lower()
        stem = doc_path.stem.lower() if doc_path else ""
        name = doc_path.name.lower() if doc_path else ""
        last_windows: List[dict] = []
        while True:
            windows = self.list_windows()
            last_windows = windows
            chosen: Optional[dict] = None
            if self.args.window_id is not None:
                chosen = next((w for w in windows if int(w["window_id"]) == self.args.window_id), None)
            elif wanted_title:
                matches = [w for w in windows if wanted_title in str(w.get("window_title") or "").lower()]
                if len(matches) > 1:
                    raise UndrmError("--window-title %r matches %d windows: %s" % (
                        self.args.window_title, len(matches),
                        ", ".join("%s (%s)" % (w["window_id"], w.get("window_title")) for w in matches)))
                chosen = matches[0] if matches else None
            else:
                new = [w for w in windows if int(w["window_id"]) not in before_ids]
                titled = [w for w in (new or windows)
                          if name and (name in str(w.get("window_title") or "").lower()
                                       or stem in str(w.get("window_title") or "").lower())]
                if titled:
                    chosen = titled[0]
                elif len(new) == 1:
                    chosen = new[0]
                elif not doc_path and windows:
                    keyed = [w for w in windows if w.get("is_key") or w.get("is_frontmost")]
                    chosen = (keyed or windows)[0]
            if chosen:
                self.window_id = int(chosen["window_id"])
                self.window_title = chosen.get("window_title")
                self.window_bounds = rect_from_json(chosen.get("bounds"))
                cap = chosen.get("observation_capability")
                log("window %d %r %s" % (self.window_id, self.window_title,
                                         "(%s)" % cap if cap else ""))
                if cap == "pixels_only":
                    log("window reports pixels_only accessibility; `see --ocr` may fail and fall back to the "
                        "Vision engine if available", "warn")
                return
            if time.monotonic() > deadline:
                break
            time.sleep(0.5)
        listing = "\n".join("  %s  %r" % (w.get("window_id"), w.get("window_title")) for w in last_windows) or "  (none)"
        raise UndrmError("could not find the document window within %.0fs. Windows of pid %s:\n%s\n"
                         "Pass --window-title or --window-id to choose one."
                         % (self.args.window_timeout, self.pid, listing))

    # ---- layout + setup ------------------------------------------------------------

    def screen_for_window(self) -> Optional[dict]:
        try:
            data = self.pb.data(["screen", "list"])
        except PeekabooError as exc:
            log("screen list failed: %s" % exc, "warn")
            return None
        screens = data.get("screens") or []
        if not screens:
            return None
        if self.window_bounds:
            wb = self.window_bounds
            cx, cy = wb["x"] + wb["width"] / 2, wb["y"] + wb["height"] / 2
            for s in screens:
                b = rect_from_json(s.get("bounds"))
                if b and b["x"] <= cx < b["x"] + b["width"] and b["y"] <= cy < b["y"] + b["height"]:
                    return s
        primary = [s for s in screens if s.get("isPrimary") or s.get("is_primary")]
        return (primary or screens)[0]

    def layout_window(self) -> None:
        if self.args.no_resize:
            return
        wcfg = dict(self.profile.get("window") or {})
        screen = self.screen_for_window()
        sb = rect_from_json(screen.get("bounds")) if screen else None
        visible = size_from_json(screen.get("visibleArea") or screen.get("visible_area")) if screen else None
        avail_h = (visible or sb or {"height": 900.0})["height"]
        width = self.args.width or wcfg.get("width") or 1000
        height = self.args.height or wcfg.get("height") or max(600, int(avail_h) - 80)
        x = self.args.x if self.args.x is not None else int((sb or {"x": 0})["x"]) + 40
        y = self.args.y if self.args.y is not None else int((sb or {"y": 0})["y"]) + 60
        log("resizing window to %dx%d at (%d,%d)" % (width, height, x, y))
        try:
            data = self.pb.data(["window", "set-bounds", "--pid", str(self.pid), "--window-id", str(self.window_id),
                                 "-x", str(x), "-y", str(y), "-w", str(width), "--height", str(height)])
            nb = rect_from_json(data.get("new_bounds"))
            if nb:
                self.window_bounds = nb
            if data.get("warning"):
                log("set-bounds: %s" % data["warning"], "warn")
        except PeekabooError as exc:
            log("could not resize the window (%s); continuing with its current size" % exc.message, "warn")
            self.refresh_window_bounds()

    def refresh_window_bounds(self) -> None:
        for w in self.list_windows():
            if int(w["window_id"]) == self.window_id:
                self.window_bounds = rect_from_json(w.get("bounds")) or self.window_bounds
                self.window_title = w.get("window_title") or self.window_title
                return

    def focus_window(self) -> None:
        args = ["window", "focus", "--pid", str(self.pid), "--window-id", str(self.window_id), "--verify"]
        if self.args.space_switch:
            args.append("--space-switch")
        try:
            self.pb.run(args)
        except PeekabooError as exc:
            log("window focus failed (%s); captures still work, but key-based page turns need focus"
                % exc.message, "warn")

    def menu_paths(self) -> List[str]:
        try:
            data = self.pb.data(["menu", "list", "--pid", str(self.pid), "--include-disabled"])
        except PeekabooError as exc:
            log("menu list failed: %s" % exc.message, "warn")
            return []
        paths: List[str] = []

        def walk(items: List[dict], prefix: str) -> None:
            for item in items or []:
                if item.get("separator"):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                full = "%s > %s" % (prefix, title) if prefix else title
                paths.append(full)
                walk(item.get("items") or [], full)

        for menu in data.get("menu_structure") or []:
            walk(menu.get("items") or [], str(menu.get("title") or ""))
        return paths

    def menu_click(self, path: str, optional: bool = False, known_paths: Optional[List[str]] = None) -> bool:
        try:
            self.pb.run(["menu", "click", "--pid", str(self.pid), "--path", path])
            return True
        except PeekabooError as exc:
            if exc.code in ("MENU_ITEM_NOT_FOUND", "MENU_BAR_NOT_FOUND") or "not found" in exc.message.lower():
                if optional:
                    log("menu item %r not present (skipped)" % path, "warn")
                    return False
                suggestion = ""
                if known_paths:
                    close = difflib.get_close_matches(path, known_paths, n=5, cutoff=0.4)
                    if close:
                        suggestion = "\n  similar menu items:\n    " + "\n    ".join(close)
                raise UndrmError("menu item %r not found in %s.%s\n  Inspect with: peekaboo menu list --pid %s"
                                 % (path, self.app_name, suggestion, self.pid))
            raise

    def run_setup(self) -> None:
        steps = list(self.profile.get("setup") or [])
        for extra in self.args.setup_menu or []:
            steps.append({"menu": extra})
        if self.args.skip_setup or not steps:
            return
        known = self.menu_paths()
        for step in steps:
            if "menu" in step:
                log("menu: %s" % step["menu"])
                self.menu_click(step["menu"], optional=bool(step.get("optional")), known_paths=known)
            elif "key" in step:
                log("key: %s" % step["key"])
                self.press_key(step["key"], foreground=bool(step.get("foreground", True)))
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
            time.sleep(float(step.get("settle", 0.3)))
        time.sleep(self.args.setup_settle)
        self.refresh_window_bounds()

    # ---- capturing ----------------------------------------------------------------

    def _retina_flag(self) -> List[str]:
        return ["--retina"] if self.retina else []

    def probe_hash(self) -> str:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / ("probe-%d.png" % self.pb.calls)
        data = self.pb.data(["see", "--pid", str(self.pid), "--window-id", str(self.window_id),
                             "--no-elements", "--path", str(tmp)] + self._retina_flag(), timeout=90)
        files = data.get("files") or []
        path = Path(files[0]["path"]) if files and files[0].get("path") else tmp
        if not path.exists():
            raise UndrmError("probe capture did not produce %s" % path)
        digest = sha256_file(path)
        try:
            path.unlink()
        except OSError:
            pass
        return digest

    def wait_for_page(self, prev_hash: Optional[str]) -> Optional[str]:
        """Poll until the window is stable. Returns the stable hash, or None when the
        window never changed from prev_hash (advance had no effect)."""
        a = self.args
        started = time.monotonic()
        deadline = started + a.settle_timeout
        last: Optional[str] = None
        stable = 0
        unchanged_polls = 0
        while True:
            digest = self.probe_hash()
            now = time.monotonic()
            if prev_hash is not None and digest == prev_hash:
                unchanged_polls += 1
                stable = 0
                if unchanged_polls >= 2 and now - started >= a.end_grace:
                    return None
            else:
                unchanged_polls = 0
                stable = stable + 1 if digest == last else 1
                if stable >= a.settle_samples:
                    return digest
            last = digest
            if now > deadline:
                if last is not None and last != prev_hash:
                    self.note("page did not settle within %.0fs; capturing anyway" % a.settle_timeout)
                    return last
                return None
            time.sleep(a.settle_interval)

    def press_key(self, key: str, foreground: bool) -> None:
        if foreground:
            self.pb.run(["press", key, "--pid", str(self.pid), "--foreground"])
        else:
            self.pb.run(["press", key, "--pid", str(self.pid), "--window-id", str(self.window_id)])

    def advance(self) -> None:
        mode = self.advance_mode
        if mode == "menu":
            self.pb.run(["menu", "click", "--pid", str(self.pid), "--path", self.next_menu])
        elif mode == "key":
            try:
                self.press_key(self.next_key, foreground=False)
            except PeekabooError as exc:
                if self.args.no_foreground_fallback:
                    raise
                log("background key press refused (%s); switching to foreground key presses. "
                    "Do not use the Mac while the capture runs." % exc.code, "warn")
                self.advance_mode = "key-foreground"
                self.press_key(self.next_key, foreground=True)
        elif mode == "key-foreground":
            self.press_key(self.next_key, foreground=True)
        else:
            raise UndrmError("unknown advance mode %r" % mode)
        time.sleep(self.args.advance_delay)

    def ocr_capture(self, page_no: int) -> Tuple[Path, dict, List[dict], str]:
        """Capture + OCR the current page. Returns (png, see_json, ocr_elements, engine)."""
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        png = self.pages_dir / ("page-%04d.png" % page_no)
        if self.ocr_engine == "vision":
            return self.vision_capture(png)
        args = ["see", "--pid", str(self.pid), "--window-id", str(self.window_id), "--ocr",
                "--path", str(png), "--timeout", "%ds" % int(self.args.see_timeout)] + self._retina_flag()
        if self.args.ax_max_elements:
            args += ["--max-elements", str(self.args.ax_max_elements)]
        envelope: Optional[dict] = None
        last_exc: Optional[PeekabooError] = None
        for attempt in range(2):
            try:
                envelope = self.pb.run(args, timeout=self.args.see_timeout + 60)
                break
            except PeekabooError as exc:
                last_exc = exc
                if exc.code in ("ACCESSIBILITY_INCOMPLETE", "TIMEOUT") and attempt == 0:
                    log("see --ocr: %s (retrying once)" % exc.code, "warn")
                    time.sleep(1.0)
                    continue
                break
        if envelope is None:
            assert last_exc is not None
            if last_exc.code == "ACCESSIBILITY_INCOMPLETE":
                if self.ocr_engine == "auto" and VisionOCR.available():
                    log("see --ocr cannot observe this window; falling back to the Vision OCR engine", "warn")
                    self.ocr_engine = "vision"
                    return self.vision_capture(png)
                raise UndrmError("%s\n  This viewer exposes no accessibility elements, so `see --ocr` refuses. "
                                 "Install the Swift toolchain (xcode-select --install) and rerun with "
                                 "--ocr-engine vision." % last_exc)
            raise last_exc
        data = envelope.get("data") or {}
        elements = [e for e in (data.get("ui_elements") or []) if is_ocr_element(e)]
        raw = data.get("screenshot_raw")
        if raw and Path(raw) != png and Path(raw).exists() and not png.exists():
            shutil.copyfile(raw, png)
        if not png.exists():
            raise UndrmError("see --ocr did not write %s" % png)
        snap = data.get("snapshot_id")
        if snap:
            self.snapshot_ids.append(snap)
        return png, envelope, elements, "peekaboo"

    def vision_capture(self, png: Path) -> Tuple[Path, dict, List[dict], str]:
        data = self.pb.data(["see", "--pid", str(self.pid), "--window-id", str(self.window_id),
                             "--no-elements", "--path", str(png)] + self._retina_flag(), timeout=90)
        files = data.get("files") or []
        written = Path(files[0]["path"]) if files and files[0].get("path") else png
        if written != png and written.exists():
            shutil.move(str(written), str(png))
        if not png.exists():
            raise UndrmError("capture did not write %s" % png)
        coords = None
        for obs in data.get("observations") or []:
            coords = obs.get("coordinates") or coords
        logical = rect_from_json((coords or {}).get("logical_bounds")) or self.window_bounds
        if not logical:
            raise UndrmError("cannot map OCR results: no logical bounds for window %s" % self.window_id)
        result = self.vision.recognize(png, languages=self.args.languages)
        elements = VisionOCR.elements_from(result, logical, self.args.min_confidence)
        image_size = {"width": float(result.get("width") or 0), "height": float(result.get("height") or 0)}
        envelope = {
            "success": True,
            "data": {
                "ui_elements": elements,
                "coordinate_context": {
                    "version": 1,
                    "logical_space": "global_display_points",
                    "origin": "top_left",
                    "logical_bounds": logical,
                    "delivered_image_size": image_size,
                    "output_scale": (image_size["width"] / logical["width"]) if logical.get("width") else None,
                },
                "screenshot_raw": str(png),
                "capture_mode": "window",
                "ocr_engine": "vision-" + str(result.get("level") or "accurate"),
                "observation": {"warnings": []},
            },
        }
        return png, envelope, elements, "vision"

    def clean_snapshots(self) -> None:
        if self.args.keep_snapshots or not self.snapshot_ids:
            return
        for snap in self.snapshot_ids:
            try:
                self.pb.run(["clean", "--snapshot", snap], timeout=30)
            except PeekabooError as exc:
                log("clean --snapshot %s: %s" % (snap, exc.message), "debug")
        self.snapshot_ids = []

    def note(self, message: str) -> None:
        log(message, "warn")
        self.warnings.append(message)

    # ---- manifest -----------------------------------------------------------------

    def manifest(self) -> dict:
        return {
            "version": MANIFEST_VERSION,
            "generator": "undrm_capture.py",
            "started": self.started.isoformat(timespec="seconds"),
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
            "status": self.status,
            "stop_reason": self.stop_reason,
            "document": str(Path(self.args.doc).expanduser().resolve()) if self.args.doc else None,
            "title": self.args.title or (Path(self.args.doc).stem if self.args.doc else self.window_title),
            "profile": self.profile.get("name"),
            "app": {"name": self.app_name, "bundle_id": self.bundle_id, "pid": self.pid},
            "window": {"id": self.window_id, "title": self.window_title, "bounds": self.window_bounds},
            "peekaboo": {"binary": self.args.peekaboo, "version": self.peekaboo_version, "retina": self.retina},
            "ocr_engine": self.ocr_engine,
            "advance": {"mode": self.advance_mode, "menu": self.next_menu, "key": self.next_key},
            "settings": {
                "settle_interval": self.args.settle_interval,
                "settle_samples": self.args.settle_samples,
                "settle_timeout": self.args.settle_timeout,
                "end_grace": self.args.end_grace,
                "min_chars": self.args.min_chars,
            },
            "warnings": self.warnings,
            "page_count": len(self.pages),
            "pages": self.pages,
        }

    def write_manifest(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.out / "manifest.json.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.manifest(), fh, indent=2)
        os.replace(tmp, self.out / "manifest.json")

    # ---- main loop ------------------------------------------------------------------

    def capture_page(self, page_no: int, settled_hash: str) -> dict:
        attempts = 0
        while True:
            attempts += 1
            png, envelope, elements, engine = self.ocr_capture(page_no)
            chars = ocr_char_count(elements)
            if chars < self.args.min_chars and attempts <= self.args.ocr_retries:
                log("page %d: only %d characters recognized; waiting and retrying (%d/%d)"
                    % (page_no, chars, attempts, self.args.ocr_retries), "warn")
                time.sleep(self.args.retry_delay)
                continue
            break
        if not self.args.no_postcheck:
            after = self.probe_hash()
            if after != settled_hash:
                self.note("page %d changed while it was being captured; recapturing" % page_no)
                png, envelope, elements, engine = self.ocr_capture(page_no)
                chars = ocr_char_count(elements)
                settled_hash = after
        data = envelope.get("data") or {}
        json_path = self.pages_dir / ("page-%04d.json" % page_no)
        with open(json_path, "w") as fh:
            json.dump(envelope, fh, indent=1)
        ctx = data.get("coordinate_context") or {}
        page_warnings = list((data.get("observation") or {}).get("warnings") or [])
        trunc = data.get("truncation")
        if isinstance(trunc, dict) and trunc.get("warning"):
            page_warnings.append(trunc["warning"])
        if chars < self.args.min_chars:
            page_warnings.append("only %d characters recognized" % chars)
        entry = {
            "index": page_no,
            "image": str(png.relative_to(self.out)),
            "see_json": str(json_path.relative_to(self.out)),
            "hash": settled_hash,
            "engine": engine,
            "ocr_elements": len(elements),
            "chars": chars,
            "logical_bounds": rect_from_json(ctx.get("logical_bounds")) or self.window_bounds,
            "image_size": size_from_json(ctx.get("delivered_image_size")),
            "output_scale": ctx.get("output_scale"),
            "warnings": page_warnings,
            "captured": dt.datetime.now().isoformat(timespec="seconds"),
        }
        log("page %d: %d text lines, %d chars%s" % (page_no, len(elements), chars,
                                                    " [%s]" % "; ".join(page_warnings) if page_warnings else ""))
        return entry

    def run(self) -> int:
        a = self.args
        if self.out.exists() and (self.out / "manifest.json").exists() and not a.force:
            raise UndrmError("%s already contains a capture; choose another --out or pass --force" % self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.preflight()
        self.open_document()
        self.focus_window()
        self.layout_window()
        self.run_setup()
        self.write_manifest()
        log("capturing pages into %s (Ctrl-C to stop; partial results are kept)" % self.out)

        prev_hash: Optional[str] = None
        seen: Dict[str, int] = {}
        page_no = 0
        exit_code = 0
        try:
            while True:
                if a.pages is not None and page_no >= a.pages:
                    self.status, self.stop_reason = "complete", "requested page count reached"
                    break
                if page_no >= a.max_pages:
                    self.status, self.stop_reason = "stopped", "--max-pages reached"
                    break
                if page_no > 0:
                    self.advance()
                settled = self.wait_for_page(prev_hash)
                if settled is None and page_no > 0:
                    retried = False
                    for _ in range(a.end_retries):
                        log("no change after advancing; advancing again to distinguish a repeated page "
                            "from the end of the document")
                        self.advance()
                        settled = self.wait_for_page(prev_hash)
                        if settled is not None:
                            retried = True
                            break
                    if settled is None:
                        self.status, self.stop_reason = "complete", "advancing no longer changes the page"
                        break
                    if retried:
                        page_no += 1
                        dup = dict(self.pages[-1])
                        dup.update({"index": page_no, "duplicate_of": self.pages[-1]["index"],
                                    "warnings": ["identical to the previous page after advancing; "
                                                 "recorded as a duplicate (blank page or dropped page turn)"]})
                        self.pages.append(dup)
                        self.note("page %d recorded as a duplicate of page %d" % (page_no, dup["duplicate_of"]))
                if settled is None:
                    self.status, self.stop_reason = "error", "the first page never settled"
                    exit_code = 1
                    break
                page_no += 1
                if settled in seen:
                    self.note("page %d looks identical to page %d" % (page_no, seen[settled]))
                seen.setdefault(settled, page_no)
                entry = self.capture_page(page_no, settled)
                self.pages.append(entry)
                prev_hash = entry["hash"]
                self.write_manifest()
                self.clean_snapshots()
        except KeyboardInterrupt:
            self.status, self.stop_reason = "interrupted", "stopped by user"
            exit_code = 130
        except UndrmError as exc:
            self.status, self.stop_reason = "error", str(exc)
            log(str(exc), "error")
            exit_code = 1
        finally:
            self.write_manifest()
            self.clean_snapshots()
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        log("%s: %d page(s) captured (%s). Manifest: %s" % (self.status, len(self.pages), self.stop_reason,
                                                            self.out / "manifest.json"))
        if self.pages:
            log("next: ./undrm_assemble.py %s" % shlex.quote(str(self.out)))
        return exit_code


# ------------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("what to capture")
    g.add_argument("--doc", help="document to open in the viewer (omit with --no-open to use what is already open)")
    g.add_argument("--profile", help="viewer profile name from profiles/ (preview, acrobat, books, kindle, generic) or a JSON path")
    g.add_argument("--app", help="viewer application name (overrides the profile)")
    g.add_argument("--no-open", action="store_true", help="do not open --doc; target the viewer's existing window")
    g.add_argument("--window-title", help="pick the viewer window whose title contains this text")
    g.add_argument("--window-id", type=int, help="pick an exact WindowServer window id (see `peekaboo window list`)")
    g.add_argument("--pages", type=int, help="capture exactly this many pages")
    g.add_argument("--max-pages", type=int, default=2000, help="safety cap (default 2000)")
    g.add_argument("--title", help="document title recorded in the manifest")
    g.add_argument("--out", help="capture directory (default captures/<name>-<timestamp>)")
    g.add_argument("--force", action="store_true", help="allow writing into an --out that already holds a capture")

    g = p.add_argument_group("viewer layout")
    g.add_argument("--width", type=int, help="window width in points (profile default 1000)")
    g.add_argument("--height", type=int, help="window height in points (default: screen height - 80)")
    g.add_argument("--x", type=int, help="window x origin")
    g.add_argument("--y", type=int, help="window y origin")
    g.add_argument("--no-resize", action="store_true", help="leave the window geometry alone")
    g.add_argument("--space-switch", action="store_true", help="allow focusing a window on another Space")
    g.add_argument("--setup-menu", action="append", metavar="PATH", help='extra menu path to click during setup, e.g. "View > Single Page"')
    g.add_argument("--skip-setup", action="store_true", help="skip the profile's setup menu clicks")
    g.add_argument("--setup-settle", type=float, default=1.0, help="seconds to wait after setup (default 1.0)")

    g = p.add_argument_group("page advance")
    g.add_argument("--advance", choices=["menu", "key", "key-foreground"], help="how to turn pages (profile default)")
    g.add_argument("--next-menu", help='menu path that goes to the next page, e.g. "Go > Next Page"')
    g.add_argument("--next-key", help="key chord for the next page (xdotool style: right, pagedown, space)")
    g.add_argument("--advance-delay", type=float, default=0.15, help="seconds to wait after advancing before probing")
    g.add_argument("--no-foreground-fallback", action="store_true", help="fail instead of switching to foreground key presses")
    g.add_argument("--end-retries", type=int, default=1, help="extra advances to try when the page did not change (default 1)")

    g = p.add_argument_group("page-loaded detection")
    g.add_argument("--settle-interval", type=float, default=0.35, help="seconds between probe captures (default 0.35)")
    g.add_argument("--settle-samples", type=int, default=2, help="identical consecutive probes required (default 2)")
    g.add_argument("--settle-timeout", type=float, default=20.0, help="give up waiting for a stable page after this (default 20s)")
    g.add_argument("--end-grace", type=float, default=3.0, help="seconds of no change after advancing that mean 'last page' (default 3)")
    g.add_argument("--no-postcheck", action="store_true", help="skip the probe that verifies the page did not change during OCR")

    g = p.add_argument_group("OCR")
    g.add_argument("--ocr-engine", choices=["auto", "peekaboo", "vision"], default="auto",
                   help="auto: `peekaboo see --ocr`, falling back to Apple Vision accurate mode via tools/vision-ocr.swift; "
                        "vision: always use the accurate Vision engine (needs swiftc)")
    g.add_argument("--see-timeout", type=float, default=60.0, help="peekaboo see --timeout for OCR captures (default 60s)")
    g.add_argument("--ax-max-elements", type=int, help="pass --max-elements to peekaboo see for very busy windows")
    g.add_argument("--min-chars", type=int, default=20, help="retry the capture when fewer characters are recognized (default 20)")
    g.add_argument("--ocr-retries", type=int, default=2, help="retries for pages below --min-chars (default 2)")
    g.add_argument("--retry-delay", type=float, default=1.5, help="seconds between OCR retries (default 1.5)")
    g.add_argument("--min-confidence", type=float, default=0.3, help="Vision engine confidence floor (default 0.3)")
    g.add_argument("--languages", type=lambda s: [x for x in s.split(",") if x], help="Vision engine languages, e.g. en-US,de-DE")
    g.add_argument("--no-retina", action="store_true", help="capture at 1x instead of the display's native scale")

    g = p.add_argument_group("misc")
    g.add_argument("--peekaboo", default=os.environ.get("PEEKABOO_BIN", "peekaboo"), help="peekaboo binary (default: $PEEKABOO_BIN or peekaboo)")
    g.add_argument("--window-timeout", type=float, default=20.0, help="seconds to wait for the document window (default 20)")
    g.add_argument("--keep-snapshots", action="store_true", help="do not prune Peekaboo's snapshot cache as pages are captured")
    g.add_argument("--skip-checks", action="store_true", help="skip version and permission preflight checks")
    g.add_argument("--verbose", action="store_true", help="print every peekaboo command")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        if not args.out:
            stem = Path(args.doc).stem if args.doc else (profile.get("name") or "capture")
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "capture"
            args.out = str(Path("captures") / ("%s-%s" % (stem, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
        if not (args.app or profile.get("app")):
            if args.doc and not args.no_open:
                parser.error("--doc needs --app or --profile so the viewer is known")
            if args.no_open:
                parser.error("--no-open needs --app or --profile so the viewer's window can be found")
        if not args.doc and not args.no_open and not args.window_id and not args.window_title:
            if not (args.app or profile.get("app")):
                parser.error("give --doc (with --profile/--app), or --no-open with --app to capture an open document")
            args.no_open = True
        session = CaptureSession(args, profile)
        return session.run()
    except UndrmError as exc:
        log(str(exc), "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
