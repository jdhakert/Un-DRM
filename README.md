# Un-DRM

Capture a document from any macOS viewer, page by page, with
[Peekaboo](https://github.com/jdhakert/Peekaboo), and turn the captures into
text, Markdown, JSON and a searchable PDF.

The pipeline has three steps. Peekaboo does the first two; the third only reads
what was saved.

| Step | Script | What happens |
| --- | --- | --- |
| 1. Open | `undrm_capture.py` | Opens the document in the viewer, resizes the window so one page is shown at a readable size, applies the viewer's single-page settings through its menus. |
| 2. Capture + OCR | `undrm_capture.py` | For every page: waits until the viewer has finished rendering, runs `peekaboo see --ocr` on the exact window (PNG + Apple Vision text), saves both, advances to the next page. Stops when advancing no longer changes the page. |
| 3. Assemble | `undrm_assemble.py` | Rebuilds reading order from the OCR line boxes and writes `document.txt`, `document.md`, `document.json` and a searchable `document.pdf`. |

## Requirements

- macOS 15 or later.
- Peekaboo 4.1 or later, which added `see --ocr`; 4.4 or later is recommended
  (it fixed captures being refused while Claude or OpenClaw is running):
  `brew install openclaw/tap/peekaboo` (this fork tracks the openclaw line).
- Screen Recording and Accessibility granted to the Peekaboo host
  (`peekaboo permissions status`, `peekaboo permissions grant`). Event Synthesizing
  is only needed for key-based page turns.
- Python 3.9 or later (`/usr/bin/python3` after `xcode-select --install`). No packages.
- Optional: the Swift toolchain, for the accurate Vision OCR engine
  (`tools/vision-ocr.swift`, see below).

## Quick start

```sh
git clone https://github.com/jdhakert/Un-DRM && cd Un-DRM

# 1 + 2: open a PDF in Preview and capture every page
./undrm_capture.py --profile preview --doc ~/Documents/outline.pdf

# 3: assemble the capture directory it printed
./undrm_assemble.py captures/outline-20260921-101500 --title "Outline"
```

For a viewer that does not open files from the command line (Apple Books, Kindle),
open the book yourself, go to the first page, then:

```sh
./undrm_capture.py --profile kindle --no-open --pages 312
./undrm_assemble.py captures/kindle-20260921-113000
```

Leave the Mac alone while the capture runs. The script brings the viewer to the
front once at the start (`app launch --foreground`, which Peekaboo requires for
`--open`, then `window focus`); after that the probes, the `see --ocr` captures and
menu-based page turns run in the background and do not touch focus. Only key
presses may fall back to foreground delivery, and only when Peekaboo refuses
background delivery before dispatching anything (the log says so).

## Step 1 and 2: `undrm_capture.py`

```
./undrm_capture.py [--profile NAME] [--doc FILE | --no-open] [options]
```

What it does, in Peekaboo terms:

1. `peekaboo --version`, `see --help`, `permissions status` preflight.
2. `app launch <Viewer> --open <doc> --foreground --wait-ready --wait-for-window`,
   then `window list --pid` to find the document window (new window whose title
   contains the file name; override with `--window-title` or `--window-id`).
3. `window focus --verify`, `window set-bounds` (default 1000 pt wide, visible
   screen height minus 80, at least 600), then the profile's setup menus via
   `menu click --pid --path`. Missing optional items (for example "Content Only"
   on an older Preview) are skipped; a missing required item aborts with the
   closest matches from `menu list`.
4. Per page:
   - **Page-loaded check.** `see --no-elements` probes of the exact window are
     hashed until two consecutive probes are identical (`--settle-samples`,
     `--settle-interval`). After a page turn the probe must also differ from the
     previous page; if it never does within `--end-grace` seconds the script
     advances once more (`--end-retries`) to tell a repeated blank page from the
     end of the document.
   - **Capture + OCR.** `see --pid --window-id --ocr --retina --path pages/page-NNNN.png --json`.
     OCR rows come back as `staticText` elements with global logical bounds and a
     confidence; the raw envelope is saved as `pages/page-NNNN.json`. Pages with
     fewer than `--min-chars` characters are retried after a delay (blank pages
     are accepted after the retries, with a note). A page that has neither
     accessibility elements nor recognizable text makes Peekaboo report
     `ACCESSIBILITY_INCOMPLETE`; the PNG it already wrote is kept and the page is
     recorded as empty.
   - **Post-check.** The capture is bracketed by probes: if the window changed
     while it was being OCRed, the page is captured again until two probes agree
     (bounded by `--settle-timeout`).
   - **Advance.** `menu click --path "Go > Next Page"` (Preview, Acrobat) or
     `press right --pid --window-id` (Books, Kindle, generic), per the profile or
     `--advance`, `--next-menu`, `--next-key`.
   - `clean --snapshot` removes Peekaboo's on-disk copy of each capture when `see`
     ran in-process; on the default daemon route the snapshots live in the daemon's
     memory (it prunes them itself), `clean` answers `not_found` and the script
     stops issuing clean calls (`--keep-snapshots` skips them entirely).
5. `manifest.json` is rewritten after every page, so an interrupted run (Ctrl-C)
   still assembles.

Useful options: `--pages N` (stop after N pages; the run still ends earlier at the
end of the document), `--width/--height/--x/--y`, `--no-resize`, `--skip-setup`
(profile setup only; `--setup-menu "View > Single Page"` still runs),
`--settle-timeout`, `--see-timeout`, `--no-retina`, `--verbose` (prints every
`peekaboo` command). `./undrm_capture.py --help` lists them all.

### Viewer profiles

Profiles in `profiles/` hold the app name, setup menus and page-advance method.
Menu titles differ between macOS and app versions; the script validates each
path against `peekaboo menu list --pid <pid>` at run time and prints the closest
matches when one is missing, so adjusting a profile is a one-line change.

| Profile | App | Setup | Advance |
| --- | --- | --- | --- |
| `preview` | Preview | View > Single Page, Content Only, Zoom to Fit | Go > Next Page |
| `acrobat` | Adobe Acrobat Reader | View > Page Display > Single Page View | View > Page Navigation > Next Page |
| `books` | Books | none (set a single-column layout in the app first) | right arrow |
| `kindle` | Kindle | none (set a single-column layout in the app first) | right arrow |
| `generic` | `--app` | none | right arrow (`--next-key` / `--next-menu` to change) |

To add a viewer, copy `profiles/generic.json`, fill in `app`, and pick the setup
and advance entries after looking at `peekaboo menu list --app <App>`.

### OCR engines

- `peekaboo` (default): `see --ocr`, which runs Apple Vision locally on the
  Peekaboo host in *fast* mode. On Retina captures of rendered text this is
  usually clean.
- `vision` (`--ocr-engine vision`): `tools/vision-ocr.swift`, Apple Vision in
  *accurate* mode, compiled once into `tools/` on first use (needs `swiftc`). The
  assembler can also re-run it on an existing capture with `--reocr`, so the
  usual path is to capture with the default engine and re-OCR later if the fast
  engine stumbled on small type.

## Step 3: `undrm_assemble.py`

```
./undrm_assemble.py <capture-dir> [--out DIR] [--title T] [--formats txt,md,json,pdf]
```

It reads `manifest.json` and each page's OCR JSON, converts the OCR boxes to
window-relative points, and reconstructs reading order:

1. Recursive XY-cut: split on the widest vertical whitespace gap (columns), else
   the widest horizontal gap (rows), until no gap is left. A vertical cut is only
   accepted when both sides are wide enough to be text columns, so list markers,
   speaker names and table-of-contents page numbers stay with their lines.
   Headers and footers that span both columns come out first and last.
2. Fragments on one baseline are merged left to right; lines become paragraphs at
   larger vertical gaps, first-line indents, or (for hanging-indent layouts such
   as outlines and bibliographies) at each flush-left line.
3. Words hyphenated across lines are rejoined (`--no-dehyphenate` to keep them;
   compound words that happen to break at their hyphen are joined too, the raw
   lines in `document.json` show what was joined). `--keep-lines` preserves the
   OCR line breaks instead of reflowing paragraphs.

Outputs:

- `document.txt`: the text, form feed between pages.
- `document.md`: `## Page N` sections with the page image and its text.
- `document.json`: per page text, paragraphs and every line with confidence and
  bounds.
- `document.pdf`: each page image (JPEG via `sips`, or `--pdf-image png` for
  lossless embedding; captures with an alpha channel are flattened with Pillow
  when it is installed, otherwise they fall back to JPEG) with an invisible
  Helvetica text layer placed on the OCR boxes, so Preview, Acrobat and Spotlight
  can search and copy the text. Characters outside WinAnsi (Greek, math, CJK)
  become `?` in that layer only; the script reports how many.
  `--pdf-visible-text` draws the layer in red for checking alignment.

`--pages 1-10,15` limits the output, `--min-confidence 0.5` drops weak OCR lines,
`--col-gap 0.6` helps with very tight column gutters.

## How the page-loaded check works

A page is considered loaded when two consecutive exact-window captures have the
same SHA-256 and differ from the previous page's capture. Page-turn animations,
progressive rendering and lazy image loads all show up as changing pixels and are
waited out. A viewer that keeps animating something (a blinking caret, a clock)
never settles; the script then captures after `--settle-timeout` and falls back
to the recognized text to detect the last page: when the text of a captured page
matches the previous page after advancing, it advances again, and gives up after
`--end-retries` repeats. Because the check is pixel-exact, keep the window size
fixed for the whole run and do not move other windows over it.

## Limitations

- Reflowable e-books paginate by window size, so `--pages` counts are only valid
  for the window size used during the capture.
- Two consecutive pages that are pixel-identical are recorded as a duplicate
  (blank pages), and a page turn that the viewer silently dropped looks the same.
  A page that takes longer than `--end-grace` to render after a page turn can be
  mistaken for "no change"; raise `--end-grace` for slow viewers. Check the
  `warnings` in `manifest.json` when the page count is off.
- For a viewer that never settles, two consecutive pages with identical text
  (two blank pages) cannot be told apart and only one is kept.
- OCR is OCR: check numbers, citations and tables against the images in
  `document.md`, and use `--reocr` (accurate Vision) when the fast engine
  stumbles on small type.
- `undrm_capture.py` only runs on a Mac. The test-suite uses
  `tests/fake_peekaboo.py`, which emulates the Peekaboo commands used, so it runs
  anywhere: `python3 -m unittest discover -s tests` (needs Pillow; pypdf is used to
  verify the PDF when present).

## Troubleshooting

- `peekaboo see --ocr` reports `ACCESSIBILITY_INCOMPLETE`: that page had neither
  accessibility elements nor recognizable text (blank page, full-page image). The
  page image is kept and the page is recorded with 0 characters and a warning.
- Background `press` is refused: grant Event Synthesizing
  (`peekaboo permissions request event-synthesizing`) or prefer a menu-based
  advance (`--advance menu --next-menu "Go > Next Page"`). The script falls back to
  foreground key presses on its own; do not touch the keyboard while it runs.
- The wrong window was picked: `peekaboo window list --app <App> --json`, then
  `--window-id <id>`.
- A menu item is not found: `peekaboo menu list --app <App>` and edit the profile.
- Captures are blank or wallpaper-only: run from a normal Terminal session in the
  active desktop, not over SSH; see Peekaboo's permissions guide.

Only use this on material you are entitled to copy for your own use.
