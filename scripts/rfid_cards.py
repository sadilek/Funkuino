#!/usr/bin/env python3
"""RFID card assignments shared by every ESPuino on one library.

Each device keeps its own card table in NVS (``GET/POST/DELETE /rfid``). With
two boxes on one library a card taught on one should play the same on the
other, so the library keeps one shared table, ``status/rfid-cards.json``, and
Studio reconciles every device it reaches against it.

Entries are stored device-independently:

* ``{"path": "<library-relative path>", "playMode": n}`` for a file or folder
  below the device's base folder (``remote_root``), so it maps to a unit and
  to each device's own path;
* ``{"fileOrUrl": "...", "playMode": n}`` for anything else (a web radio URL,
  a path outside the base folder), passed on verbatim;
* ``{"modId": n}`` for a modification card (sleep timer, lock, …).

Besides the table the file remembers, per device MAC, the table that device
had when we last looked (``seen``). That is what tells a change made *at the
box* (its own web UI) from a box that is merely behind:

* an entry that differs from ``seen`` was changed at the box → it wins and is
  learnt into the shared table, to go out to the other devices;
* an entry that equals ``seen`` but not the shared table is stale → the shared
  value is pushed;
* a device seen for the first time adds the cards the table lacks and gets the
  rest; on a conflict the shared table wins (it is what the other boxes play).

A card removed in Studio leaves a tombstone in ``deleted``, so a device that
was offline at the time drops it at its next reconcile. A card missing on a
device is pushed again, never treated as a deletion: a box whose NVS was wiped
must get its cards back, not wipe them from every other box.

Pushing only what differs matters beyond traffic: ``POST /rfid`` also resets
the card's saved play position (lastPlayPos), so re-sending an unchanged entry
would make an audiobook start over.

The logic here is pure (no I/O besides load/save); studio.py does the device
calls and reports back with :meth:`confirm_pushed` / :meth:`confirm_deleted`.
"""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import espuino

MOD_ID_MIN = 100   # firmware: a stored mode >= 100 is a modification card


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def canonical(raw: dict, remote_root: str) -> dict | None:
    """A device's ``/rfid`` entry -> the stored form (None if unusable)."""
    if not isinstance(raw, dict):
        return None
    if raw.get("modId") is not None:
        try:
            mod = int(raw["modId"])
        except (TypeError, ValueError):
            return None
        return {"modId": mod} if mod > 0 else None
    try:
        mode = int(raw.get("playMode"))
    except (TypeError, ValueError):
        return None
    if mode <= 0:
        return None
    file_or_url = str(raw.get("fileOrUrl") or "")
    if not file_or_url:
        return None
    if "://" not in file_or_url:
        rel = _relative(file_or_url, remote_root)
        if rel:
            return {"path": rel, "playMode": mode}
    return {"fileOrUrl": file_or_url, "playMode": mode}


def _relative(device_path: str, remote_root: str) -> str:
    """Device path -> library-relative path ("" if outside the base folder);
    the inverse of espuino.join_remote, NFC-normalised."""
    s = _nfc(device_path.lstrip("/"))
    root = _nfc((remote_root or "").strip("/"))
    if not root:
        return s
    return s[len(root) + 1:] if s.startswith(root + "/") else ""


def to_device(entry: dict, remote_root: str) -> dict:
    """The stored form -> the fields of a ``POST /rfid`` body (without id)."""
    if "modId" in entry:
        return {"modId": entry["modId"]}
    if "path" in entry:
        return {"fileOrUrl": espuino.join_remote(remote_root, *Path(entry["path"]).parts),
                "playMode": entry["playMode"]}
    return {"fileOrUrl": entry["fileOrUrl"], "playMode": entry["playMode"]}


@dataclass
class Plan:
    """What reconciling one device needs: tags to POST (with their stored
    entry), tags to DELETE, and the tags learnt from it (changed at the box)."""
    push: list[tuple[str, dict]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)


