#!/usr/bin/env python3
"""Funkuino Studio -- one localhost web app for the whole ESPuino workflow.

A single-page dashboard over the existing (synchronous) Funkuino tooling: library
state per RFID card, device sync with a live log, the card-printing bridge, and
an embedded Claude agent (Claude Agent SDK on the user's subscription token from
``.env``) that drives URL -> download -> naming/merge fixes and escalates
questions to the browser.

Design notes:
* Everything that touches the device is blocking and funnelled through one
  ``espuino.ESPuino`` client and a single ``asyncio.Lock`` (the device wedges on
  concurrent storage ops); blocking work runs in the default executor.
* Device reachability never blocks the dashboard: ``/api/state`` returns the
  cached last ``/info`` and a background ping refreshes it.
* Server->client events go over one multiplexed websocket, each stamped with a
  global increasing ``seq``; agent events are also kept per session for replay
  after a reload, and on disk so sessions outlive a restart (studio_agent).

Bind is 127.0.0.1 only. The OAuth token is read from ``.env`` and handed to the
Agent SDK via its options env -- it is never logged or returned.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import errno
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import requests
from aiohttp import WSCloseCode, web

import cards
import card_backends
import espuino
import rfid_cards
import studio_agent
import studio_state
import sync
from print_state import PrintState, _key
from sync_state import SyncState

# Relative to this module, not to the code root: installed as a wheel there is
# no "scripts" directory below the root — the package *is* it.
WEB_DIR = Path(__file__).resolve().parent / "studio_web"
DEFAULT_PORT = 8800
PING_TIMEOUT = 4.0  # seconds; keep the dashboard responsive when the device is off
THUMB_CACHE_MAX = 200   # LRU cap on rendered thumbnails
WS_QUEUE_MAX = 500      # per-client outbound backlog before dropping oldest events
RFID_PING_SECS = 3.0    # device-ws keepalive interval (mimic the stock UI)
RFID_BACKOFF_MIN = 5.0  # reconnect backoff floor / cap while the device is offline
RFID_BACKOFF_MAX = 60.0
RFID_ID_RE = re.compile(r"\d{12}", re.ASCII)  # card id = exactly 12 ASCII digits
PLAYER_NO_PLAYLIST = 0    # firmware playMode when nothing is playing (values.h)
_TRACK_PREFIX_RE = re.compile(r"^\(\d+/\d+\):\s*")  # firmware "(N/M): " title prefix
FRONTEND_FILES = ("index.html", "style.css", "app.js")
PLACEHOLDER = ("Studio frontend missing (scripts/studio_web/index.html). "
               "The API is up; only the UI is absent.")


def _creation_ts(st) -> float:
    """Best creation timestamp: macOS st_birthtime, else mtime."""
    return getattr(st, "st_birthtime", None) or st.st_mtime


def _device_error_msg(exc: Exception) -> str:
    """A short German message for a unit-sync failure."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "Gerät nicht erreichbar"
    return str(exc)


def _de_secs(elapsed: float) -> str:
    """Seconds with a German decimal comma, e.g. 0.5 -> '0,5'."""
    return f"{elapsed:.1f}".replace(".", ",")


def _sync_summary(stats, elapsed: float, dry_run: bool) -> str:
    """A German one-line closing summary mirroring sync.main()'s CLI line."""
    uploaded = stats.added + stats.updated + stats.resumed
    parts = [f"{uploaded} hochgeladen", f"{stats.deleted} gelöscht",
             f"{stats.skipped} unverändert"]
    if stats.seeded:
        parts.insert(1, f"{stats.seeded} übernommen")
    if stats.failed:
        parts.append(f"{stats.failed} fehlgeschlagen")
    line = f"Fertig in {_de_secs(elapsed)} s: " + ", ".join(parts)
    return line + " — Dry-Run" if dry_run else line


# Where the Claude Code CLI installs itself. The agent SDK searches the same
# places; we look too, so the setup screen can say whether it is there at all
# instead of only reporting a missing token.
CLI_LOCATIONS = (
    Path.home() / ".local/bin/claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
    Path.home() / ".claude/local/claude",
    Path.home() / ".npm-global/bin/claude",
)


def find_claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    return next((str(p) for p in CLI_LOCATIONS if p.is_file()), None)


class DeviceConn:
    """One configured ESPuino and everything Studio learnt from it.

    Studio keeps a connection to every configured device at once, not only to
    the one the switcher shows: card assignments go out to a box as soon as it
    is reachable, and the switcher can say which boxes are online. Each has
    its own lock, since one storage operation at a time is a per-device limit.
    """

    def __init__(self, spec: dict, cfg: espuino.Config):
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "")
        self.cfg = cfg
        self.mac = ""                                   # from /info (wifi.macAddress)
        self.client: espuino.ESPuino | None = None      # built lazily (DNS -> executor)
        self.lock = asyncio.Lock()                      # serialises every device op
        self.info: dict | None = None                   # last successful /info
        self.reachable = False                          # last attempt result
        self.attempted = False                          # was it ever tried?
        self.error: str | None = None                   # why the last attempt failed
        self.listening = False                          # device-ws listener connected
        self.task: asyncio.Task | None = None           # the listener
        self.ws: aiohttp.ClientWebSocketResponse | None = None  # reused for controls
        self.ws_send_lock = asyncio.Lock()
        self.cards_checked: float | None = None         # last card reconcile
        # Now-playing (from the listener's trackinfo broadcasts).
        self.player = dict(IDLE_PLAYER)
        self.player_last_unit: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.cfg.host

    @property
    def key(self) -> str:
        """Its identity in the sync manifests and the card table."""
        return self.mac or self.cfg.host


IDLE_PLAYER = {"playing": False, "unitId": None, "name": "", "pausePlay": False}


def failure_reason(exc: BaseException) -> str:
    """A short, human answer to "why is this box offline?" from whatever a
    connect raised (requests, urllib3 and aiohttp each wrap the socket error
    one or two levels deep): the innermost OS error wins, since that is what
    tells a box that is off from a Mac that refuses to talk to it."""
    chain, seen = [], set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    for err in reversed(chain):
        if isinstance(err, socket.gaierror):     # its errno is an EAI_* code
            return f"Name nicht auflösbar ({err.strerror})"
        if isinstance(err, OSError) and err.errno is not None:
            # os.strerror, not err.strerror: asyncio puts "Connect call
            # failed ('ip', port)" there, while requests gets the plain text.
            text = os.strerror(err.errno)
            if err.errno == errno.EHOSTUNREACH and sys.platform == "darwin":
                # Measured: a box that is off answers EHOSTDOWN; EHOSTUNREACH
                # for a LAN address is macOS's Local Network block, which spares
                # ping and curl and hit a terminal tab older than an iTerm update.
                text += (" — macOS blockiert das lokale Netz für diesen Prozess."
                         " Studio aus einem neuen Terminalfenster starten oder"
                         " die Freigabe „Lokales Netzwerk“ prüfen")
            return text
    if any(isinstance(err, (TimeoutError, requests.Timeout)) for err in chain):
        return "keine Antwort (Zeitüberschreitung)"
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 200 else text[:199] + "…"


