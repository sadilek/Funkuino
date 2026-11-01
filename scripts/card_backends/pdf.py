"""The shipped backend: square cards on sheets of A4, cut out and laminated.

The drawing lives in ``cards`` — the solid-bar trim, the card size, the cut
marks and the page layout are the paper medium's own rules, and Studio's cover
picker calls them directly. This module is the seam's side of a run: what it
produces, and what it says while doing it.
"""

from __future__ import annotations

import time
from pathlib import Path

import cards
import espuino

from . import Artifact

NAME = "pdf"
LABEL = "A4-Bogen (Papier)"
PAGE = True


def add_arguments(parser) -> None:
    parser.add_argument("--cols", type=int, default=cards.DEFAULT_COLS,
                        help="Cards per row")
    parser.add_argument("--card-cm", type=float, default=cards.DEFAULT_CARD_CM,
                        help="Card edge length in centimetres")
    parser.add_argument("--no-marks", action="store_true",
                        help="Do not draw cut-guide tick marks")
    parser.add_argument("--no-trim", action="store_true",
                        help="Do not trim solid side bars before squaring")
    parser.add_argument("--out",
                        help="Output PDF path (default: print-sheets/cards-<timestamp>.pdf)")


def _per_page(args) -> int:
    return cards.page_grid(args.card_cm, args.cols)[1]


def _partial_page_note(count: int, args) -> str | None:
    """Full pages only laminate well, so flag a partial last page and say how
    many more covers would fill it."""
    per_page = _per_page(args)
    short = (-count) % per_page
    if not short:
        return None
    return (f"NOTE: the last page has {per_page - short}/{per_page} cards. "
            f"Add {short} more cover(s) for a full page — or './cards --undo' "
            f"to drop this run, download more, and reprint a full sheet.")


def describe(covers, args) -> str:
    rows, per_page = cards.page_grid(args.card_cm, args.cols)
    note = _partial_page_note(len(covers), args)
    return (f"A4 sheet: {args.cols} per row, {args.card_cm:g}x{args.card_cm:g} cm, "
            f"{per_page} per page ({rows} rows)."
            + (f"\n{note}" if note else ""))


def emit(covers, args, log=print) -> Artifact:
    pages = cards.render_pages(covers, args.card_cm, args.cols,
                               marks=not args.no_marks, trim=not args.no_trim, log=log)
    out = Path(args.out) if args.out else (
        espuino.PRINT_SHEETS_DIR / f"cards-{time.strftime('%Y%m%d-%H%M%S')}.pdf")
    cards.save_pdf(pages, out)
    log(f"\nWrote {out}  ({len(pages)} page(s), {len(covers)} card(s)).")
    if note := _partial_page_note(len(covers), args):
        log(note)
    return Artifact(run_out=str(out), open_target=str(out))
