#!/usr/bin/env python3
"""Lay out cover images into printable A4 sheets of RFID cards.

The workflow this feeds: print a sheet, stick a round RFID tag on the back of
each image, laminate the whole page, then cut it into square cards. Because the
cards are packed edge-to-edge (no gaps), every internal cut is shared between two
cards — a single straight cut separates a whole row or column. Thin tick marks in
the page margins mark each cut line so a ruler can be laid across the sheet.

Each card is a square (default 6x6 cm). Source covers are YouTube (Music)
thumbnails: a square title image centred in a 16:9 frame with solid colour bars
left/right. Those bars are trimmed automatically and the image is centre-cropped
to a square, reproducing the manual crop that was done before (the title text
sits inside the square, so it is preserved).

Like ./sync, this keeps a manifest so you don't have to say *what* to print:

    ./cards                    # lay out every cover not yet printed -> a new PDF
    ./cards --dry-run          # list what's new, write & mark nothing
    ./cards "Bibi und Tina"    # only covers under this subpath (still new-only)
    ./cards --all              # include already-printed covers too
    ./cards --reset            # forget all print history (so everything is "new")
    ./cards --reset "Bibi und Tina"   # forget just this subpath

By default a real run marks the covers it laid out as printed, so the next
plain ./cards only picks up newly-added covers. Use --no-mark to generate a
sheet without touching the manifest, or --dry-run to preview.

**Other media.** What a card *is* and where it goes is a backend's business
(card_backends): this command selects the covers, hands them over and marks
them — nothing here knows about paper. So a print service can add itself as
`--backend <name>` without this file growing a branch for it:

    ./cards --backend kartenschmiede    # a card service, not a sheet
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

import card_backends
import espuino
from print_state import PrintState

# --- page / card geometry ---------------------------------------------------
DPI = 300
A4_CM = (21.0, 29.7)
DEFAULT_CARD_CM = 6.0
DEFAULT_COLS = 3
# Minimum white space kept between the card block and the paper edge, so home
# printers (which cannot print to the very edge) never clip a card.
PAGE_MARGIN_CM = 0.7
# Cut-guide tick marks: length and shade (light grey, thin).
MARK_LEN_CM = 0.45
MARK_COLOR = (170, 170, 170)
MARK_WIDTH = 2
# Solid-bar trim tolerance: how far a pixel may differ from the corner colour
# and still count as "part of the uniform border".
TRIM_TOL = 24

STATE_FILE = espuino.PRINT_STATE_FILE


def cm(value: float) -> int:
    """Centimetres -> pixels at the print DPI."""
    return round(value / 2.54 * DPI)


# --- image preparation ------------------------------------------------------
def _trim_uniform_border(im: Image.Image, tol: int = TRIM_TOL) -> Image.Image:
    """Remove a uniform solid border (e.g. the colour bars YouTube pads a square
    cover with) by cropping to the region that differs from the corner colour.
    Returns the image unchanged if it has no such border."""
    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg).convert("L")
    mask = diff.point(lambda p: 255 if p > tol else 0)
    bbox = mask.getbbox()
    if bbox and bbox != (0, 0, rgb.width, rgb.height):
        return im.crop(bbox)
    return im


def _centre_square(im: Image.Image) -> Image.Image:
    """Centre-crop to the largest centred square."""
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def prepare_card(path: Path, size_px: int, trim: bool = True) -> Image.Image:
    """Load a cover and return a square RGB image of exactly ``size_px`` for the
    card slot: trim solid bars (optional), centre-crop to square, resize."""
    im = Image.open(path)
    im = im.convert("RGB")
    if trim:
        im = _trim_uniform_border(im)
    im = _centre_square(im)
    if im.size != (size_px, size_px):
        im = im.resize((size_px, size_px), Image.LANCZOS)
    return im


# --- page layout ------------------------------------------------------------
def _draw_cut_marks(draw: ImageDraw.ImageDraw, page_w: int, page_h: int,
                    ox: int, oy: int, card_px: int, cols: int, rows: int) -> None:
    """Draw tick marks in the margins at every card boundary (cols+1 verticals,
    rows+1 horizontals), at the top/bottom and left/right paper edges, so a ruler
    can be aligned edge-to-edge for each straight cut."""
    mark = cm(MARK_LEN_CM)
    for i in range(cols + 1):
        x = ox + i * card_px
        draw.line([(x, 0), (x, mark)], fill=MARK_COLOR, width=MARK_WIDTH)
        draw.line([(x, page_h - mark), (x, page_h)], fill=MARK_COLOR, width=MARK_WIDTH)
    for j in range(rows + 1):
        y = oy + j * card_px
        draw.line([(0, y), (mark, y)], fill=MARK_COLOR, width=MARK_WIDTH)
        draw.line([(page_w - mark, y), (page_w, y)], fill=MARK_COLOR, width=MARK_WIDTH)


def page_grid(card_cm: float, cols: int) -> tuple[int, int]:
    """(rows, cards-per-page) that fit on one A4 sheet for this card size/cols."""
    page_w, page_h = cm(A4_CM[0]), cm(A4_CM[1])
    card_px = cm(card_cm)
    rows = max(1, (page_h - 2 * cm(PAGE_MARGIN_CM)) // card_px)
    if cols * card_px > page_w - 2 * cm(PAGE_MARGIN_CM):
        raise SystemExit(f"ERROR: {cols} columns of {card_cm} cm do not fit across A4.")
    return rows, cols * rows


def render_pages(covers: list[Path], card_cm: float, cols: int,
                 marks: bool, trim: bool, log=print) -> list[Image.Image]:
    """Render the covers into one or more A4 page images (top-most row first,
    left-to-right), each page's card block centred on the paper."""
    page_w, page_h = cm(A4_CM[0]), cm(A4_CM[1])
    card_px = cm(card_cm)
    rows, per_page = page_grid(card_cm, cols)

    pages: list[Image.Image] = []
    for start in range(0, len(covers), per_page):
        chunk = covers[start:start + per_page]
        rows_used = (len(chunk) + cols - 1) // cols
        block_w, block_h = cols * card_px, rows_used * card_px
        ox = (page_w - block_w) // 2
        oy = (page_h - block_h) // 2

        page = Image.new("RGB", (page_w, page_h), "white")
        for idx, cover in enumerate(chunk):
            r, c = divmod(idx, cols)
            card = prepare_card(cover, card_px, trim=trim)
            page.paste(card, (ox + c * card_px, oy + r * card_px))
        if marks:
            _draw_cut_marks(ImageDraw.Draw(page), page_w, page_h,
                            ox, oy, card_px, cols, rows_used)
        pages.append(page)
        log(f"  page {len(pages)}: {len(chunk)} card(s)")
    return pages