class Studio:
    def __init__(self, cfg: espuino.Config, token: str | None,
                 port: int = DEFAULT_PORT, cards_enabled: bool = True,
                 agent_enabled: bool = True, open_browser: bool = True,
                 overrides: dict[str, str] | None = None):
        self.token = token
        self.port = port
        self.cards_enabled = cards_enabled  # off: hide print tab/column, 404 its API
        self.agent_enabled = agent_enabled  # off: hide agent tab, 404 its API
        self.open_browser = open_browser    # only matters at start; shown in settings
        # Settings pinned for this run by a start parameter or an environment
        # variable: key -> the flag/variable that pinned it (see SETTINGS).
        self.overrides = overrides or {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.agent: studio_agent.SessionManager | None = None

        # Devices: one connection per configured ESPuino; the switcher picks
        # the one the Bibliothek, Sync and the player act on.
        self.devices: dict[str, DeviceConn] = {}
        self.active_id = ""
        self._build_devices(cfg)

        # Cards shared by all devices (status/rfid-cards.json). One lock, so
        # two devices reconciling at once cannot interleave their changes.
        self.registry = rfid_cards.CardRegistry.load()
        self.registry_lock = asyncio.Lock()
        if w := espuino.state_warning("Die Kartenzuordnung", self.registry.load_status):
            print(f"WARNUNG: {w}", file=sys.stderr)

        # Jobs (one sync, one cards render/CLI at a time; unit syncs by id).
        self.sync_running = False
        self.cards_running = False
        self.unit_sync_ids: set[str] = set()  # unit ids currently uploading

        # WS hub.
        self.ws_queues: set[asyncio.Queue] = set()
        self.ws_clients: set[web.WebSocketResponse] = set()  # closed on shutdown
        # Starts at the clock (ms), not 0, so it keeps rising across restarts:
        # agent sessions come back with the seqs of the run that logged them,
        # and a tab left open over a restart drops every seq it thinks it has
        # already applied. Even a sync log line per ms would not catch up.
        self._seq = int(time.time() * 1000)

        # Caches.
        self._state_cache: tuple[list[dict], dict | None] | None = None
        self._ffmpeg: dict | None = None      # see _ffmpeg_state()
        self._thumbs: "OrderedDict[str, bytes]" = OrderedDict()
        self._unit_ids: set[str] = set()  # ids from the last scan (assignment mapping)
        self._assignment_by_id: dict[str, dict] = {}    # id -> enriched assignment

        self._ws_session: aiohttp.ClientSession | None = None
        self._shutting_down = False
        self._intake_task: asyncio.Task | None = None  # polls session progress files

    # -- devices --
    def _device_specs(self) -> tuple[list[dict], str]:
        """The devices to connect to and the active one's id. A --host (or
        ESPUINO_HOST) pins a single device for this run, as it pinned the one
        device before there could be several."""
        config = espuino.read_app_config()
        saved = espuino.saved_devices(config)
        if "host" in self.overrides:
            host = os.environ.get("ESPUINO_HOST") or ""
            spec = next((d for d in saved if d["host"] == host),
                        {"id": espuino.LEGACY_DEVICE_ID, "name": "", "host": host})
            return [spec], spec["id"]
        return saved, espuino.active_device(config)["id"]

    def _build_devices(self, base: espuino.Config) -> None:
        specs, active = self._device_specs()
        self.devices = {s["id"]: DeviceConn(s, dataclasses.replace(base, host=s["host"]))
                        for s in specs}
        self.active_id = active

    @property
    def active(self) -> DeviceConn:
        return self.devices[self.active_id]

    @property
    def cfg(self) -> espuino.Config:
        """The active device's settings (library folder, base folder and login
        are the same for every device)."""
        return self.active.cfg

    # -- websocket hub --
    def emit(self, msg: dict) -> int:
        """Stamp a message with the next global seq and fan it out. Loop-thread only."""
        self._seq += 1
        msg = {**msg, "seq": self._seq}
        for q in list(self.ws_queues):
            if q.full():  # stalled tab: drop its oldest event to cap memory
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)
        return self._seq

    def emit_agent(self, session_id: str, event: dict) -> int:
        return self.emit({"t": "agent.event", "sessionId": session_id,
                          "event": event})

    def emit_threadsafe(self, msg: dict) -> None:
        """Emit from an executor thread (e.g. sync's log callback)."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.emit, msg)

    # -- device client --
    async def _get_client(self, dev: DeviceConn) -> espuino.ESPuino:
        """Build the client once (DNS resolves in the constructor -> executor)."""
        if dev.client is None:
            dev.client = await self.loop.run_in_executor(
                None, lambda: espuino.ESPuino(dev.cfg))
        return dev.client

    def _learn_info(self, dev: DeviceConn, info: dict) -> None:
        dev.info = info
        dev.mac = (info.get("wifi") or {}).get("macAddress") or dev.mac

    async def ping_device(self, dev: DeviceConn | None = None) -> dict | None:
        dev = dev or self.active
        async with dev.lock:
            dev.attempted = True
            try:
                client = await self._get_client(dev)
                info = await asyncio.wait_for(
                    self.loop.run_in_executor(None, client.info), PING_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - offline is a normal state
                dev.reachable = False
                self._note_failure(dev, exc)
                return None
            self._learn_info(dev, info)
            dev.reachable = True
            self._note_success(dev)
            return info

    def _note_failure(self, dev: DeviceConn, exc: BaseException) -> None:
        """Keep why a box is offline, and say so on the console once per new
        reason (the listener retries every 5-60 s; a line per retry would
        bury everything else). Offline used to be a bare "offline": a Mac
        refusing the connection looked exactly like a box that is off."""
        reason = failure_reason(exc)
        if reason == dev.error:
            return
        dev.error = reason
        print(f"{dev.label} ({dev.cfg.host}) nicht erreichbar: {reason}",
              file=sys.stderr, flush=True)
        self.emit({"t": "state.changed"})

    def _note_success(self, dev: DeviceConn) -> None:
        if dev.error is not None:
            print(f"{dev.label} ({dev.cfg.host}) wieder erreichbar",
                  file=sys.stderr, flush=True)
            dev.error = None

    # -- RFID device-ws listener (passive, read-only; no device lock needed) --
    async def _rfid_listener(self, dev: DeviceConn) -> None:
        """Keep a websocket to the device open, translating ``rfidId`` broadcasts
        into browser events. Reconnects with backoff while the device is offline.
        Listening is passive (tiny frames) so it does not use the device lock."""
        backoff = RFID_BACKOFF_MIN
        while not self._shutting_down:
            try:
                name, port = espuino.split_host(dev.cfg.host, dev.cfg.http_port)
                addr = await self.loop.run_in_executor(
                    None, lambda: espuino.ESPuino._resolve(name))
                url = f"ws://{addr}:{port}/ws"
                # receive_timeout (~12 s = 4x the 3 s ping) is what flips the
                # device to "offline" within ~10-15 s when it is powered off:
                # no frames arrive, the receive times out, and we fall through.
                async with self._ws_session.ws_connect(
                        url, autoping=True, heartbeat=None,
                        receive_timeout=RFID_PING_SECS * 4) as ws:
                    dev.listening = True
                    dev.ws = ws                                  # reuse for controls
                    backoff = RFID_BACKOFF_MIN
                    self._note_success(dev)
                    self._set_reachable(dev, True)               # online (on transition)
                    # One /info read, then the card table: the MAC keys it.
                    self.loop.create_task(self._on_connected(dev))
                    with contextlib.suppress(Exception):
                        await ws.send_json({"trackinfo": {}})    # warm now-playing
                    ping = self.loop.create_task(self._rfid_ping(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_device_msg(dev, msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ping  # retrieve it so it can't orphan an exception
            except Exception as exc:  # noqa: BLE001 - offline/reset is normal, just retry
                self._note_failure(dev, exc)
            dev.listening = False
            dev.ws = None
            self._reset_player(dev)                             # clear stale now-playing
            self._set_reachable(dev, False)                     # offline (on transition)
            if self._shutting_down:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RFID_BACKOFF_MAX)

    def _set_reachable(self, dev: DeviceConn, reachable: bool) -> None:
        """Update device reachability from the ws listener and push a
        state.changed only on an actual transition (so repeated offline
        reconnect attempts don't spam the dashboard)."""
        if dev.reachable == reachable and dev.attempted:
            return
        dev.reachable = reachable
        dev.attempted = True
        self.emit({"t": "state.changed"})

    async def _on_connected(self, dev: DeviceConn) -> None:
        await self._refresh_info(dev)
        await self._reconcile_cards(dev)

    async def _refresh_info(self, dev: DeviceConn) -> None:
        """Refresh the cached /info once after a (re)connect (executor + lock)."""
        try:
            async with dev.lock:
                client = await self._get_client(dev)
                info = await self.loop.run_in_executor(None, client.info)
        except Exception:  # noqa: BLE001
            return
        self._learn_info(dev, info)

    async def _rfid_ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(RFID_PING_SECS)
            # A send on a dropping socket may raise; swallow it (the receive loop
            # detects the drop) so this task never orphans an exception.
            with contextlib.suppress(Exception):
                await ws.send_json({"ping": {"ping": "ping"}})

    def _handle_device_msg(self, dev: DeviceConn, data: str) -> None:
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        if "rfidId" in obj:                        # ignore pong/status/volume/…
            self._emit_rfid_card(dev, str(obj["rfidId"]))
        elif "trackinfo" in obj:
            self._handle_trackinfo(dev, obj.get("trackinfo") or {})

    def _emit_rfid_card(self, dev: DeviceConn, tag_id: str) -> None:
        """A card placed on any box: the assignment is shared, so the banner
        offers the same either way; it names the box when there are several."""
        a = self._assignment_by_id.get(tag_id)
        self.emit({"t": "rfid.card", "id": tag_id, "known": a is not None,
                   "device": dev.label if len(self.devices) > 1 else None,
                   "assignment": None if a is None else {
                       "fileOrUrl": a["fileOrUrl"], "playMode": a["playMode"],
                       "unitId": a["unitId"]}})

    # -- now-playing tracking (from trackinfo broadcasts) --
    def _resolve_playing_unit(self, dev: DeviceConn, name: str) -> str | None:
        """Best-effort map a trackinfo ``name`` to a unit id. The firmware sends
        either the file path (at track start) or the ID3 title (after tags load),
        optionally prefixed with "(N/M): ". Only the path form resolves: a file
        unit matches its id exactly; a folder unit (album/folge) matches when the
        path is inside it."""
        stripped = _TRACK_PREFIX_RE.sub("", name or "").strip()
        if "/" not in stripped:
            return None                             # an ID3 title, not a path
        cand = studio_state.assignment_key(stripped, dev.cfg.remote_root)  # strip leading slash + remote_root + NFC
        if cand in self._unit_ids:
            return cand
        best = None
        for uid in self._unit_ids:
            if cand.startswith(uid + "/") and (best is None or len(uid) > len(best)):
                best = uid
        return best

    def _handle_trackinfo(self, dev: DeviceConn, ti: dict) -> None:
        play_mode = ti.get("playMode")
        num = ti.get("numberOfTracks", 0)
        if play_mode in (None, PLAYER_NO_PLAYLIST) or not num:
            self._reset_player(dev)
            return
        name = str(ti.get("name") or "")
        pause = bool(ti.get("pausePlay"))
        uid = self._resolve_playing_unit(dev, name)
        if uid is not None:
            dev.player_last_unit = uid
        else:
            uid = dev.player_last_unit              # sticky across ID3/path swaps
        self._update_player(dev, {"playing": not pause, "unitId": uid,
                                  "name": name, "pausePlay": pause})

    def _reset_player(self, dev: DeviceConn) -> None:
        dev.player_last_unit = None
        self._update_player(dev, dict(IDLE_PLAYER))

    def _update_player(self, dev: DeviceConn, new: dict) -> None:
        """Keep each box's now-playing; only the active one's reaches the page."""
        if new != dev.player:                       # emit on change only
            dev.player = new
            if dev is self.devices.get(self.active_id):
                self.emit({"t": "player", **new})

    async def _device_ws_send(self, dev: DeviceConn, payload: dict) -> bool:
        """Send a JSON frame on the listener's open ws (serialized). Returns
        False if the listener is down so the caller can fall back."""
        ws = dev.ws
        if ws is None or ws.closed:
            return False
        async with dev.ws_send_lock:
            try:
                await ws.send_json(payload)
                return True
            except Exception:  # noqa: BLE001
                return False

    # -- shared card table <-> devices --
    async def _reconcile_cards(self, dev: DeviceConn) -> None:
        """Bring one device's card table in line with the shared one (see
        rfid_cards): read ``GET /rfid``, learn what was changed at the box,
        push what it lacks, drop what was unassigned meanwhile. Runs whenever
        the device (re)connects, so a box that was off catches up then."""
        learned: list[str] = []
        async with self.registry_lock:
            try:
                async with dev.lock:
                    client = await self._get_client(dev)
                    if not dev.mac:
                        info = await self.loop.run_in_executor(None, client.info)
                        self._learn_info(dev, info)
                    raw = await self.loop.run_in_executor(None, lambda: self._get_rfid(client))
            except Exception:  # noqa: BLE001 - offline: try again at the next connect
                return
            root = dev.cfg.remote_root
            table = {}
            for a in raw:
                entry = rfid_cards.canonical(a, root)
                if a.get("id") is not None and entry is not None:
                    table[str(a["id"])] = entry
            plan = self.registry.plan(dev.key, table, host=dev.cfg.host)
            learned = plan.learned
            failed = 0
            for tag, entry in plan.push:
                if not await self._device_card_op(dev, tag, entry):
                    failed += 1
            for tag in plan.delete:
                if not await self._device_card_op(dev, tag, None):
                    failed += 1
            await self.loop.run_in_executor(None, self.registry.save)
            dev.cards_checked = time.time()
            if plan.push or plan.delete or plan.learned or failed:
                print(f"Karten {dev.label}: {len(plan.learned)} übernommen, "
                      f"{len(plan.push) - failed} übertragen, {len(plan.delete)} "
                      f"entfernt, {failed} fehlgeschlagen", flush=True)
        await self.rescan()  # rebuilds card_map on units + reindexes assignments
        self.emit({"t": "state.changed"})
        if learned:
            # Taught at this box: hand it to every other box that is online now.
            for other in self.devices.values():
                if other is not dev and other.listening:
                    self.loop.create_task(self._reconcile_cards(other))

    async def _device_card_op(self, dev: DeviceConn, tag: str, entry: dict | None) -> bool:
        """POST (entry) or DELETE (None) one card on one device and note the
        outcome in the shared table. Caller holds registry_lock."""
        try:
            async with dev.lock:
                client = await self._get_client(dev)
                if entry is None:
                    await self.loop.run_in_executor(
                        None, lambda: self._delete_rfid(client, tag))
                else:
                    body = {"id": tag, **rfid_cards.to_device(entry, dev.cfg.remote_root)}
                    await self.loop.run_in_executor(
                        None, lambda: self._post_rfid(client, body))
        except Exception:  # noqa: BLE001 - stays pending, retried at the next connect
            return False
        if entry is None:
            self.registry.confirm_deleted(dev.key, tag)
        else:
            self.registry.confirm_pushed(dev.key, tag, entry)
        return True

    async def _send_card(self, tag: str) -> list[str]:
        """Send one card's current state (assigned or removed) to every device
        that is online; returns the labels of those that still lack it."""
        entry = self.registry.cards.get(tag)
        async with self.registry_lock:
            for dev in self.devices.values():
                # Only a device reconciled this run: its `seen` is current.
                if not dev.listening or dev.cards_checked is None:
                    continue
                have = self.registry.seen.get(dev.key, {}).get(tag)
                if have == entry:
                    continue
                await self._device_card_op(dev, tag, entry)
            await self.loop.run_in_executor(None, self.registry.save)
        return [d.label for d in self.devices.values()
                if tag in self.registry.pending(self._registry_key(d))]

    def _registry_key(self, dev: DeviceConn) -> str:
        return self.registry.key_for(dev.mac, dev.cfg.host)

    @staticmethod
    def _get_rfid(client: espuino.ESPuino) -> list[dict]:
        r = client.session.get(f"{client.base}/rfid", timeout=client.timeout)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _post_rfid(client: espuino.ESPuino, entry: dict) -> dict:
        r = client.session.post(f"{client.base}/rfid", json=entry,
                                timeout=client.timeout)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return entry

    @staticmethod
    def _delete_rfid(client: espuino.ESPuino, tag_id: str) -> None:
        r = client.session.delete(f"{client.base}/rfid", params={"id": tag_id},
                                  timeout=client.timeout)
        r.raise_for_status()

    @staticmethod
    def _play_audio(client: espuino.ESPuino, path: str, playmode: int) -> None:
        r = client.session.post(f"{client.base}/exploreraudio",
                                params={"path": path, "playmode": playmode},
                                timeout=client.timeout)
        r.raise_for_status()

    # -- player controls --
    async def h_unit_play(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        unit_id = (body.get("id") or "").strip()
        units = self._state_cache[0] if self._state_cache else []
        unit = next((u for u in units if u["id"] == unit_id), None)
        if unit is None:
            return web.json_response({"error": "Einheit nicht gefunden"}, status=400)
        # A sync/unit-sync holds the device (SD); a play would block behind the
        # lock for the whole upload, so refuse fast instead of hanging.
        if self.sync_running or self.unit_sync_ids:
            return web.json_response({"error": "Andere Aktion läuft"}, status=409)
        dev = self.active
        path = espuino.join_remote(dev.cfg.remote_root, *Path(unit_id).parts)
        playmode = studio_state.DEFAULT_PLAYMODE.get(unit["kind"], 3)
        try:
            async with dev.lock:
                client = await self._get_client(dev)
                await self.loop.run_in_executor(
                    None, lambda: self._play_audio(client, path, playmode))
        except (requests.ConnectionError, requests.Timeout):
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Wiedergabe fehlgeschlagen: {exc}"}, status=500)
        return web.json_response({"ok": True, "path": path, "playMode": playmode})

    async def h_player_stop(self, request: web.Request) -> web.Response:
        dev = self.active
        payload = {"controls": {"action": espuino.CMD_STOP}}
        if await self._device_ws_send(dev, payload):  # reuse the listener's ws
            return web.json_response({"ok": True, "via": "ws"})
        # Listener down: fall back to a short-lived ws via the blocking client.
        try:
            async with dev.lock:
                client = await self._get_client(dev)
                await self.loop.run_in_executor(
                    None, lambda: client.send_command(espuino.CMD_STOP))
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
        return web.json_response({"ok": True, "via": "fallback"})

    # -- intake progress poller (per-session download progress files) --
    async def _intake_poller(self) -> None:
        """Poll each session's $FUNKUINO_PROGRESS_FILE ~2/s and forward changes.
        The latest snapshot is also stored on the session (exposed in /api/state)
        so a reloaded tab can restore the bar."""
        seen: dict[str, int] = {}
        while not self._shutting_down:
            await asyncio.sleep(0.5)
            if self.agent is None:
                continue
            for sid, sess in list(self.agent.sessions.items()):
                pf = sess.progress_file
                if pf is None:
                    continue
                try:
                    mtime = pf.stat().st_mtime_ns
                except OSError:
                    continue
                if seen.get(sid) == mtime:
                    continue
                seen[sid] = mtime
                try:
                    snap = json.loads(pf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                sess.progress = snap
                self.emit({"t": "intake.progress", "sessionId": sid, "progress": snap})

    # -- library scan --
    def _scan(self, cards_known: bool, cards_table: dict[str, dict],
              pending: dict[str, set[str]]) -> tuple[list[dict], dict | None]:
        """Runs in an executor thread; the card table comes in as a copy
        (the loop thread changes it while a device reconciles)."""
        dev = self.active
        # The active device's own manifest. Before this run has read its MAC,
        # the one last written for its address; with a single device the
        # newest, as before there could be several.
        sync_state, meta = studio_state.pick_manifest(
            sync.STATE_DIR, mac=dev.mac, host=dev.cfg.host,
            newest_fallback=len(self.devices) == 1)
        print_state = PrintState.load(cards.STATE_FILE)
        # Card ids from the shared table, once there is one at all (a device
        # was read or a card assigned); before that "unknown", not "none".
        card_map = (studio_state.build_card_map(cards_table)
                    if cards_known else None)
        units = studio_state.scan(self.cfg, sync_state, print_state,
                                  card_map=card_map)
        if card_map is not None:
            for unit in units:
                # Boxes that do not have this unit's card(s) yet.
                unit["cardsPending"] = sorted(
                    label for label, tags in pending.items()
                    if any(t in tags for t in unit.get("cards", [])))
        return units, meta

    async def rescan(self) -> tuple[list[dict], dict | None]:
        known = self.registry.known
        table = {tag: dict(entry) for tag, entry in self.registry.cards.items()}
        pending = {d.label: self.registry.pending(self._registry_key(d))
                   for d in self.devices.values()}
        self._state_cache = await self.loop.run_in_executor(
            None, self._scan, known, table, pending)
        self._unit_ids = {u["id"] for u in self._state_cache[0]}
        self._reindex_assignments()
        return self._state_cache

    # -- RFID assignments (from the shared card table) --
    def _enrich_assignment(self, tag: str, entry: dict) -> dict:
        """One card as the UI sees it: the active device's path, the unit it
        plays (or None) and the boxes still lacking it."""
        on_device = rfid_cards.to_device(entry, self.cfg.remote_root)
        unit_id = entry.get("path")
        return {"id": tag, "fileOrUrl": on_device.get("fileOrUrl"),
                "playMode": on_device.get("playMode"), "modId": on_device.get("modId"),
                "unitId": unit_id if unit_id in self._unit_ids else None}

    def _reindex_assignments(self) -> None:
        self._assignment_by_id = {
            tag: self._enrich_assignment(tag, entry)
            for tag, entry in self.registry.cards.items()}

    def _devices_payload(self) -> list[dict]:
        """The switcher's list: every device, its state and how many cards
        it still has to get."""
        out = []
        for dev in self.devices.values():
            out.append({"id": dev.id, "name": dev.name, "label": dev.label,
                        "host": dev.cfg.host, "active": dev.id == self.active_id,
                        "reachable": dev.reachable, "attempted": dev.attempted,
                        "error": None if dev.reachable else dev.error,
                        "cardsPending": len(self.registry.pending(self._registry_key(dev)))
                        if self.registry.known else 0})
        return out

    def _cards_backlog(self) -> dict:
        """Counted from the cover files on disk, like the picker grid and
        ``./cards``, not from the cached library scan: a cover added since
        the last rescan (an agent download, a file copied in) showed in the
        grid while the counter still left it out."""
        _, per_page = cards.page_grid(cards.DEFAULT_CARD_CM, cards.DEFAULT_COLS)
        new = sum(1 for c in self._cover_items() if c["printed"] != "printed")
        return {"newCovers": new, "perPage": per_page}

    # -- HTTP handlers -------------------------------------------------------
    async def h_index(self, request: web.Request) -> web.StreamResponse:
        index = WEB_DIR / "index.html"
        if not index.is_file():
            return web.Response(text=PLACEHOLDER, content_type="text/plain")
        # Injecting `hidden` here (not just client-side in app.js) means a
        # disabled tab -- or, for --no-agent, the library toolbar's URL-download
        # field too -- is never painted at all: no first-paint flash while the
        # initial /api/state round-trip (device ping + library scan) is still
        # in flight.
        html = index.read_text(encoding="utf-8")
        if not self.cards_enabled:
            html = html.replace(
                '<button class="tab" data-tab="karten" role="tab">',
                '<button class="tab" data-tab="karten" role="tab" hidden>')
        if not self.agent_enabled:
            html = html.replace(
                '<button class="tab" data-tab="agent" role="tab">',
                '<button class="tab" data-tab="agent" role="tab" hidden>')
            html = html.replace(
                '<span class="lib-dl">',
                '<span class="lib-dl" hidden>')
        # Each asset URL names its file's version. The assets were served with
        # no Cache-Control, so browsers cached them heuristically (for hours,
        # going by Last-Modified) and a plain reload after an update kept the
        # old style.css and app.js: a fix to either did not reach the page. A
        # new URL bypasses any copy cached before, versioned or not.
        for asset in ("style.css", "app.js"):
            with contextlib.suppress(OSError):
                version = (WEB_DIR / asset).stat().st_mtime_ns
                html = html.replace(f'"/static/{asset}"',
                                    f'"/static/{asset}?v={version}"')
        # The version of the whole frontend this page is made of, compared by
        # app.js with /api/state's after a reconnect: a tab left open across a
        # restart onto changed files reloads itself (see checkFrontendVersion).
        html = html.replace(
            "</head>",
            f'  <meta name="funkuino-frontend" content="{_frontend_version()}">\n</head>', 1)
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def h_static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        # no-cache = always revalidate (a 304 via the ETag, cheap on localhost).
        return web.FileResponse(target, headers={"Cache-Control": "no-cache"})

    async def h_thumb(self, request: web.Request) -> web.StreamResponse:
        rel = request.query.get("rel", "")
        try:
            px = max(32, min(1024, int(request.query.get("px", "320"))))
        except ValueError:
            px = 320
        covers_dir = espuino.DEFAULT_COVERS_DIR.resolve()
        target = (covers_dir / rel).resolve()
        if covers_dir not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        key = f"{rel}|{target.stat().st_mtime_ns}|{px}"
        data = self._thumbs.get(key)
        if data is None:
            try:
                data = await self.loop.run_in_executor(
                    None, lambda: self._render_thumb(target, px))
            except Exception:  # noqa: BLE001
                raise web.HTTPNotFound()
            self._thumbs[key] = data
            while len(self._thumbs) > THUMB_CACHE_MAX:
                self._thumbs.popitem(last=False)  # evict least-recently-used
        else:
            self._thumbs.move_to_end(key)
        return web.Response(body=data, content_type="image/jpeg")

    @staticmethod
    def _render_thumb(path: Path, px: int) -> bytes:
        card = cards.prepare_card(path, px, trim=True)
        buf = io.BytesIO()
        card.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    async def h_state(self, request: web.Request) -> web.Response:
        if request.query.get("refresh") or self._state_cache is None:
            await self.rescan()
        units, manifest = self._state_cache
        dev = self.active
        device = (None if not dev.attempted
                  else {"reachable": dev.reachable, "info": dev.info,
                        "host": dev.cfg.host, "id": dev.id, "name": dev.name,
                        "label": dev.label,
                        "error": None if dev.reachable else dev.error})
        available = bool(self.agent and self.agent.available)
        agent = {"available": available,
                 "sessions": self.agent.summaries() if self.agent else [],
                 # Both halves of the setup, so the UI can name what is missing
                 # rather than pointing at a file the user never created.
                 "tokenSet": bool(self.token),
                 "cli": find_claude_cli()}
        if not available:
            agent["reason"] = ("Kein Zugangstoken hinterlegt."
                               if agent["cli"] else
                               "Claude Code CLI nicht gefunden und kein Token hinterlegt.")
        return web.json_response({
            "units": units,
            "syncManifest": manifest,
            "device": device,
            "jobs": {"sync": self.sync_running, "cards": self.cards_running,
                     "unitSync": sorted(self.unit_sync_ids)},
            "agent": agent,
            "cardsBacklog": await self.loop.run_in_executor(None, self._cards_backlog),
            "devices": self._devices_payload(),
            "rfid": {"listening": dev.listening},
            "player": dev.player,
            "tools": {"ffmpeg": self._ffmpeg_state()},
            "features": {"cards": self.cards_enabled, "agent": self.agent_enabled},
            "prefs": {"librarySort": _library_sort(), "printType": _print_type()},
            # What the printer select in the Kartendruck toolbar offers, from the
            # card_backends registry (shipped A4 sheet + any installed plugin).
            "cards": {"printTypes": await self.loop.run_in_executor(None, print_types)},
            "frontend": _frontend_version(),
        })

    def _ffmpeg_state(self) -> dict:
        """ffmpeg is the one dependency the user has to supply themselves on
        Linux and Windows (the macOS app carries its own), and everything audio
        fails without it — so the UI says so up front instead of letting a
        download die halfway.

        Cached, because the probe shells out; a *missing* one is re-checked on
        every call, which is the case where the answer can still change while
        Studio is running (the user goes and installs it).
        """
        if self._ffmpeg is None or not self._ffmpeg["path"]:
            self._ffmpeg = espuino.ffmpeg_status()
        return self._ffmpeg

    async def h_device_ping(self, request: web.Request) -> web.Response:
        info = await self.ping_device()
        if info is None:
            reason = self.active.error
            return web.json_response(
                {"error": "Gerät nicht erreichbar" + (f": {reason}" if reason else "")},
                status=503)
        return web.json_response({"reachable": True, "info": info})

    # -- sync job --
    async def h_sync(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        if self.sync_running:
            return web.json_response({"error": "Sync läuft bereits"}, status=409)
        if self.unit_sync_ids:
            return web.json_response(
                {"error": "Einzel-Sync läuft noch"}, status=409)
        dry_run = bool(body.get("dryRun"))
        delete = bool(body.get("delete"))
        # Files already on the box that Studio never uploaded (a card filled
        # at a computer): adopt them only on the user's explicit choice.
        # true = adopt, false = upload them anyway, absent = ask first.
        adopt = body.get("adopt")
        if adopt is not None and not isinstance(adopt, bool):
            return web.json_response({"error": "adopt muss true/false sein"}, status=400)
        # A real delete needs explicit confirmation; a delete *dry-run* only
        # previews would-be deletions and is harmless, so it doesn't.
        if delete and not dry_run and not body.get("confirm"):
            return web.json_response(
                {"error": "Löschen erfordert confirm:true"}, status=400)
        self.sync_running = True
        self.loop.create_task(self._run_sync(dry_run, delete, adopt))
        return web.json_response({"started": True, "dryRun": dry_run})

    async def _run_sync(self, dry_run: bool, delete: bool,
                        adopt: bool | None = None) -> None:
        start = time.monotonic()
        try:
            dev = self.active
            async with dev.lock:
                client = await self._get_client(dev)
                info = await self.loop.run_in_executor(None, client.info)
                self._learn_info(dev, info)
                dev.reachable = dev.attempted = True
                state = SyncState.load(sync.STATE_DIR, device_id=dev.key,
                                       host=dev.cfg.host)
                if w := espuino.state_warning("Der Abgleichstand",
                                              state.load_status):
                    self.emit({"t": "sync.log", "line": f"WARNUNG: {w}"})

                def log(line: str) -> None:
                    self.emit_threadsafe({"t": "sync.log", "line": line})

                # Asked only where a missing manifest entry really means
                # "not ours" (see SyncState.knows_own_uploads).
                offer = adopt is None and state.knows_own_uploads
                stats = await self.loop.run_in_executor(
                    None, lambda: sync.mirror(client, dev.cfg, state,
                                              delete=delete, dry_run=dry_run,
                                              adopt_existing=adopt is True,
                                              offer_adopt=offer, log=log))
            offer_msg = None
            if offer and stats.adoptable:
                offer_msg = {"t": "sync.offer", "count": stats.adoptable,
                             "bytes": stats.adoptable_bytes, "files": stats.files,
                             "delete": delete, "dryRun": dry_run,
                             "device": dev.label}
            if stats.adopt_offered:
                # Stopped after the listing: nothing was uploaded or deleted.
                self.emit({"t": "sync.log", "line":
                           f"{stats.adoptable} von {stats.files} Dateien liegen schon "
                           "auf der Kiste, Studio hat sie aber nie hochgeladen. "
                           "Noch nichts übertragen — bitte wählen."})
                self.emit(offer_msg)
                return
            # A quiet run produces no further mirror log lines and the closing
            # "Done in Xs" summary only lives in sync.main(), so emit our own
            # closing line here (before sync.done) so the log pane isn't left
            # looking frozen on an empty dry-run.
            elapsed = time.monotonic() - start
            self.emit({"t": "sync.log",
                       "line": _sync_summary(stats, elapsed, dry_run)})
            # Enrich with the aggregate aliases the frontend renders directly.
            payload = asdict(stats)
            payload["uploaded"] = stats.added + stats.updated + stats.resumed
            payload["bytes"] = stats.bytes_sent
            self.emit({"t": "sync.done", "stats": payload})
            if offer_msg:
                self.emit(offer_msg)   # after a dry run: the same question
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - start
            self.emit({"t": "sync.log",
                       "line": f"Abgebrochen nach {_de_secs(elapsed)} s: {exc}"})
            self.emit({"t": "sync.error", "msg": str(exc)})
        finally:
            self.sync_running = False
            await self.rescan()
            self.emit({"t": "state.changed"})

    # -- cards jobs (shell out to the dispatcher, bin/funkuino) --
    async def h_cards_print(self, request: web.Request) -> web.Response:
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        try:
            chosen = _usable_print_type(body.get("printType"))
        except ConsentMissing as exc:
            return web.json_response({"error": str(exc), "needsConsent": True}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        # --no-open: the job's own hand-off is _reveal_target below, so the
        # command must not open it a second time.
        argv = ["cards", "--no-open", "--backend", chosen]
        if body.get("dryRun"):
            argv.append("--dry-run")
        return await self._start_cards(argv)

    async def h_cards_undo(self, request: web.Request) -> web.Response:
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        return await self._start_cards(["cards", "--undo"])

    async def _start_cards(self, argv: list[str]) -> web.Response:
        if self.cards_running:
            return web.json_response({"error": "Karten-Job läuft bereits"},
                                     status=409)
        self.cards_running = True
        self.loop.create_task(self._run_cards(argv))
        return web.json_response({"started": True})

    def _reveal_target(self, target: str | None) -> None:
        """Hand a finished run to the user: an order's page in the browser, or a
        sheet in whatever opens PDFs.

        One implementation for both mediums and every platform —
        ``card_backends.reveal``, the same courtesy ``./cards --open`` gives. The
        platform was checked here before, so a file was handed over on macOS only
        and a Linux user was left with a sheet that was written and never opened.
        The failure is not swallowed: a hand-off that refuses lands in the job log.
        """
        if not target:
            return
        if problem := card_backends.reveal(target):
            self.emit_threadsafe({"t": "cards.log",
                                  "line": f"Konnte {target} nicht öffnen: {problem}"})

    async def _run_cards(self, argv: list[str]) -> None:
        try:
            # How many runs the manifest held before: the job revealed below is
            # only one that added a run. "Nothing new" and --undo add none, and
            # the newest run then is an older one — an order page from last
            # week, which must not pop up again.
            runs_before = len(PrintState.load(cards.STATE_FILE).runs)
            proc = await asyncio.create_subprocess_exec(
                *espuino.dispatcher_argv(), *argv,
                cwd=str(espuino.DATA_ROOT),
                # Resolved once here so the child does not re-read the config.
                # UTF-8 output, because that is what we decode below: left to
                # the locale, it is Latin-1 or cp1252 on some systems, and every
                # umlaut in a cover name arrived in the log as U+FFFD.
                env={**os.environ, "FUNKUINO_DATA_DIR": str(espuino.DATA_ROOT),
                     "PYTHONIOENCODING": "utf-8"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self.emit({"t": "cards.log", "line": line})
            code = await proc.wait()
            # Same courtesy as the picker: a finished job opens for the user —
            # an order's page in the browser, or the sheet itself. The run the
            # job added says which of the two it was.
            if code == 0 and "--dry-run" not in argv:
                runs = PrintState.load(cards.STATE_FILE).runs
                if len(runs) > runs_before:
                    self._reveal_target(runs[-1].get("url") or runs[-1].get("out"))
            self.emit({"t": "cards.done", "code": code})
        except Exception as exc:  # noqa: BLE001
            self.emit({"t": "cards.log", "line": f"FEHLER: {exc}"})
            self.emit({"t": "cards.done", "code": -1})
        finally:
            self.cards_running = False
            await self.rescan()
            self.emit({"t": "state.changed"})

    # -- integrated card picker (covers listing + render) --
    async def h_cards_covers(self, request: web.Request) -> web.Response:
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        data = await self.loop.run_in_executor(None, self._cards_covers)
        return web.json_response(data)

    def _cards_covers(self) -> dict:
        """All covers with printed status, newest st_birthtime first, plus
        perPage and the last print run."""
        state = PrintState.load(cards.STATE_FILE)
        _, per_page = cards.page_grid(cards.DEFAULT_CARD_CM, cards.DEFAULT_COLS)
        items = self._cover_items(state)
        items.sort(key=lambda d: d["birthtime"], reverse=True)
        # A run that marks nothing is a kept order page (forget_under), not a print.
        last = next((r for r in reversed(state.runs) if r.get("changes")), None)
        last_run = None if not last else {
            "at": last.get("at"), "out": last.get("out"),
            "count": len(last.get("changes", {}))}
        return {"covers": items, "perPage": per_page, "lastRun": last_run}

    @staticmethod
    def _cover_items(state: PrintState | None = None) -> list[dict]:
        """Every cover file under card-covers/ with its print status."""
        covers_dir = espuino.DEFAULT_COVERS_DIR
        if state is None:
            state = PrintState.load(cards.STATE_FILE)
        items = []
        for p in cards.collect_covers(covers_dir):
            st = p.stat()
            rel = _key(str(p.resolve().relative_to(covers_dir.resolve())))
            if state.is_printed(rel, st.st_size, st.st_mtime):
                printed = "printed"
            elif rel in state.printed:
                printed = "changed"
            else:
                printed = "new"
            items.append({"rel": rel, "printed": printed,
                          "birthtime": _creation_ts(st),
                          "size": st.st_size, "mtime": st.st_mtime})
        return items

    async def h_cards_render(self, request: web.Request) -> web.Response:
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        rels = [str(r) for r in body.get("rels", []) if r]
        if not rels:
            return web.json_response({"error": "Keine Cover ausgewählt"}, status=400)
        try:
            chosen = _usable_print_type(body.get("printType"))
        except ConsentMissing as exc:
            return web.json_response({"error": str(exc), "needsConsent": True}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if self.cards_running:
            return web.json_response({"error": "Karten-Job läuft bereits"},
                                     status=409)
        self.cards_running = True
        try:
            result = await self.loop.run_in_executor(
                None, lambda: self._print_covers(rels, chosen))
            self.emit({"t": "cards.done", "code": 0})
            return web.json_response({"ok": True, **result})
        except FileNotFoundError as exc:
            self.emit({"t": "cards.done", "code": -1})
            return web.json_response(
                {"ok": False, "error": f"Cover fehlt: {exc}"}, status=400)
        except Exception as exc:  # noqa: BLE001
            self.emit({"t": "cards.log", "line": f"FEHLER: {exc}"})
            self.emit({"t": "cards.done", "code": -1})
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        finally:
            self.cards_running = False
            await self.rescan()
            self.emit({"t": "state.changed"})

    def _print_covers(self, rels: list[str], backend_name: str) -> dict:
        """Print the chosen covers on the chosen medium and mark them printed.

        Goes through the same seam the command line does (card_backends +
        cards.record_run), so a sheet made here is the sheet ``./cards`` would
        make, a card order is the order ``./cards --backend …`` would place, and
        either one is reverted by ``./cards --undo``.
        """
        covers_dir = espuino.DEFAULT_COVERS_DIR
        covers = [covers_dir / r for r in rels]
        missing = [p for p in covers if not p.exists()]
        if missing:
            raise FileNotFoundError(str(missing[0]))

        def log(line: str) -> None:
            self.emit_threadsafe({"t": "cards.log", "line": line})

        backend = card_backends.load(backend_name)
        artifact = backend.emit(covers, _backend_defaults(backend), log=log)
        state = PrintState.load(cards.STATE_FILE)
        marked = cards.record_run(state, covers, artifact,
                                  time.strftime("%Y-%m-%d %H:%M:%S"))
        if marked < len(covers):
            log(f"{len(covers) - marked} Cover nicht gedruckt; sie bleiben neu.")
        self._reveal_target(artifact.open_target)
        return {"out": artifact.open_target, "cards": marked,
                "backend": backend_name}

    # -- targeted per-unit upload --
    #
    # STANDING RULE (do not relearn — see CLAUDE.md "Device traffic degrades
    # playback"): NEVER run device-touching tests here (uploads, walks, even
    # repeated /info beyond a single ping) without the user's explicit go. The
    # ESP32 shares Wi-Fi + SD between playback and our traffic, so a sync while a
    # child is listening makes the audio stutter, and the device cannot report
    # whether it is currently playing. Verify this path via the error/409/manifest
    # logic only; schedule real live uploads with the user.
    #
    # TODO (out of scope now, do not build): because the device can't distinguish
    # "idle" from "playing", a future improvement could confirm with the user
    # before starting a sync/unit-sync when playback might be running.
    async def h_unit_sync(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        unit_id = (body.get("id") or "").strip()
        if not unit_id:
            return web.json_response({"error": "id fehlt"}, status=400)
        if self.sync_running:
            return web.json_response({"error": "Sync läuft bereits"}, status=409)
        if unit_id in self.unit_sync_ids:
            return web.json_response(
                {"error": "Einheit wird bereits synchronisiert"}, status=409)
        self.unit_sync_ids.add(unit_id)
        self.loop.create_task(self._run_unit_sync(unit_id))
        return web.json_response({"started": True, "id": unit_id})

    async def _run_unit_sync(self, unit_id: str) -> None:
        self.emit({"t": "unit.sync", "id": unit_id, "status": "running"})
        try:
            local_dir = Path(self.cfg.local_dir)
            target = local_dir / unit_id
            if target.is_dir():
                files = studio_state._mp3s(target)
            elif target.is_file() and target.suffix.lower() == ".mp3":
                files = [target]
            else:
                files = []
            files = [f for f in files if not espuino.is_ignored(f.name)]
            if not files:
                raise RuntimeError("Keine Dateien für diese Einheit gefunden")

            dev = self.active
            async with dev.lock:
                client = await self._get_client(dev)
                info = await self.loop.run_in_executor(None, client.info)
                self._learn_info(dev, info)
                dev.reachable = dev.attempted = True
                state = SyncState.load(sync.STATE_DIR, device_id=dev.key,
                                       host=dev.cfg.host)

                def work() -> None:
                    # Mirror-identical semantics for just this unit's files.
                    # First classify so the progress ring's 100% == the bytes we
                    # actually upload (unchanged files are not counted).
                    to_upload = []
                    for f in files:
                        rel = f.relative_to(local_dir)
                        remote = espuino.join_remote(dev.cfg.remote_root, *rel.parts)
                        size = f.stat().st_size
                        mtime = int(f.stat().st_mtime)
                        if state.is_unchanged(remote, size, mtime):
                            self._unit_progress(unit_id, line=f"übersprungen: {rel.as_posix()}")
                        else:
                            to_upload.append((f, rel, remote, size, mtime))

                    total = sum(t[3] for t in to_upload)
                    prog = {"done": 0, "t": 0.0, "pct": -1}

                    def report(sent: int, line: str | None = None) -> None:
                        pct = 100.0 if total == 0 else min(100.0, (prog["done"] + sent) / total * 100.0)
                        now = time.monotonic()
                        whole = int(pct)
                        # Throttle: a line always emits; otherwise at most one event
                        # per whole-percent step or per ~500 ms (callback is hot).
                        if line is None and whole == prog["pct"] and now - prog["t"] < 0.5:
                            return
                        prog["t"], prog["pct"] = now, whole
                        self._unit_progress(unit_id, line=line, pct=round(pct, 1))

                    for f, rel, remote, size, mtime in to_upload:
                        state.mark_pending(remote)  # persists intent before upload
                        report(0, f"hochladen: {rel.as_posix()}")
                        client.upload(f, remote, progress=lambda sent, tot: report(sent))
                        prog["done"] += size
                        state.mark_synced(remote, size, mtime)  # persists success

                await self.loop.run_in_executor(None, work)
            self.emit({"t": "unit.sync", "id": unit_id, "status": "done"})
        except Exception as exc:  # noqa: BLE001
            self.emit({"t": "unit.sync", "id": unit_id, "status": "error",
                       "error": _device_error_msg(exc)})
        finally:
            self.unit_sync_ids.discard(unit_id)
            await self.rescan()
            self.emit({"t": "state.changed"})

    def _unit_progress(self, unit_id: str, line: str | None = None,
                       pct: float | None = None) -> None:
        msg = {"t": "unit.sync", "id": unit_id, "status": "progress"}
        if pct is not None:
            msg["pct"] = pct
        if line is not None:
            msg["line"] = line
        self.emit_threadsafe(msg)

    # -- RFID assignment endpoints --
    #
    # An assignment goes into the shared card table first and then to every
    # box that is online; a box that is off gets it at its next connect. So
    # assigning works with any one box reachable (the card was just read on
    # one), and the response names the boxes still waiting for it.
    async def h_rfid_assignments(self, request: web.Request) -> web.Response:
        return web.json_response({
            "listening": self.active.listening,
            "assignments": list(self._assignment_by_id.values()),
            "fetchedAt": self.active.cards_checked,
        })

    async def h_rfid_assign(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        tag_id = str(body.get("tagId", "")).strip()
        unit_id = (body.get("unitId") or "").strip()
        if not RFID_ID_RE.fullmatch(tag_id):
            return web.json_response(
                {"error": "Ungültige Karten-ID (genau 12 Ziffern)"}, status=400)
        units = self._state_cache[0] if self._state_cache else []
        unit = next((u for u in units if u["id"] == unit_id), None)
        if unit is None:
            return web.json_response({"error": "Einheit nicht gefunden"}, status=400)

        play_mode = body.get("playMode")
        if play_mode is None:
            play_mode = studio_state.DEFAULT_PLAYMODE.get(unit["kind"], 3)
        try:
            play_mode = int(play_mode)
        except (TypeError, ValueError):
            return web.json_response({"error": "Ungültiger Abspielmodus"}, status=400)
        if play_mode <= 0 or play_mode >= rfid_cards.MOD_ID_MIN:
            return web.json_response({"error": "Ungültiger Abspielmodus"}, status=400)

        # fileOrUrl mirrors the explorer path: leading slash + the unit id (NFC).
        file_or_url = espuino.join_remote(self.cfg.remote_root, *Path(unit_id).parts)
        if "^" in file_or_url or "#" in file_or_url:
            return web.json_response(
                {"error": "Pfad enthält unzulässige Zeichen (^ oder #)"}, status=400)

        entry = {"path": unit["id"], "playMode": play_mode}
        async with self.registry_lock:
            self.registry.assign(tag_id, entry)
            try:
                await self.loop.run_in_executor(None, self.registry.save)
            except OSError as exc:
                return web.json_response(
                    {"error": f"Zuordnung nicht gespeichert: {exc}"}, status=500)
        pending = await self._send_card(tag_id)
        await self.rescan()
        self.emit({"t": "state.changed"})
        self.emit({"t": "rfid.assigned", "tagId": tag_id, "unitId": unit_id})
        return web.json_response({"ok": True, "pending": pending,
                                  "assignment": {"id": tag_id, "fileOrUrl": file_or_url,
                                                 "playMode": play_mode}})

    async def h_rfid_unassign(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        tag_id = str(body.get("tagId", "")).strip()
        if not RFID_ID_RE.fullmatch(tag_id):
            return web.json_response(
                {"error": "Ungültige Karten-ID (genau 12 Ziffern)"}, status=400)
        existing = self._assignment_by_id.get(tag_id)
        if existing is None:
            return web.json_response({"error": "Karte ist nicht zugeordnet"}, status=404)
        unit_id = existing.get("unitId")
        async with self.registry_lock:
            self.registry.unassign(tag_id)
            try:
                await self.loop.run_in_executor(None, self.registry.save)
            except OSError as exc:
                return web.json_response(
                    {"error": f"Löschen nicht gespeichert: {exc}"}, status=500)
        pending = await self._send_card(tag_id)
        await self.rescan()
        self.emit({"t": "state.changed"})
        self.emit({"t": "rfid.unassigned", "tagId": tag_id, "unitId": unit_id})
        return web.json_response({"ok": True, "tagId": tag_id, "unitId": unit_id,
                                  "pending": pending})

    # -- agent endpoints --
    def _agent_ready(self) -> web.Response | None:
        if not self.agent_enabled:
            raise web.HTTPNotFound()
        if not (self.agent and self.agent.available):
            return web.json_response(
                {"error": "SDK_TOKEN fehlt in .env"}, status=503)
        return None

    async def h_agent_create(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        body = await _json_body(request)
        kind = body.get("kind", "chat")
        model = body.get("model", "sonnet")
        if kind == "url" and not body.get("url"):
            return web.json_response({"error": "url fehlt"}, status=400)
        if kind == "chat" and not body.get("text"):
            return web.json_response({"error": "text fehlt"}, status=400)
        sid = await self.agent.create(kind=kind, model=model,
                                      url=body.get("url"), text=body.get("text"))
        return web.json_response({"id": sid})

    async def h_agent_events(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        sid = request.match_info["sid"]
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        events = self.agent.replay(sid, since)
        if events is None:
            raise web.HTTPNotFound()
        session = self.agent.get(sid)
        return web.json_response({"events": events, "status": session.status})

    async def h_agent_message(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        session = self.agent.get(request.match_info["sid"])
        if session is None:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text fehlt"}, status=400)
        session.post_turn(text, text)
        return web.json_response({"ok": True})

    async def h_agent_answer(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        session = self.agent.get(request.match_info["sid"])
        if session is None:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        ok = session.resolve(body.get("requestId", ""), "question",
                             body.get("answers", {}))
        if not ok:
            return web.json_response(
                {"ok": False, "error": "Anfrage nicht mehr offen"}, status=409)
        return web.json_response({"ok": True})

    async def h_agent_permission(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        session = self.agent.get(request.match_info["sid"])
        if session is None:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        ok = session.resolve(body.get("requestId", ""), "permission",
                             {"allow": bool(body.get("allow")),
                              "always": bool(body.get("always"))})
        if not ok:
            return web.json_response(
                {"ok": False, "error": "Anfrage nicht mehr offen"}, status=409)
        return web.json_response({"ok": True})

    async def h_agent_token(self, request: web.Request) -> web.Response:
        """Store (or clear) the agent's access token from the UI.

        It lands in the data folder's .env, the same place the server reads at
        start, so a token entered in the app is there for the terminal tools and
        survives a restart. Applying it to the live SessionManager as well means
        no restart is needed — existing sessions keep the token they started
        with, new ones get this one.
        """
        if not self.agent_enabled:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        token = str(body.get("token") or "").strip()
        try:
            espuino.write_env_value(espuino.APP_CONFIG_DIR / "credentials.env",
                            "SDK_TOKEN", token or None)
        except OSError as exc:
            return web.json_response({"error": f"Konnte .env nicht schreiben: {exc}"},
                                     status=500)
        self.token = token or None
        if self.agent is not None:
            self.agent.token = self.token
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
        else:
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self.emit({"t": "state.changed"})
        return web.json_response({"ok": True, "available": bool(self.token)})

    async def h_cards_notice_open(self, request: web.Request) -> web.Response:
        """Open a print medium's own page (the link in its third-party notice).

        Through the server rather than a plain link: the macOS app's web view
        opens no new windows, and Studio runs on the user's machine, where
        ``card_backends.reveal`` hands the page to their browser. Only the
        address the backend itself names is opened, never one from the request.
        """
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        entry = next((t for t in print_types() if t["id"] == body.get("printType")), None)
        url = ((entry or {}).get("notice") or {}).get("url")
        if not url:
            return web.json_response({"error": "kein Link"}, status=404)
        problem = await self.loop.run_in_executor(None, card_backends.reveal, url)
        if problem:
            return web.json_response({"error": problem}, status=500)
        return web.json_response({"ok": True})

    async def h_prefs(self, request: web.Request) -> web.Response:
        """Store a UI preference (library sort, print medium) in config.json.

        Not in the browser's localStorage: that is keyed by origin, port
        included, and the macOS app starts Studio on a fresh port every launch,
        so the choice would be forgotten each time.
        """
        body = await _json_body(request)
        changes: dict = {}
        if "librarySort" in body:
            mode = body.get("librarySort")
            if mode not in LIBRARY_SORTS:
                return web.json_response({"error": "unbekannte Sortierung"}, status=400)
            changes["library_sort"] = mode
        if "printType" in body:
            try:
                chosen = _checked_print_type(body.get("printType"))
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            if chosen and not _consented(chosen):
                # The dialog's "Okay, Dienst nutzen" sends consent with the
                # choice; without it a third-party medium cannot be chosen.
                if body.get("consent") is not True:
                    return web.json_response(
                        {"error": "Dieser Druckdienst muss erst bestätigt werden.",
                         "needsConsent": True}, status=403)
                changes["print_consent"] = sorted({*_consents(), chosen})
            changes["print_type"] = chosen
        if not changes:
            return web.json_response({"error": "nichts zu speichern"}, status=400)
        try:
            espuino.update_app_config(changes)
        except (OSError, ValueError) as exc:
            return web.json_response(
                {"error": f"Konnte config.json nicht schreiben: {exc}"}, status=500)
        return web.json_response({"ok": True})

    # -- settings dialog --
    def _effective_setting(self, key: str):
        if key in DEVICE_SETTINGS:
            value = getattr(self.cfg, key)
            return bool(value) if key == "http_password" else (value or "")
        return {"cards_enabled": self.cards_enabled, "agent_enabled": self.agent_enabled,
                "port": self.port, "open_browser": self.open_browser}[key]

    def _settings_payload(self) -> dict:
        settings = {}
        for key in SETTINGS:
            saved = _saved_setting(key)
            settings[key] = {
                "value": self._effective_setting(key),
                "saved": bool(saved) if key == "http_password" else saved,
                "override": self.overrides.get(key),
            }
        config = espuino.read_app_config()
        return {"settings": settings,
                # The saved list (what the dialog edits); a --host pins one
                # address for this run instead, which the dialog says.
                "devices": {"saved": espuino.saved_devices(config),
                            "active": espuino.active_device(config)["id"],
                            "override": self.overrides.get("host"),
                            "overrideHost": self.cfg.host if "host" in self.overrides else None},
                "dataDir": str(espuino.DATA_ROOT),
                "configFile": str(espuino.APP_CONFIG_FILE)}

    async def h_settings(self, request: web.Request) -> web.Response:
        return web.json_response(self._settings_payload())

    async def h_settings_save(self, request: web.Request) -> web.Response:
        """Save settings to config.json (the HTTP password to credentials.env)
        and apply what can be applied while running.

        Device settings and the two features take effect at once, unless a
        start parameter or environment variable pins them for this run — then
        only the saved value changes, for the next start without it. Port and
        browser only matter at start.
        """
        body = await _json_body(request)
        try:
            values = {key: _validate_setting(key, body[key])
                      for key in SETTINGS if key in body}
            devices = (_validate_devices(body["devices"], espuino.saved_devices())
                       if "devices" in body else None)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        device_change = any(key in DEVICE_SETTINGS and key not in self.overrides
                            and values[key] != _saved_setting(key) for key in values)
        if devices is not None and "host" not in self.overrides:
            device_change = device_change or devices != espuino.saved_devices()
        if device_change and (self.sync_running or self.unit_sync_ids):
            return web.json_response(
                {"error": "Während eines Syncs lässt sich das Gerät nicht ändern."},
                status=409)
        password = values.pop("http_password", None)
        changes = {key: (None if value == SETTING_DEFAULTS[key] else value)
                   for key, value in values.items()}
        if devices is not None:
            changes.update(_devices_config(devices, espuino.read_app_config()
                                           .get("active_device")))
        try:
            espuino.update_app_config(changes)
            if password is not None:
                espuino.write_env_value(espuino.credentials_file(),
                                        "ESPUINO_HTTP_PASSWORD", password or None)
        except (OSError, ValueError) as exc:
            return web.json_response(
                {"error": f"Konnte die Einstellungen nicht speichern: {exc}"}, status=500)

        if "cards_enabled" in values and "cards_enabled" not in self.overrides:
            self.cards_enabled = values["cards_enabled"]
        if "agent_enabled" in values and "agent_enabled" not in self.overrides:
            self.agent_enabled = values["agent_enabled"]
        if device_change:
            # Env vars (incl. a --host, see main) still win inside from_env.
            await self._apply_device_config(espuino.Config.from_env())
        self.emit({"t": "state.changed"})
        return web.json_response(self._settings_payload())

    async def _apply_device_config(self, base: espuino.Config) -> None:
        """Switch to another set of devices (or the same ones with another base
        folder or login) without a restart: drop the connections and everything
        learnt from them, and reconnect. The card table stays: it belongs to
        the library, not to a device."""
        old = self.devices
        for dev in old.values():
            if dev.task is not None:
                dev.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dev.task
        for dev in old.values():
            async with dev.lock:   # let a running device op finish first
                dev.client = None
        self._build_devices(base)
        self.emit({"t": "player", **self.active.player})
        await self.rescan()  # the base folder maps cards and sync status to units
        self._start_devices()

    def _start_devices(self) -> None:
        for dev in self.devices.values():
            dev.task = self.loop.create_task(self._rfid_listener(dev))
            self.loop.create_task(self.ping_device(dev))

    async def h_device_select(self, request: web.Request) -> web.Response:
        """The switcher: which box the Bibliothek, Sync and the player act on.

        Saved in config.json (``active_device``), so the commands the agent
        runs (`funkuino sync`) talk to the same box as Studio shows. Not while
        a sync runs: it would finish on one box and be reported on the other.
        """
        body = await _json_body(request)
        dev_id = str(body.get("id") or "")
        if dev_id not in self.devices:
            return web.json_response({"error": "Gerät nicht gefunden"}, status=404)
        if dev_id == self.active_id:
            return web.json_response({"ok": True, "id": dev_id})
        if self.sync_running or self.unit_sync_ids:
            return web.json_response(
                {"error": "Während eines Syncs lässt sich das Gerät nicht wechseln."},
                status=409)
        if "host" not in self.overrides:
            try:
                espuino.update_app_config(
                    _devices_config(espuino.saved_devices(), dev_id))
            except (OSError, ValueError) as exc:
                return web.json_response(
                    {"error": f"Konnte die Auswahl nicht speichern: {exc}"}, status=500)
        self.active_id = dev_id
        self.emit({"t": "player", **self.active.player})
        await self.rescan()       # sync status is per device
        self.emit({"t": "state.changed"})
        return web.json_response({"ok": True, "id": dev_id})

    async def h_agent_interrupt(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        session = self.agent.get(request.match_info["sid"])
        if session is None:
            raise web.HTTPNotFound()
        await session.interrupt()
        return web.json_response({"ok": True})

    async def h_agent_delete(self, request: web.Request) -> web.Response:
        if not self.agent_enabled:
            raise web.HTTPNotFound()
        if self.agent is None or not await self.agent.remove(request.match_info["sid"]):
            raise web.HTTPNotFound()
        self.emit({"t": "state.changed"})  # other tabs drop it from their list
        return web.json_response({"ok": True})

    # -- websocket --
    async def h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        self.ws_queues.add(queue)

        async def writer():
            while True:
                msg = await queue.get()
                await ws.send_json(msg)

        wtask = self.loop.create_task(writer())
        self.ws_clients.add(ws)
        try:
            async for _ in ws:  # client sends nothing; just detect close
                pass
        finally:
            self.ws_clients.discard(ws)
            wtask.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wtask
            self.ws_queues.discard(queue)
        return ws

    # -- lifecycle --
    async def on_startup(self, app: web.Application) -> None:
        self.loop = asyncio.get_running_loop()
        self.agent = studio_agent.SessionManager(
            self.token, self.emit_agent, self.loop,
            sync_active=lambda: self.sync_running)
        self.agent.load()
        self._ws_session = aiohttp.ClientSession()
        for stale in espuino.APP_CONFIG_DIR.glob("progress-*.json"):
            with contextlib.suppress(OSError):
                stale.unlink()  # leftovers from a previous run
        await self.rescan()
        self._start_devices()     # a listener + one ping per device, in the background
        self._intake_task = self.loop.create_task(self._intake_poller())

    async def on_shutdown(self, app: web.Application) -> None:
        # An open tab's /ws handler only returns once the socket closes, and
        # aiohttp waits for running handlers (up to shutdown_timeout) before it
        # lets the process exit — so close them ourselves. The tab reconnects
        # with backoff to whatever Studio starts next.
        await asyncio.gather(
            *(ws.close(code=WSCloseCode.GOING_AWAY, message=b"Studio stopped")
              for ws in list(self.ws_clients)),
            return_exceptions=True)

    async def on_cleanup(self, app: web.Application) -> None:
        self._shutting_down = True
        for task in (*(d.task for d in self.devices.values()), self._intake_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._ws_session is not None:
            await self._ws_session.close()
        if self.agent is not None:
            await self.agent.shutdown()

    # -- CSRF / DNS-rebinding guard --
    def _allowed_hosts(self) -> set[str]:
        """Host[:port] values a state-changing request may carry. Binding is
        127.0.0.1 only, so anything else means a cross-site or rebinding attempt."""
        return {"127.0.0.1", "localhost", "[::1]",
                f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                f"[::1]:{self.port}"}

    def _csrf_response(self, request: web.Request) -> web.Response | None:
        """403 for a non-GET (or /ws) request from a foreign Host/Origin; None to
        allow. Missing Origin is allowed (curl / same-origin navigations)."""
        if request.host and request.host.lower() not in self._allowed_hosts():
            return web.json_response({"error": "Ungültiger Host"}, status=403)
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() not in self._allowed_hosts():
                return web.json_response({"error": "Ungültige Origin"}, status=403)
        return None

    def build_app(self) -> web.Application:
        @web.middleware
        async def csrf_mw(request: web.Request, handler):
            # State-changing methods and the /ws upgrade must be same-origin.
            if request.method not in ("GET", "HEAD", "OPTIONS") or request.path == "/ws":
                blocked = self._csrf_response(request)
                if blocked is not None:
                    return blocked
            return await handler(request)

        app = web.Application(middlewares=[csrf_mw])
        app.router.add_get("/", self.h_index)
        app.router.add_get("/static/{name:.*}", self.h_static)
        app.router.add_get("/thumb", self.h_thumb)
        app.router.add_get("/ws", self.h_ws)
        app.router.add_get("/api/state", self.h_state)
        app.router.add_post("/api/device/ping", self.h_device_ping)
        app.router.add_post("/api/device/select", self.h_device_select)
        app.router.add_post("/api/sync", self.h_sync)
        app.router.add_post("/api/cards/print", self.h_cards_print)
        app.router.add_post("/api/cards/undo", self.h_cards_undo)
        app.router.add_get("/api/cards/covers", self.h_cards_covers)
        app.router.add_post("/api/cards/render", self.h_cards_render)
        app.router.add_post("/api/cards/notice-open", self.h_cards_notice_open)
        app.router.add_post("/api/units/sync", self.h_unit_sync)
        app.router.add_post("/api/units/play", self.h_unit_play)
        app.router.add_post("/api/player/stop", self.h_player_stop)
        app.router.add_get("/api/rfid/assignments", self.h_rfid_assignments)
        app.router.add_post("/api/rfid/assign", self.h_rfid_assign)
        app.router.add_post("/api/rfid/unassign", self.h_rfid_unassign)
        app.router.add_post("/api/prefs", self.h_prefs)
        app.router.add_get("/api/settings", self.h_settings)
        app.router.add_post("/api/settings", self.h_settings_save)
        app.router.add_post("/api/agent/token", self.h_agent_token)
        app.router.add_post("/api/agent/sessions", self.h_agent_create)
        app.router.add_get("/api/agent/sessions/{sid}/events", self.h_agent_events)
        app.router.add_post("/api/agent/sessions/{sid}/message", self.h_agent_message)
        app.router.add_post("/api/agent/sessions/{sid}/answer", self.h_agent_answer)
        app.router.add_post("/api/agent/sessions/{sid}/permission", self.h_agent_permission)
        app.router.add_post("/api/agent/sessions/{sid}/interrupt", self.h_agent_interrupt)
        app.router.add_delete("/api/agent/sessions/{sid}", self.h_agent_delete)
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)
        app.on_cleanup.append(self.on_cleanup)
        return app


LIBRARY_SORTS = ("date", "alpha")

# What the "Drucker" select in the Kartendruck toolbar offers: the media Studio
# can hand a print job to. The list is the card_backends registry — the shipped
# A4 sheet plus whatever is installed in <data folder>/extensions/card_backends/
# — so a medium is added by dropping a module there, never by editing this file,
# and the command line sees exactly the same set (`cards --backend …`).
def print_types() -> list[dict]:
    """The registry, plus whether each medium may be used: one that sends the
    covers to a third party (it has a ``notice``) only after the user accepted
    that notice in Studio."""
    accepted = _consents()
    return [{**t, "consented": not t.get("notice") or t["id"] in accepted}
            for t in card_backends.registry()]


class ConsentMissing(ValueError):
    """A third-party medium was asked for before its notice was accepted."""


def _consents() -> list[str]:
    """The media whose third-party notice was accepted (config.json)."""
    value = espuino.read_app_config().get("print_consent")
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _consented(name: str) -> bool:
    return next((t["consented"] for t in print_types() if t["id"] == name), False)



# What the settings dialog edits. Each is saved in config.json (the HTTP
# password in credentials.env, with the other credentials) and can still be
# pinned for one run by a start parameter or an environment variable — which
# then wins, and the dialog says so. The device ones are read by every command
# through espuino.Config, so `funkuino sync` talks to the device set up here.
SETTINGS = {
    "remote_root": {"env": "ESPUINO_REMOTE_ROOT"},
    "http_user": {"env": "ESPUINO_HTTP_USER"},
    "http_password": {"env": "ESPUINO_HTTP_PASSWORD"},
    "cards_enabled": {},
    "agent_enabled": {},
    "port": {},
    "open_browser": {},
}
DEVICE_SETTINGS = ("remote_root", "http_user", "http_password")
SETTING_DEFAULTS = {
    "remote_root": espuino.DEFAULT_REMOTE_ROOT,
    "http_user": "", "http_password": "",
    "cards_enabled": True, "agent_enabled": True,
    "port": DEFAULT_PORT, "open_browser": True,
}


def _saved_setting(key: str):
    """The saved value of a setting, or its default. A value of the wrong type
    (a hand-edited config.json) counts as unset rather than failing."""
    default = SETTING_DEFAULTS[key]
    if key == "http_password":
        return espuino.parse_env(espuino.credentials_file()).get(
            "ESPUINO_HTTP_PASSWORD", "")
    value = espuino.read_app_config().get(key)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
        return value if ok else default
    return value if isinstance(value, str) and value else default


def _validate_setting(key: str, value):
    """Normalise one value from the dialog; ValueError carries the message."""
    default = SETTING_DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key}: ja oder nein erwartet")
        return value
    if key == "port":
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError("Der Port muss eine Zahl sein.") from None
        if not 1024 <= port <= 65535:
            raise ValueError("Der Port muss zwischen 1024 und 65535 liegen.")
        return port
    if not isinstance(value, str):
        raise ValueError(f"{key}: Text erwartet")
    if key == "http_password":
        return value  # verbatim: spaces may be part of it; "" removes it
    value = value.strip()
    if key == "remote_root":
        value = "/" + value.strip("/") if value.strip("/") else ""
    return value or default


def _clean_host(value: str) -> str:
    """A device address from the dialog; ValueError carries the message."""
    # Forgive a pasted address: "http://espuino.local/" -> "espuino.local".
    value = re.sub(r"^https?://", "", value.strip(), flags=re.I).rstrip("/")
    if not value:
        raise ValueError("Jedes Gerät braucht eine Adresse.")
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", value):
        raise ValueError("Die Geräteadresse ist ein Name wie espuino.local "
                         "oder eine IP-Adresse.")
    return value


def _validate_devices(value, saved: list[dict]) -> list[dict]:
    """The device list from the dialog -> ``[{id, name, host}]``. An entry
    without a known id is a new device and gets one; names and addresses must
    be unique, since the switcher and `funkuino sync --device` go by them."""
    if not isinstance(value, list) or not value:
        raise ValueError("Mindestens ein Gerät muss eingerichtet sein.")
    known = {d["id"] for d in saved}
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Ungültiger Geräteeintrag.")
        name = str(entry.get("name") or "").strip()
        host = _clean_host(str(entry.get("host") or ""))
        dev_id = entry.get("id")
        if not (isinstance(dev_id, str) and dev_id in known
                and all(d["id"] != dev_id for d in out)):
            dev_id = uuid.uuid4().hex[:8]
        if any(d["host"].lower() == host.lower() for d in out):
            raise ValueError(f"Die Adresse {host} steht zweimal in der Liste.")
        if name and any(d["name"].casefold() == name.casefold() for d in out):
            raise ValueError(f"Der Name {name} steht zweimal in der Liste.")
        out.append({"id": dev_id, "name": name, "host": host})
    if len(out) > 1 and not all(d["name"] for d in out):
        raise ValueError("Bei mehreren Geräten braucht jedes einen Namen.")
    return out


def _devices_config(devices: list[dict], active: str | None) -> dict:
    """config.json keys for a device list and the active device. ``host``
    mirrors the active device's address for anything that reads only it (an
    older Studio, the macOS app's setup screen), and a single unnamed device
    is written the old way, as just ``host``."""
    chosen = next((d for d in devices if d["id"] == active), devices[0])
    host = None if chosen["host"] == espuino.DEFAULT_HOST else chosen["host"]
    if len(devices) == 1 and not devices[0]["name"]:
        return {"devices": None, "active_device": None, "host": host}
    return {"devices": devices, "active_device": chosen["id"], "host": host}


def _frontend_version() -> str:
    """Names the frontend files as they are on disk now. Read per request, not
    once at start: the files are served from disk, so a page loaded now gets
    whatever is there now, and this has to say the same."""
    stamps = []
    for name in FRONTEND_FILES:
        try:
            stamps.append(str((WEB_DIR / name).stat().st_mtime_ns))
        except OSError:
            stamps.append("-")
    return "-".join(stamps)


def _library_sort() -> str:
    mode = espuino.read_app_config().get("library_sort")
    return mode if mode in LIBRARY_SORTS else "date"


def _print_type() -> str:
    """The stored print medium, or the default one (the A4 sheet).

    A stored name that is no longer offered (the plugin providing it is gone,
    or its third-party notice was never accepted here) falls back rather than
    breaking the UI — the select then shows the fallback,
    and the pref is left for the day the backend is back. The fallback is the
    DEFAULT backend, never "whichever comes first": alphabetically that is an
    installed card service, which would make a media order the default without
    anyone choosing it.
    """
    types = [t for t in print_types() if t["consented"]]
    ids = {t["id"] for t in types}
    value = espuino.read_app_config().get("print_type")
    if isinstance(value, str) and value in ids:
        return value
    if card_backends.DEFAULT in ids:
        return card_backends.DEFAULT
    return types[0]["id"] if types else ""


def _checked_print_type(value) -> str | None:
    """Validate a print type from a request body; None when none was given.

    An unknown name is refused (ValueError) instead of falling back to the A4
    path: the media cost different money, so printing paper where a card order
    was meant — or the reverse — is worse than an error.
    """
    if value in (None, ""):
        return None
    if value not in {t["id"] for t in print_types()}:
        raise ValueError(f"Unbekannter Drucker: {value!r}")
    return str(value)


def _usable_print_type(value) -> str:
    """The medium a print request runs on: the one it names, else the stored
    one — refused (ConsentMissing) while its third-party notice is unaccepted,
    which the UI should have asked for before the request was ever sent."""
    chosen = _checked_print_type(value) or _print_type()
    if not _consented(chosen):
        raise ConsentMissing("Dieser Druckdienst muss erst bestätigt werden.")
    return chosen


def _backend_defaults(backend):
    """A backend's flags at their defaults, as argparse would give them for no
    arguments — so Studio hands a backend the same ``args`` a bare ``./cards``
    would, without a second copy of its option list."""
    parser = argparse.ArgumentParser(add_help=False)
    backend.add_arguments(parser)
    return parser.parse_args([])


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _exit_with_parent() -> None:
    """Exit once the process that started us is gone.

    A GUI shell (Funkuino.app) starts this server as a child. Quitting the shell
    normally lets it terminate us, but a crash, a force-quit or a plain SIGTERM
    does not — and an orphaned server keeps the port and the device websocket
    open, which then blocks the next start. Watching for reparenting (our ppid
    changes to 1 / launchd) catches every one of those cases. Opt-in via
    FUNKUINO_EXIT_WITH_PARENT so a terminal-started server is unaffected.
    """
    original = os.getppid()

    def watch() -> None:
        while os.getppid() == original:
            time.sleep(1.0)
        os._exit(0)  # hard exit: the parent is gone, nobody is left to serve

    threading.Thread(target=watch, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("FUNKUINO_EXIT_WITH_PARENT"):
        _exit_with_parent()

    # Every option overrides a setting from Studio's settings dialog for this
    # run only; without it the saved setting applies (see SETTINGS).
    parser = argparse.ArgumentParser(
        description="Funkuino Studio web app. Options override the saved "
                    "settings (Studio's settings dialog) for this run.")
    parser.add_argument("--port", type=int,
                        help=f"Port to bind (saved setting, default {DEFAULT_PORT})")
    parser.add_argument("--host", help="ESPuino device host/IP")
    parser.add_argument("--browser", action=argparse.BooleanOptionalAction,
                        help="Open a browser tab on start (saved setting, default yes)")
    parser.add_argument("--cards", action=argparse.BooleanOptionalAction,
                        help="Show the card-printing tab and column (saved setting, default yes)")
    parser.add_argument("--agent", action=argparse.BooleanOptionalAction,
                        help="Show the agent tab (saved setting, default yes)")
    args = parser.parse_args(argv)

    overrides = {key: spec["env"] for key, spec in SETTINGS.items()
                 if spec.get("env") and os.environ.get(spec["env"])}
    if os.environ.get("ESPUINO_HOST"):
        overrides["host"] = "ESPUINO_HOST"
    if args.host:
        overrides["host"] = "--host"
        # Through the environment, so the commands the agent runs
        # (`funkuino sync`) talk to the same device as Studio does.
        os.environ["ESPUINO_HOST"] = args.host
    flags = {"port": ("port", "--port"), "open_browser": ("browser", "--browser"),
             "cards_enabled": ("cards", "--cards"), "agent_enabled": ("agent", "--agent")}
    start = {}
    for key, (attr, flag) in flags.items():
        value = getattr(args, attr)
        if value is None:
            start[key] = _saved_setting(key)
        else:
            start[key] = value
            overrides[key] = flag if value is not False else flag.replace("--", "--no-", 1)

    token = espuino.parse_env(espuino.credentials_file()).get("SDK_TOKEN")
    if token:
        # So the Agent SDK subprocess inherits it even if not passed explicitly.
        os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", token)

    cfg = espuino.Config.from_env()
    port = start["port"]
    studio = Studio(cfg, token, port=port, cards_enabled=start["cards_enabled"],
                    agent_enabled=start["agent_enabled"],
                    open_browser=start["open_browser"], overrides=overrides)
    app = studio.build_app()

    url = f"http://127.0.0.1:{port}/"
    devices = ", ".join(f"{d.label} ({d.cfg.host})" if d.name else d.cfg.host
                        for d in studio.devices.values())
    print(f"Funkuino Studio at {url}"
          f"  (device: {devices}, agent: {'on' if token else 'no token'})")
    if not espuino.find_ffmpeg():
        # Studio itself runs fine without it; downloading and merging do not.
        print(f"WARNING: ffmpeg not found — downloads and merging will fail.\n"
              f"         Install it with:  {espuino.ffmpeg_hint()}", file=sys.stderr)
    if start["open_browser"]:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    # Jobs run as tasks, not inside requests, so nothing legitimate needs
    # aiohttp's default 60 s grace; a slow device call must not hold the exit.
    web.run_app(app, host="127.0.0.1", port=port, print=None,
                handle_signals=True, shutdown_timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
