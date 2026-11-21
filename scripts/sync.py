#!/usr/bin/env python3
"""Mirror the local ``files/`` directory onto the ESPuino over its HTTP API.

rsync-style one-way mirror (local -> device):

* A fast, safe directory listing tells us which files already exist on the
  device.
* A local manifest (.sync-state.json) tells us whether a file's content changed
  since we last uploaded it (the device can't report sizes cheaply).
* New files and changed files are uploaded, strictly one at a time.
* With ``--delete``, files on the device that no longer exist locally are
  removed (true mirror semantics).

Uploads run at ~240 KiB/s, so the first population of a large library takes a
while, but subsequent syncs only touch what changed. See CLAUDE.md for the full
rationale (why HTTP and not FTP, device quirks, etc.).

Examples:
    ./sync                 # upload new/changed files
    ./sync --dry-run       # show what would change, touch nothing
    ./sync --delete        # also remove device files not present locally
    ./sync --host 192.168.1.42
    ./sync --device Katharina   # one of the devices set up in Studio
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import espuino
from sync_state import SyncState

# Device-generated files at the SD root that ``--delete`` must never remove.
REMOTE_PROTECTED = {"/backup.txt"}

# Per-device upload manifests live here as <host>.json.
STATE_DIR = espuino.SYNC_STATE_DIR


def _make_progress(remote_path: str, size: int):
    """A carriage-return progress bar for large uploads (interactive terminals
    only; None for small files or when output is redirected)."""
    if size < 1_000_000 or not sys.stdout.isatty():
        return None
    label = remote_path.rsplit("/", 1)[-1]
    if len(label) > 34:
        label = label[:31] + "..."
    width = 24

    def cb(sent: int, total: int) -> None:
        frac = sent / total if total else 1.0
        bar = "#" * int(frac * width)
        print(f"\r    [{bar:<{width}}] {frac*100:3.0f}%  {sent//1024:>6}/{total//1024} KiB  {label}",
              end="", flush=True)

    return cb


@dataclass
class Stats:
    added: int = 0
    updated: int = 0
    resumed: int = 0
    skipped: int = 0
    seeded: int = 0
    deleted: int = 0
    failed: int = 0
    bytes_sent: int = 0
    files: int = 0              # local files considered
    adoptable: int = 0          # on the device, but not in our manifest
    adoptable_bytes: int = 0
    adopt_offered: bool = False  # stopped before any upload, see offer_adopt


def _local_files(local_root: Path, remote_root: str):
    """(local path, remote path, size, mtime) of every file to mirror."""
    for dirpath, dirnames, filenames in os.walk(local_root):
        dirnames[:] = sorted(d for d in dirnames if not espuino.is_ignored(d))
        rel_dir = os.path.relpath(dirpath, local_root)
        remote_dir = (
            remote_root
            if rel_dir == "."
            else espuino.join_remote(remote_root, *Path(rel_dir).parts)
        )
        for fname in sorted(filenames):
            if espuino.is_ignored(fname):
                continue
            local_path = os.path.join(dirpath, fname)
            yield (local_path, espuino.join_remote(remote_dir, fname),
                   os.path.getsize(local_path), int(os.path.getmtime(local_path)))


def mirror(client: espuino.ESPuino, cfg: espuino.Config, state: SyncState,
           delete: bool, dry_run: bool, adopt_existing: bool = False,
           offer_adopt: bool = False, log=print) -> Stats:
    """Mirror cfg.local_dir onto the device.

    ``offer_adopt`` (Studio): if files the manifest does not know are already
    on the device, return right after the listing with ``adopt_offered`` set,
    before anything is uploaded, so the user can choose between adopting them
    and uploading everything. A dry run always completes and only counts them.
    """
    local_root = Path(cfg.local_dir)
    if not local_root.is_dir():
        raise RuntimeError(f"Local directory does not exist: {local_root}")

    log("Indexing device (fast listing) ...")
    remote_files, remote_dirs = client.remote_index(cfg.remote_root)

    stats = Stats()
    plan = list(_local_files(local_root, cfg.remote_root))
    local_files: set[str] = {remote for _, remote, _, _ in plan}
    stats.files = len(plan)
    for _, remote_path, size, _ in plan:
        if state.get(remote_path) is None and remote_path in remote_files:
            stats.adoptable += 1
            stats.adoptable_bytes += size
    if offer_adopt and stats.adoptable and not adopt_existing and not dry_run:
        stats.adopt_offered = True
        return stats

    for local_path, remote_path, size, mtime in plan:
        exists = remote_path in remote_files
        entry = state.get(remote_path)

        if state.is_unchanged(remote_path, size, mtime):
            stats.skipped += 1
            continue
        if entry is None and exists and adopt_existing:
            # Only on an explicit choice (--adopt-existing, Studio's offer):
            # trust a pre-populated device's file as complete without
            # uploading. Never by default, because we cannot verify a
            # remote file is not a truncated leftover.
            if not dry_run:
                state.adopt(remote_path, size, mtime)
            stats.seeded += 1
            continue

        # Needs upload: new, changed, or a pending (interrupted) upload.
        if state.is_pending(remote_path):
            action = "resume"
        elif exists:
            action = "update"
        else:
            action = "add"
        log(f"  {action:>6}  {remote_path}  ({size:,} B)")
        if not dry_run:
            # Persist intent BEFORE the upload and success AFTER, so an
            # interrupted upload is re-tried next run (never trusted as done).
            state.mark_pending(remote_path)
            prog = _make_progress(remote_path, size)
            try:
                client.upload(local_path, remote_path, progress=prog)
            except Exception as exc:  # noqa: BLE001
                if prog:
                    print()
                # Leave the pending marker so the next run retries this file;
                # don't abort the whole sync for one bad upload.
                log(f"  FAILED  {remote_path}: {exc}")
                stats.failed += 1
                continue
            if prog:
                print()  # finish the progress-bar line
            state.mark_synced(remote_path, size, mtime)
        if action == "add":
            stats.added += 1
        elif action == "update":
            stats.updated += 1
        else:
            stats.resumed += 1
        stats.bytes_sent += size

    if delete:
        _delete_extras(client, state, remote_files, remote_dirs, local_files,
                       local_root, cfg, dry_run, stats, log)

    if not dry_run:
        state.save()
    return stats


def _delete_extras(client, state, remote_files, remote_dirs, local_files,
                   local_root, cfg, dry_run, stats, log) -> None:
    # Safety: never delete when the local side looks empty (wrong path / unmounted).
    if not local_files:
        log("Refusing to delete: no local files found (safety guard).")
        return

    local_dirs = {
        espuino.join_remote(cfg.remote_root, *Path(os.path.relpath(dp, local_root)).parts)
        for dp, _, _ in os.walk(local_root)
        if os.path.relpath(dp, local_root) != "."
    }

    for rp in sorted(remote_files - local_files):
        if rp in REMOTE_PROTECTED:
            continue
        log(f"  {'delete':>6}  {rp}")
        if not dry_run:
            client.delete(rp)
            state.forget(rp)
        stats.deleted += 1

    # Remove now-empty extra directories, deepest first.
    for rp in sorted(remote_dirs - local_dirs, key=lambda p: p.count("/"), reverse=True):
        log(f"  {'rmdir':>6}  {rp}")
        if not dry_run:
            try:
                client.delete(rp)
            except Exception as exc:  # noqa: BLE001
                log(f"          could not remove {rp}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror files/ onto the ESPuino over HTTP.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", help="ESPuino hostname or IP")
    parser.add_argument("--device", help="A device set up in Studio, by name "
                                         "(default: the one chosen in Studio)")
    parser.add_argument("--local-dir", help="Local directory to mirror from")
    parser.add_argument("--remote-root", help="Remote root directory on the device")
    parser.add_argument("--delete", action="store_true",
                        help="Delete device files/dirs that no longer exist locally")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show planned changes without modifying the device")
    parser.add_argument("--adopt-existing", action="store_true",
                        help="Trust files already on the device as complete without "
                             "uploading (one-time seeding of a pre-populated device; "
                             "off by default so partial leftovers are re-uploaded)")
    args = parser.parse_args(argv)

    try:
        cfg = espuino.Config.from_env(
            device=args.device, host=args.host, local_dir=args.local_dir,
            remote_root=args.remote_root)
    except ValueError as exc:
        names = ", ".join(espuino.device_label(d) for d in espuino.saved_devices())
        print(f"ERROR: {exc} Eingerichtet: {names}", file=sys.stderr)
        return 2

    client = espuino.ESPuino(cfg)

    # Identify the device by MAC (its stable identity) so the right per-device
    # manifest is used regardless of whether we reached it by name or IP.
    try:
        info = client.info()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot reach ESPuino at {cfg.host}: {exc}", file=sys.stderr)
        return 1
    mac = (info.get("wifi") or {}).get("macAddress") or ""
    device_id = mac or cfg.host  # fall back to host if firmware omits the MAC

    print(f"ESPuino sync -> {cfg.host} [{mac or 'MAC unknown'}]  (local: {cfg.local_dir})")
    if args.dry_run:
        print("DRY RUN - no changes will be made.\n")

    state = SyncState.load(STATE_DIR, device_id=device_id, host=cfg.host)
    if warning := espuino.state_warning("Der Abgleichstand", state.load_status):
        # Loud: without it every file counts as new and the whole library is
        # uploaded again, which looks like a bug rather than a recovery.
        print(f"WARNUNG: {warning}")

    start = time.monotonic()
    try:
        stats = mirror(client, cfg, state, delete=args.delete,
                       dry_run=args.dry_run, adopt_existing=args.adopt_existing, log=print)
    except Exception as exc:  # noqa: BLE001 - clean CLI message
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - start
    print(
        f"\nDone in {elapsed:.1f}s: {stats.added} added, {stats.updated} updated, "
        f"{stats.resumed} re-uploaded, {stats.seeded} already-present, "
        f"{stats.skipped} unchanged, {stats.deleted} deleted, "
        f"{stats.failed} failed, {stats.bytes_sent / 1_048_576:.1f} MiB sent."
    )
    if args.dry_run and stats.adoptable and not args.adopt_existing:
        print(f"NOTE: {stats.adoptable} file(s) listed as 'update' are already on the device but "
              "were never uploaded by this tool (e.g. a card filled at a computer). "
              "If they are complete copies, --adopt-existing takes them over "
              "instead of uploading them again.")
    if stats.failed:
        print(f"NOTE: {stats.failed} file(s) failed and stay pending; run ./sync again to retry.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