def save_pdf(pages: list[Image.Image], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", resolution=DPI, save_all=True,
                  append_images=pages[1:])


# --- cover selection --------------------------------------------------------
def collect_covers(root: Path) -> list[Path]:
    """All cover .jpg files under ``root``, sorted by path (so series stay
    grouped and episodes stay in order). Note: espuino.IGNORE_PATTERNS lists
    ``*.jpg`` (to keep images out of the device sync), so it must NOT be applied
    to filenames here — only junk directories are skipped."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and not espuino.is_ignored(d))
        for fn in sorted(filenames):
            if fn.lower().endswith(".jpg") and not fn.startswith("._"):
                found.append(Path(dirpath) / fn)
    return sorted(found)


def record_run(state: PrintState, covers: list[Path], artifact: card_backends.Artifact,
               when: str) -> None:
    """Write a backend's run into the print manifest.

    The ONE place a print is recorded, whoever asked for it and on whatever
    medium: ``cards`` calls it, and so does Studio's cover picker — which is
    what keeps the paper sheet, the card order and the UI's "printed" state
    from drifting apart.
    """
    covers_dir = espuino.DEFAULT_COVERS_DIR
    items = [(str(p.resolve().relative_to(covers_dir.resolve())),
              p.stat().st_size, p.stat().st_mtime) for p in covers]
    state.mark_run(items, artifact.run_out, when)
    # A medium without a file of its own can say what it is instead — a card
    # service records its order page in `url`, next to the covers it placed.
    run = state.runs[-1]
    if artifact.run_out is None:
        run.pop("out", None)          # one spelling per fact: no `out: null`
    run.update(artifact.run_fields or {})
    state.save()


def _report(exc: card_backends.BackendError) -> int:
    """A medium's failure as one line on stderr, flushing stdout first so piped
    output stays in the order it was produced in."""
    sys.stdout.flush()
    print(f"ERROR: {exc}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    # The backend is loaded first: which flags are legal is its business, so it
    # declares its own into the parser below (`cards --backend X --its-flag …`).
    backend = card_backends.load(card_backends.peek_backend(argv))

    parser = argparse.ArgumentParser(
        description=f"Lay card covers out for printing ({backend.LABEL}).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("path", nargs="?",
                        help="Limit to a subpath under card-covers/ (default: all)")
    parser.add_argument("--all", action="store_true",
                        help="Include already-printed covers, not only new ones")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be printed; write and mark nothing")
    parser.add_argument("--no-mark", action="store_true",
                        help="Produce the output but do not mark covers as printed")
    parser.add_argument("--reset", action="store_true",
                        help="Forget print history (optionally limited to PATH) and exit")
    parser.add_argument("--undo", action="store_true",
                        help="Revert the most recent run: its covers count as new "
                             "again and its auto-generated sheet is removed. Repeat "
                             "to step further back. Then exit.")
    parser.add_argument("--backend", default=backend.NAME,
                        help="Which medium to print on ("
                             + ", ".join(e["id"] for e in card_backends.registry()) + ")")
    parser.add_argument("--open", action=argparse.BooleanOptionalAction, default=True,
                        help="Open the result when done (default); --no-open to stay "
                             "put, e.g. on a machine without a browser")
    backend.add_arguments(parser)
    args = parser.parse_args(argv)

    # A flag that is about the medium's own bookkeeping rather than about the
    # covers (list/forget an order) never reaches the selection below.
    preflight = getattr(backend, "preflight", None)
    if preflight is not None:
        try:
            if (code := preflight(args)) is not None:
                return code
        except card_backends.BackendError as exc:
            return _report(exc)

    covers_dir = espuino.DEFAULT_COVERS_DIR
    root = covers_dir
    if args.path:
        p = Path(args.path)
        root = p if p.is_absolute() else covers_dir / args.path
    state = PrintState.load(STATE_FILE)
    if warning := espuino.state_warning("Der Druckverlauf", state.load_status):
        print(f"WARNUNG: {warning}")

    if args.reset:
        rel = "" if not args.path else str(Path(root).resolve().relative_to(covers_dir.resolve()))
        n = state.forget_under(rel)
        state.save()
        scope = f" under {rel!r}" if rel else ""
        print(f"Forgot print history for {n} cover(s){scope}. They now count as new.")
        return 0

    if args.undo:
        run = state.undo_last()
        if run is None:
            print("No print run to undo.")
            return 0
        state.save()
        n = len(run.get("changes", {}))
        print(f"Undid the print run from {run.get('at')}: "
              f"{n} cover(s) count as new again.")
        outp = run.get("out")
        if outp and Path(outp).exists():
            pdf = Path(outp)
            # Only auto-remove the tool's own timestamped sheet, never a custom --out.
            if pdf.parent.resolve() == espuino.PRINT_SHEETS_DIR.resolve():
                pdf.unlink()
                print(f"Removed the superseded sheet {pdf.name}.")
            else:
                print(f"(Left {pdf} in place — delete it yourself if not needed.)")
        return 0

    if not root.exists():
        print(f"ERROR: not found: {root}", file=sys.stderr)
        return 1

    all_covers = collect_covers(root)
    if not all_covers:
        print(f"No cover images (*.jpg) found under {root}.")
        return 0

    def rel_of(p: Path) -> str:
        return str(p.resolve().relative_to(covers_dir.resolve()))

    def is_new(p: Path) -> bool:
        st = p.stat()
        return not state.is_printed(rel_of(p), st.st_size, st.st_mtime)

    covers = all_covers if args.all else [p for p in all_covers if is_new(p)]
    if not covers:
        print(f"Nothing new to print ({len(all_covers)} cover(s) already printed). "
              f"Use --all to reprint, or --reset to forget history.")
        return 0

    kind = "cover(s)" if args.all else "new cover(s)"
    print(f"{len(covers)} {kind} for {backend.LABEL}:")
    for p in covers:
        print(f"  {rel_of(p)}")

    if args.dry_run:
        try:
            extra = backend.describe(covers, args)
        except card_backends.BackendError as exc:
            return _report(exc)
        if extra:
            print(f"\n{extra}")
        print("\n(dry run — nothing written)")
        return 0

    try:
        artifact = backend.emit(covers, args, log=print)
    except card_backends.BackendError as exc:
        return _report(exc)

    if args.no_mark:
        print("(--no-mark: manifest unchanged; these covers still count as new.)")
    else:
        record_run(state, covers, artifact, time.strftime("%Y-%m-%d %H:%M:%S"))
        print(f"Marked {len(covers)} cover(s) as printed "
              f"(undo with ./cards --undo, or ./cards --reset to redo all).")

    if args.open:
        if problem := card_backends.reveal(artifact.open_target):
            print(f"(could not open it: {problem})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
