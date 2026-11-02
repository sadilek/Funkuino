#!/usr/bin/env python3
"""Manifest of which cover images have already been laid out for printing.

Card printing is device-independent (a card maps to a file, not to an ESPuino),
so unlike the sync manifest there is a single state file at the repo root,
``.print-state.json``. It records, per cover image (path relative to the
card-covers dir), the size+mtime it had when we last put it on a print sheet.

This is what makes ``./cards`` with no arguments mean "print everything new":
a cover is considered *new* (unprinted) when it is absent from the manifest, or
when its size/mtime differ from the recorded values (e.g. it was re-downloaded).
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import espuino
from typing import Iterable


def _key(rel: str) -> str:
    """Normalise a relative cover path to a stable manifest key (POSIX + NFC).

    macOS hands filenames in decomposed form (NFD); normalising to NFC keeps the
    key stable regardless of how the path was obtained (see the same reasoning in
    espuino.join_remote)."""
    return unicodedata.normalize("NFC", Path(rel).as_posix())


class PrintState:
    def __init__(self, path: Path, printed: dict, runs: list | None = None):
        self.path = path
        self.printed = printed  # key -> {"size": int, "mtime": float}
        # Undo history: each run records the covers it marked and, for each, the
        # entry that key had *before* the run (None if it was newly marked), so a
        # run can be reverted exactly. Newest run last.
        self.runs = runs if runs is not None else []
        self.load_status = "missing"   # see espuino.read_json_state
        self.backed_up = False         # one previous-good copy per run

    @classmethod
    def load(cls, state_file: Path) -> "PrintState":
        data, status = espuino.read_json_state(Path(state_file))
        state = cls(Path(state_file), data.get("printed", {}), data.get("runs", []))
        state.load_status = status
        return state

    def save(self) -> None:
        espuino.ensure_status_dir()
        if not self.backed_up:
            espuino.keep_backup(self.path)
            self.backed_up = True
        payload = {"printed": self.printed, "runs": self.runs}
        espuino.write_text_atomically(
            self.path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    def mark_run(self, items: list[tuple[str, int, float]], out: str | None,
                 when: str) -> None:
        """Mark each (rel, size, mtime) printed and append one undo entry for the
        whole batch, capturing each key's prior value."""
        changes: dict[str, dict | None] = {}
        for rel, size, mtime in items:
            k = _key(rel)
            if k not in changes:                 # first touch wins as "before"
                changes[k] = self.printed.get(k)
            self.printed[k] = {"size": size, "mtime": mtime}
        self.runs.append({"at": when, "out": out, "changes": changes})

    def undo_last(self) -> dict | None:
        """Revert the most recent run: restore each key to its pre-run value
        (delete if it was newly added). Returns the popped run dict, or None if
        there is no run to undo.

        A run that marks nothing any more is skipped: ``forget_under`` keeps a
        card order's run for its page after pruning its covers (see there), and
        undoing that would revert nothing."""
        index = next((i for i in range(len(self.runs) - 1, -1, -1)
                      if self.runs[i].get("changes")), None)
        if index is None:
            return None
        run = self.runs.pop(index)
        for k, prev in run.get("changes", {}).items():
            if prev is None:
                self.printed.pop(k, None)
            else:
                self.printed[k] = prev
        return run

    def is_printed(self, rel: str, size: int, mtime: float) -> bool:
        entry = self.printed.get(_key(rel))
        if not entry:
            return False
        # mtime is compared with a small tolerance (filesystem timestamp jitter).
        return entry.get("size") == size and abs(entry.get("mtime", 0) - mtime) < 1.0

    def mark_printed(self, rel: str, size: int, mtime: float) -> None:
        self.printed[_key(rel)] = {"size": size, "mtime": mtime}

    def forget(self, rels: Iterable[str]) -> int:
        n = 0
        for rel in rels:
            if self.printed.pop(_key(rel), None) is not None:
                n += 1
        return n

    def forget_under(self, prefix: str) -> int:
        """Drop all printed entries whose key is at or under ``prefix`` (a
        relative cover path). Empty prefix clears everything. Also prunes the
        undo history of the same keys so a later undo cannot resurrect a cover
        that was explicitly forgotten."""
        pref = _key(prefix) if prefix else ""

        def under(k: str) -> bool:
            return not pref or k == pref or k.startswith(pref + "/")

        victims = [k for k in self.printed if under(k)]
        for k in victims:
            del self.printed[k]
        # Prune undo history: drop forgotten keys from each run, then any run
        # that no longer records anything. A run with an order page (``url``,
        # a card service's) stays: the order exists at the service whatever the
        # history says, and its page is the only place it is written down.
        for run in self.runs:
            for k in [k for k in run.get("changes", {}) if under(k)]:
                del run["changes"][k]
        self.runs = [r for r in self.runs if r.get("changes") or r.get("url")]
        return len(victims)
