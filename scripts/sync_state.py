#!/usr/bin/env python3
"""Local upload manifest for the ESPuino mirror.

The device offers no cheap way to read a remote file's size (see espuino.py), so
we cannot tell "changed" from "unchanged" by asking the device. Instead we keep
a local record of what we last uploaded -- remote path -> (size, mtime) -- and
compare the local files against it. Existence is still verified against the
device (a fast, safe directory listing); this manifest only answers "did the
*content* change since we last pushed it?".

The manifest is keyed by the device's MAC address (its stable hardware
identity), so the same files can be mirrored to several ESPuinos independently,
and one device reached under different names (hostname vs IP) still shares a
single manifest. The host name is kept only as a human-readable label. Manifests
are machine-specific and git-ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import espuino


def _slug(value: str) -> str:
    """Filesystem-safe filename component (MAC address, host, …)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "device"


def _absolute_keys(files: dict[str, dict]) -> dict[str, dict]:
    """Make every manifest key device-absolute, as join_remote now produces.

    A remote_root without a leading slash ("mp3") used to yield keys like
    "mp3/x.mp3"; left as they are, every file would look new after the update
    and the next sync would re-upload the whole library. An absolute key
    already present wins: it was written later."""
    out = {k: v for k, v in files.items() if k.startswith("/")}
    for key, entry in files.items():
        if not key.startswith("/"):
            out.setdefault("/" + key, entry)
    return out


@dataclass
class SyncState:
    path: Path
    device_id: str = ""  # MAC address (stable per device)
    host: str = ""       # last host label used to reach it
    files: dict[str, dict] = field(default_factory=dict)
    load_status: str = "missing"   # see espuino.read_json_state
    backed_up: bool = False        # one previous-good copy per run

    @staticmethod
    def file_name(device_id: str) -> str:
        return f"sync-{_slug(device_id)}.json"

    @classmethod
    def load(cls, state_dir: str | Path, device_id: str, host: str = "") -> "SyncState":
        """Load (or start) the manifest for ``device_id`` inside ``state_dir``."""
        path = Path(state_dir) / cls.file_name(device_id)
        data, status = espuino.read_json_state(path)
        return cls(
            path=path,
            device_id=data.get("device_id", device_id),
            host=host or data.get("host", ""),
            files=_absolute_keys(data.get("files", {})),
            load_status=status,
        )

    def save(self) -> None:
        espuino.ensure_status_dir()
        if not self.backed_up:
            espuino.keep_backup(self.path)
            self.backed_up = True
        payload = {"device_id": self.device_id, "host": self.host, "files": self.files}
        espuino.write_text_atomically(
            self.path, json.dumps(payload, indent=1, ensure_ascii=False))

    def get(self, remote_path: str) -> dict | None:
        return self.files.get(remote_path)

    def is_pending(self, remote_path: str) -> bool:
        """True if an upload was started for this file but never confirmed
        complete (e.g. the previous run crashed or was Ctrl+C'd mid-upload)."""
        entry = self.files.get(remote_path)
        return bool(entry) and bool(entry.get("pending"))

    def is_unchanged(self, remote_path: str, size: int, mtime: int) -> bool:
        """True only if this file was fully uploaded (not pending) and matches
        the recorded size and is no newer than the recorded mtime."""
        entry = self.files.get(remote_path)
        return (bool(entry) and not entry.get("pending")
                and entry.get("size") == size and entry.get("mtime", 0) >= mtime)

    def mark_pending(self, remote_path: str) -> None:
        """Record that an upload is about to start, and persist immediately so a
        crash mid-upload is remembered and the file is re-uploaded next time."""
        self.files[remote_path] = {"pending": True}
        self.save()

    def mark_synced(self, remote_path: str, size: int, mtime: int) -> None:
        """Record a confirmed-complete upload and persist immediately."""
        self.files[remote_path] = {"size": size, "mtime": mtime}
        self.save()

    def adopt(self, remote_path: str, size: int, mtime: int) -> None:
        """Trust a file that already exists on the device and was never touched
        by us (pre-populated library). Not persisted per-call; the final save()
        writes it (re-derived harmlessly if the run is interrupted first)."""
        self.files[remote_path] = {"size": size, "mtime": mtime}

    def forget(self, remote_path: str) -> None:
        self.files.pop(remote_path, None)
        self.save()