@dataclass
class CardRegistry:
    path: Path
    cards: dict[str, dict] = field(default_factory=dict)
    deleted: dict[str, float] = field(default_factory=dict)   # tag -> when
    seen: dict[str, dict[str, dict]] = field(default_factory=dict)  # mac -> tag -> entry
    hosts: dict[str, str] = field(default_factory=dict)  # mac -> address last used
    load_status: str = "missing"
    backed_up: bool = False

    @classmethod
    def load(cls, path: str | Path = espuino.RFID_CARDS_FILE) -> "CardRegistry":
        path = Path(path)
        data, status = espuino.read_json_state(path)

        def table(value) -> dict[str, dict]:
            return ({str(k): v for k, v in value.items() if isinstance(v, dict)}
                    if isinstance(value, dict) else {})

        seen = data.get("seen")
        hosts = data.get("hosts")
        return cls(
            path=path,
            cards=table(data.get("cards")),
            deleted={str(k): float(v) for k, v in (data.get("deleted") or {}).items()
                     if isinstance(v, (int, float))},
            seen={str(mac): table(t) for mac, t in seen.items()} if isinstance(seen, dict) else {},
            hosts={str(k): str(v) for k, v in hosts.items()} if isinstance(hosts, dict) else {},
            load_status=status,
        )

    @property
    def known(self) -> bool:
        """Has any device ever been read (or a card been assigned)? Until then
        the UI shows "unknown" rather than "no card"."""
        return bool(self.cards or self.seen or self.deleted)

    def save(self) -> None:
        espuino.ensure_status_dir()
        if not self.backed_up:
            espuino.keep_backup(self.path)
            self.backed_up = True
        payload = {"cards": self.cards, "deleted": self.deleted, "seen": self.seen,
                   "hosts": self.hosts}
        espuino.write_text_atomically(
            self.path, json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True))

    # -- changes made in Studio --
    def assign(self, tag: str, entry: dict) -> None:
        self.cards[tag] = entry
        self.deleted.pop(tag, None)

    def unassign(self, tag: str) -> None:
        self.cards.pop(tag, None)
        self.deleted[tag] = time.time()

    # -- reconciling a device --
    def key_for(self, mac: str, host: str) -> str:
        """The key a device's ``seen`` table is under: its MAC, or -- before
        this run has read the device -- the MAC last seen at that address."""
        if mac:
            return mac
        return next((k for k, h in self.hosts.items() if h == host), "")

    def plan(self, mac: str, device: dict[str, dict], host: str = "") -> Plan:
        """Merge a device's table (tag -> stored form) into the shared one and
        say what the device needs. Mutates ``cards`` (learnt entries) and
        ``seen[mac]`` (now = what the device has); save afterwards."""
        plan = Plan()
        if host:
            self.hosts = {k: h for k, h in self.hosts.items() if h != host}
            self.hosts[mac] = host
        last = self.seen.get(mac)
        for tag, value in device.items():
            if self.cards.get(tag) == value:
                continue
            if last is None:
                # First look at this device: add what the table lacks, unless
                # it was deleted on purpose; a conflict is the table's.
                if tag not in self.cards and tag not in self.deleted:
                    self.cards[tag] = value
                    plan.learned.append(tag)
            elif last.get(tag) != value:
                # Changed at the box since we last looked: that wins.
                self.cards[tag] = value
                self.deleted.pop(tag, None)
                plan.learned.append(tag)
            elif tag not in self.cards and tag not in self.deleted:
                self.cards[tag] = value      # defensive: table lost the entry
                plan.learned.append(tag)
        plan.push = [(tag, entry) for tag, entry in sorted(self.cards.items())
                     if device.get(tag) != entry]
        plan.delete = sorted(tag for tag in device
                             if tag not in self.cards and tag in self.deleted)
        self.seen[mac] = dict(device)
        return plan

    def confirm_pushed(self, mac: str, tag: str, entry: dict) -> None:
        self.seen.setdefault(mac, {})[tag] = entry

    def confirm_deleted(self, mac: str, tag: str) -> None:
        self.seen.setdefault(mac, {}).pop(tag, None)

    def pending(self, key: str) -> set[str]:
        """Tags a device (by :meth:`key_for`) does not have as the table says
        (all of them for a device never read: we cannot know)."""
        last = self.seen.get(key) if key else None
        if last is None:
            return set(self.cards)
        out = {tag for tag, entry in self.cards.items() if last.get(tag) != entry}
        out |= {tag for tag in last if tag not in self.cards and tag in self.deleted}
        return out
