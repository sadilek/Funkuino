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
  after a reload.

Bind is 127.0.0.1 only. The OAuth token is read from ``.env`` and handed to the
Agent SDK via its options env -- it is never logged or returned.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import requests
from aiohttp import web

import cards
import espuino
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


def write_env_value(path: Path, key: str, value: str | None) -> None:
    """Set (or drop) one KEY=VALUE in a .env, leaving the other lines alone.

    The token is a credential, so the file is created 0600 — it would otherwise
    inherit a world-readable default.
    """
    lines = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    kept = [ln for ln in lines
            if not ln.strip().startswith(f"{key}=") and not ln.strip().startswith(f"{key} =")]
    if value:
        kept.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).strip() + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def parse_env(path: Path) -> dict[str, str]:
    """Read simple ``KEY=VALUE`` lines from a .env file (no dependency)."""
    env: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class Studio:
    def __init__(self, cfg: espuino.Config, token: str | None,
                 port: int = DEFAULT_PORT, cards_enabled: bool = True,
                 agent_enabled: bool = True):
        self.cfg = cfg
        self.token = token
        self.port = port
        self.cards_enabled = cards_enabled  # --no-cards: hide print tab/column, 404 its API
        self.agent_enabled = agent_enabled  # --no-agent: hide agent tab, 404 its API
        self.loop: asyncio.AbstractEventLoop | None = None
        self.agent: studio_agent.SessionManager | None = None

        # Device: one lazily-built client, one lock serialising every device op.
        self._client: espuino.ESPuino | None = None
        self.device_lock = asyncio.Lock()
        self.device_info: dict | None = None       # last successful /info
        self.device_reachable = False               # last attempt result
        self.device_attempted = False               # was /info ever tried?

        # Jobs (one sync, one cards render/CLI at a time; unit syncs by id).
        self.sync_running = False
        self.cards_running = False
        self.unit_sync_ids: set[str] = set()  # unit ids currently uploading

        # WS hub.
        self.ws_queues: set[asyncio.Queue] = set()
        self._seq = 0

        # Caches.
        self._state_cache: tuple[list[dict], dict | None] | None = None
        self._ffmpeg: dict | None = None      # see _ffmpeg_state()
        self._thumbs: "OrderedDict[str, bytes]" = OrderedDict()
        self._unit_ids: set[str] = set()  # ids from the last scan (assignment mapping)

        # RFID: a passive device-ws listener + the assignments cache.
        self.rfid_listening = False
        self._assignments: list[dict] | None = None   # last GET /rfid (None = never)
        self._assignments_at: float | None = None      # when we last fetched them
        self._assignment_by_id: dict[str, dict] = {}    # id -> enriched assignment
        self._rfid_task: asyncio.Task | None = None
        self._ws_session: aiohttp.ClientSession | None = None
        self._shutting_down = False
        self._intake_task: asyncio.Task | None = None  # polls session progress files

        # Now-playing (from the listener's trackinfo broadcasts) + a handle on the
        # listener's ws so player controls reuse it instead of a 2nd connection.
        self._player = {"playing": False, "unitId": None, "name": "", "pausePlay": False}
        self._player_last_unit: str | None = None
        self._device_ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_send_lock = asyncio.Lock()

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
    async def _get_client(self) -> espuino.ESPuino:
        """Build the client once (DNS resolves in the constructor -> executor)."""
        if self._client is None:
            self._client = await self.loop.run_in_executor(
                None, lambda: espuino.ESPuino(self.cfg))
        return self._client

    async def ping_device(self) -> dict | None:
        async with self.device_lock:
            self.device_attempted = True
            try:
                client = await self._get_client()
                info = await asyncio.wait_for(
                    self.loop.run_in_executor(None, client.info), PING_TIMEOUT)
            except Exception:  # noqa: BLE001 - offline is a normal state
                self.device_reachable = False
                return None
            self.device_info = info
            self.device_reachable = True
            return info

    # -- RFID device-ws listener (passive, read-only; no device_lock needed) --
    async def _rfid_listener(self) -> None:
        """Keep a websocket to the device open, translating ``rfidId`` broadcasts
        into browser events. Reconnects with backoff while the device is offline.
        Listening is passive (tiny frames) so it does not use the device_lock."""
        backoff = RFID_BACKOFF_MIN
        while not self._shutting_down:
            try:
                addr = await self.loop.run_in_executor(
                    None, lambda: espuino.ESPuino._resolve(self.cfg.host))
                url = f"ws://{addr}:{self.cfg.http_port}/ws"
                # receive_timeout (~12 s = 4x the 3 s ping) is what flips the
                # device to "offline" within ~10-15 s when it is powered off:
                # no frames arrive, the receive times out, and we fall through.
                async with self._ws_session.ws_connect(
                        url, autoping=True, heartbeat=None,
                        receive_timeout=RFID_PING_SECS * 4) as ws:
                    self.rfid_listening = True
                    self._device_ws = ws                         # reuse for controls
                    backoff = RFID_BACKOFF_MIN
                    self._set_reachable(True)                    # online (on transition)
                    self.loop.create_task(self._refresh_info())         # one /info read
                    self.loop.create_task(self._refresh_assignments())  # one /rfid read
                    with contextlib.suppress(Exception):
                        await ws.send_json({"trackinfo": {}})    # warm now-playing
                    ping = self.loop.create_task(self._rfid_ping(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_device_msg(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ping  # retrieve it so it can't orphan an exception
            except Exception:  # noqa: BLE001 - offline/reset is normal, just retry
                pass
            self.rfid_listening = False
            self._device_ws = None
            self._reset_player()                                # clear stale now-playing
            self._set_reachable(False)                          # offline (on transition)
            if self._shutting_down:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RFID_BACKOFF_MAX)

    def _set_reachable(self, reachable: bool) -> None:
        """Update device reachability from the ws listener and push a
        state.changed only on an actual transition (so repeated offline
        reconnect attempts don't spam the dashboard)."""
        if self.device_reachable == reachable and self.device_attempted:
            return
        self.device_reachable = reachable
        self.device_attempted = True
        self.emit({"t": "state.changed"})

    async def _refresh_info(self) -> None:
        """Refresh the cached /info once after a (re)connect (executor + lock)."""
        try:
            async with self.device_lock:
                client = await self._get_client()
                info = await self.loop.run_in_executor(None, client.info)
        except Exception:  # noqa: BLE001
            return
        self.device_info = info

    async def _rfid_ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(RFID_PING_SECS)
            # A send on a dropping socket may raise; swallow it (the receive loop
            # detects the drop) so this task never orphans an exception.
            with contextlib.suppress(Exception):
                await ws.send_json({"ping": {"ping": "ping"}})

    def _handle_device_msg(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        if "rfidId" in obj:                        # ignore pong/status/volume/…
            self._emit_rfid_card(str(obj["rfidId"]))
        elif "trackinfo" in obj:
            self._handle_trackinfo(obj.get("trackinfo") or {})

    def _emit_rfid_card(self, tag_id: str) -> None:
        a = self._assignment_by_id.get(tag_id)
        self.emit({"t": "rfid.card", "id": tag_id, "known": a is not None,
                   "assignment": None if a is None else {
                       "fileOrUrl": a["fileOrUrl"], "playMode": a["playMode"],
                       "unitId": a["unitId"]}})

    # -- now-playing tracking (from trackinfo broadcasts) --
    def _resolve_playing_unit(self, name: str) -> str | None:
        """Best-effort map a trackinfo ``name`` to a unit id. The firmware sends
        either the file path (at track start) or the ID3 title (after tags load),
        optionally prefixed with "(N/M): ". Only the path form resolves: a file
        unit matches its id exactly; a folder unit (album/folge) matches when the
        path is inside it."""
        stripped = _TRACK_PREFIX_RE.sub("", name or "").strip()
        if "/" not in stripped:
            return None                             # an ID3 title, not a path
        cand = studio_state.assignment_key(stripped, self.cfg.remote_root)  # strip leading slash + remote_root + NFC
        if cand in self._unit_ids:
            return cand
        best = None
        for uid in self._unit_ids:
            if cand.startswith(uid + "/") and (best is None or len(uid) > len(best)):
                best = uid
        return best

    def _handle_trackinfo(self, ti: dict) -> None:
        play_mode = ti.get("playMode")
        num = ti.get("numberOfTracks", 0)
        if play_mode in (None, PLAYER_NO_PLAYLIST) or not num:
            self._reset_player()
            return
        name = str(ti.get("name") or "")
        pause = bool(ti.get("pausePlay"))
        uid = self._resolve_playing_unit(name)
        if uid is not None:
            self._player_last_unit = uid
        else:
            uid = self._player_last_unit            # sticky across ID3/path swaps
        self._update_player({"playing": not pause, "unitId": uid,
                             "name": name, "pausePlay": pause})

    def _reset_player(self) -> None:
        self._player_last_unit = None
        self._update_player({"playing": False, "unitId": None,
                             "name": "", "pausePlay": False})

    def _update_player(self, new: dict) -> None:
        if new != self._player:                     # emit on change only
            self._player = new
            self.emit({"t": "player", **new})

    async def _device_ws_send(self, payload: dict) -> bool:
        """Send a JSON frame on the listener's open ws (serialized). Returns
        False if the listener is down so the caller can fall back."""
        ws = self._device_ws
        if ws is None or ws.closed:
            return False
        async with self._ws_send_lock:
            try:
                await ws.send_json(payload)
                return True
            except Exception:  # noqa: BLE001
                return False

    async def _refresh_assignments(self) -> None:
        """Read all assignments (passive GET /rfid) and refresh the cache. Keeps
        the last known list on failure (e.g. a transient drop)."""
        try:
            async with self.device_lock:
                client = await self._get_client()
                data = await self.loop.run_in_executor(None, lambda: self._get_rfid(client))
        except Exception:  # noqa: BLE001
            return
        self._assignments = data
        self._assignments_at = time.time()
        await self.rescan()  # rebuilds card_map on units + reindexes assignments
        self.emit({"t": "state.changed"})

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
        path = espuino.join_remote(self.cfg.remote_root, *Path(unit_id).parts)
        playmode = studio_state.DEFAULT_PLAYMODE.get(unit["kind"], 3)
        try:
            async with self.device_lock:
                client = await self._get_client()
                await self.loop.run_in_executor(
                    None, lambda: self._play_audio(client, path, playmode))
        except (requests.ConnectionError, requests.Timeout):
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Wiedergabe fehlgeschlagen: {exc}"}, status=500)
        return web.json_response({"ok": True, "path": path, "playMode": playmode})

    async def h_player_stop(self, request: web.Request) -> web.Response:
        payload = {"controls": {"action": espuino.CMD_STOP}}
        if await self._device_ws_send(payload):     # reuse the listener's ws
            return web.json_response({"ok": True, "via": "ws"})
        # Listener down: fall back to a short-lived ws via the blocking client.
        try:
            async with self.device_lock:
                client = await self._get_client()
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
    def _scan(self) -> tuple[list[dict], dict | None]:
        sync_state, meta = studio_state.pick_manifest(sync.STATE_DIR)
        print_state = PrintState.load(cards.STATE_FILE)
        # Only attach card ids once we have ever read /rfid; otherwise omit them.
        card_map = (studio_state.build_card_map(self._assignments, self.cfg.remote_root)
                    if self._assignments is not None else None)
        units = studio_state.scan(self.cfg, sync_state, print_state,
                                  card_map=card_map)
        return units, meta

    async def rescan(self) -> tuple[list[dict], dict | None]:
        self._state_cache = await self.loop.run_in_executor(None, self._scan)
        self._unit_ids = {u["id"] for u in self._state_cache[0]}
        self._reindex_assignments()
        return self._state_cache

    # -- RFID assignments cache --
    def _enrich_assignment(self, a: dict) -> dict:
        """Add the resolved unitId (or None) to a raw /rfid entry."""
        key = studio_state.assignment_key(a.get("fileOrUrl", ""), self.cfg.remote_root)
        return {"id": str(a.get("id")), "fileOrUrl": a.get("fileOrUrl"),
                "playMode": a.get("playMode"),
                "unitId": key if key in self._unit_ids else None}

    def _reindex_assignments(self) -> None:
        if self._assignments is None:
            self._assignment_by_id = {}
            return
        self._assignment_by_id = {
            e["id"]: e for e in (self._enrich_assignment(a) for a in self._assignments)
            if e["id"] is not None}

    def _cards_backlog(self, units: list[dict]) -> dict:
        _, per_page = cards.page_grid(cards.DEFAULT_CARD_CM, cards.DEFAULT_COLS)
        new = sum(1 for u in units if u["print"] in ("new", "changed"))
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
        return web.Response(text=html, content_type="text/html")

    async def h_static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

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
        device = (None if not self.device_attempted
                  else {"reachable": self.device_reachable, "info": self.device_info,
                        "host": self.cfg.host})
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
            "cardsBacklog": self._cards_backlog(units),
            "rfid": {"listening": self.rfid_listening},
            "player": self._player,
            "tools": {"ffmpeg": self._ffmpeg_state()},
            "features": {"cards": self.cards_enabled, "agent": self.agent_enabled},
            "prefs": {"librarySort": _library_sort()},
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
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
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
        # A real delete needs explicit confirmation; a delete *dry-run* only
        # previews would-be deletions and is harmless, so it doesn't.
        if delete and not dry_run and not body.get("confirm"):
            return web.json_response(
                {"error": "Löschen erfordert confirm:true"}, status=400)
        self.sync_running = True
        self.loop.create_task(self._run_sync(dry_run, delete))
        return web.json_response({"started": True, "dryRun": dry_run})

    async def _run_sync(self, dry_run: bool, delete: bool) -> None:
        start = time.monotonic()
        try:
            async with self.device_lock:
                client = await self._get_client()
                info = await self.loop.run_in_executor(None, client.info)
                self.device_info, self.device_reachable = info, True
                self.device_attempted = True
                mac = (info.get("wifi") or {}).get("macAddress") or ""
                device_id = mac or self.cfg.host
                state = SyncState.load(sync.STATE_DIR, device_id=device_id,
                                       host=self.cfg.host)
                if w := espuino.state_warning("Der Abgleichstand",
                                              state.load_status):
                    self.emit({"t": "sync.log", "line": f"WARNUNG: {w}"})

                def log(line: str) -> None:
                    self.emit_threadsafe({"t": "sync.log", "line": line})

                stats = await self.loop.run_in_executor(
                    None, lambda: sync.mirror(client, self.cfg, state,
                                              delete=delete, dry_run=dry_run,
                                              log=log))
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
        return await self._start_cards(["cards", "--dry-run"] if body.get("dryRun")
                                       else ["cards"])

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

    def _reveal_sheet(self, pdf: Path) -> None:
        """Show a finished print sheet to the user (macOS Preview).

        Not suppressed silently: if the hand-off fails the user is left waiting
        for a window that never appears, so say so in the job log.
        """
        if sys.platform != "darwin" or not pdf.exists():
            return
        try:
            result = subprocess.run(["/usr/bin/open", str(pdf)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                self.emit({"t": "cards.log",
                           "line": f"Konnte {pdf.name} nicht öffnen: "
                                   f"{result.stderr.strip() or result.returncode}"})
        except Exception as exc:  # noqa: BLE001
            self.emit({"t": "cards.log", "line": f"Konnte {pdf.name} nicht öffnen: {exc}"})

    async def _run_cards(self, argv: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *espuino.dispatcher_argv(), *argv,
                cwd=str(espuino.DATA_ROOT),
                # Resolved once here so the child does not re-read the config.
                env={**os.environ, "FUNKUINO_DATA_DIR": str(espuino.DATA_ROOT)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            sheet: Path | None = None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                # cards.py's own summary is the only handle on the file it wrote.
                match = re.match(r"Wrote (.+\.pdf)\s", line)
                if match:
                    sheet = Path(match.group(1))
                self.emit({"t": "cards.log", "line": line})
            code = await proc.wait()
            # Same courtesy as the picker: a finished sheet opens for printing.
            if code == 0 and sheet is not None and "--dry-run" not in argv:
                self._reveal_sheet(sheet)
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
        covers_dir = espuino.DEFAULT_COVERS_DIR
        state = PrintState.load(cards.STATE_FILE)
        _, per_page = cards.page_grid(cards.DEFAULT_CARD_CM, cards.DEFAULT_COLS)
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
        items.sort(key=lambda d: d["birthtime"], reverse=True)
        last = state.runs[-1] if state.runs else None
        last_run = None if not last else {
            "at": last.get("at"), "out": last.get("out"),
            "count": len(last.get("changes", {}))}
        return {"covers": items, "perPage": per_page, "lastRun": last_run}

    async def h_cards_render(self, request: web.Request) -> web.Response:
        if not self.cards_enabled:
            raise web.HTTPNotFound()
        body = await _json_body(request)
        rels = [str(r) for r in body.get("rels", []) if r]
        if not rels:
            return web.json_response({"error": "Keine Cover ausgewählt"}, status=400)
        if self.cards_running:
            return web.json_response({"error": "Karten-Job läuft bereits"},
                                     status=409)
        self.cards_running = True
        try:
            result = await self.loop.run_in_executor(
                None, lambda: self._render_sheet(rels))
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

    def _render_sheet(self, rels: list[str]) -> dict:
        """Render chosen covers to a fresh PDF and mark them printed -- identical
        behaviour to ./cards (same defaults) so ./cards --undo reverts it."""
        covers_dir = espuino.DEFAULT_COVERS_DIR
        covers = [covers_dir / r for r in rels]
        missing = [p for p in covers if not p.exists()]
        if missing:
            raise FileNotFoundError(str(missing[0]))

        def log(line: str) -> None:
            self.emit_threadsafe({"t": "cards.log", "line": line})

        pages = cards.render_pages(covers, cards.DEFAULT_CARD_CM, cards.DEFAULT_COLS,
                                   marks=True, trim=True, log=log)
        out = (espuino.PRINT_SHEETS_DIR
               / f"cards-{time.strftime('%Y%m%d-%H%M%S')}.pdf")
        cards.save_pdf(pages, out)
        state = PrintState.load(cards.STATE_FILE)
        items = [(r, (covers_dir / r).stat().st_size, (covers_dir / r).stat().st_mtime)
                 for r in rels]
        state.mark_run(items, str(out), time.strftime("%Y-%m-%d %H:%M:%S"))
        state.save()
        self._reveal_sheet(out)
        return {"out": str(out), "pages": len(pages), "cards": len(rels)}

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

            async with self.device_lock:
                client = await self._get_client()
                info = await self.loop.run_in_executor(None, client.info)
                self.device_info, self.device_reachable = info, True
                self.device_attempted = True
                mac = (info.get("wifi") or {}).get("macAddress") or ""
                device_id = mac or self.cfg.host
                state = SyncState.load(sync.STATE_DIR, device_id=device_id,
                                       host=self.cfg.host)

                def work() -> None:
                    # Mirror-identical semantics for just this unit's files.
                    # First classify so the progress ring's 100% == the bytes we
                    # actually upload (unchanged files are not counted).
                    to_upload = []
                    for f in files:
                        rel = f.relative_to(local_dir)
                        remote = espuino.join_remote(self.cfg.remote_root, *rel.parts)
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
    async def h_rfid_assignments(self, request: web.Request) -> web.Response:
        return web.json_response({
            "listening": self.rfid_listening,
            "assignments": list(self._assignment_by_id.values()),
            "fetchedAt": self._assignments_at,
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
        if play_mode <= 0:
            return web.json_response({"error": "Ungültiger Abspielmodus"}, status=400)

        # fileOrUrl mirrors the explorer path: leading slash + the unit id (NFC).
        file_or_url = espuino.join_remote(self.cfg.remote_root, *Path(unit_id).parts)
        if "^" in file_or_url or "#" in file_or_url:
            return web.json_response(
                {"error": "Pfad enthält unzulässige Zeichen (^ oder #)"}, status=400)

        entry = {"id": tag_id, "fileOrUrl": file_or_url, "playMode": play_mode}
        try:
            async with self.device_lock:
                client = await self._get_client()
                await self.loop.run_in_executor(None, lambda: self._post_rfid(client, entry))
        except (requests.ConnectionError, requests.Timeout):
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Zuordnung fehlgeschlagen: {exc}"}, status=500)

        await self._refresh_assignments()  # re-read /rfid, rescan, state.changed
        self.emit({"t": "rfid.assigned", "tagId": tag_id, "unitId": unit_id})
        return web.json_response({"ok": True, "assignment": entry})

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
        try:
            async with self.device_lock:
                client = await self._get_client()
                await self.loop.run_in_executor(None, lambda: self._delete_rfid(client, tag_id))
        except (requests.ConnectionError, requests.Timeout):
            return web.json_response({"error": "Gerät nicht erreichbar"}, status=503)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Löschen fehlgeschlagen: {exc}"}, status=500)

        await self._refresh_assignments()  # re-read /rfid, rescan, state.changed
        self.emit({"t": "rfid.unassigned", "tagId": tag_id, "unitId": unit_id})
        return web.json_response({"ok": True, "tagId": tag_id, "unitId": unit_id})

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
            write_env_value(espuino.APP_CONFIG_DIR / "credentials.env",
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

    async def h_prefs(self, request: web.Request) -> web.Response:
        """Store a UI preference (for now: the library sort) in config.json.

        Not in the browser's localStorage: that is keyed by origin, port
        included, and the macOS app starts Studio on a fresh port every launch,
        so the choice would be forgotten each time.
        """
        body = await _json_body(request)
        mode = body.get("librarySort")
        if mode not in LIBRARY_SORTS:
            return web.json_response({"error": "unbekannte Sortierung"}, status=400)
        try:
            _set_app_config("library_sort", mode)
        except (OSError, ValueError) as exc:
            return web.json_response(
                {"error": f"Konnte config.json nicht schreiben: {exc}"}, status=500)
        return web.json_response({"ok": True})

    async def h_agent_interrupt(self, request: web.Request) -> web.Response:
        guard = self._agent_ready()
        if guard is not None:
            return guard
        session = self.agent.get(request.match_info["sid"])
        if session is None:
            raise web.HTTPNotFound()
        await session.interrupt()
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
        try:
            async for _ in ws:  # client sends nothing; just detect close
                pass
        finally:
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
        self._ws_session = aiohttp.ClientSession()
        for stale in espuino.APP_CONFIG_DIR.glob("progress-*.json"):
            with contextlib.suppress(OSError):
                stale.unlink()  # leftovers from a previous run
        await self.rescan()
        self.loop.create_task(self.ping_device())     # populate device chip in bg
        self._rfid_task = self.loop.create_task(self._rfid_listener())
        self._intake_task = self.loop.create_task(self._intake_poller())

    async def on_cleanup(self, app: web.Application) -> None:
        self._shutting_down = True
        for task in (self._rfid_task, self._intake_task):
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
        app.router.add_post("/api/sync", self.h_sync)
        app.router.add_post("/api/cards/print", self.h_cards_print)
        app.router.add_post("/api/cards/undo", self.h_cards_undo)
        app.router.add_get("/api/cards/covers", self.h_cards_covers)
        app.router.add_post("/api/cards/render", self.h_cards_render)
        app.router.add_post("/api/units/sync", self.h_unit_sync)
        app.router.add_post("/api/units/play", self.h_unit_play)
        app.router.add_post("/api/player/stop", self.h_player_stop)
        app.router.add_get("/api/rfid/assignments", self.h_rfid_assignments)
        app.router.add_post("/api/rfid/assign", self.h_rfid_assign)
        app.router.add_post("/api/rfid/unassign", self.h_rfid_unassign)
        app.router.add_post("/api/prefs", self.h_prefs)
        app.router.add_post("/api/agent/token", self.h_agent_token)
        app.router.add_post("/api/agent/sessions", self.h_agent_create)
        app.router.add_get("/api/agent/sessions/{sid}/events", self.h_agent_events)
        app.router.add_post("/api/agent/sessions/{sid}/message", self.h_agent_message)
        app.router.add_post("/api/agent/sessions/{sid}/answer", self.h_agent_answer)
        app.router.add_post("/api/agent/sessions/{sid}/permission", self.h_agent_permission)
        app.router.add_post("/api/agent/sessions/{sid}/interrupt", self.h_agent_interrupt)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app


LIBRARY_SORTS = ("date", "alpha")


def _library_sort() -> str:
    try:
        config = json.loads(espuino.APP_CONFIG_FILE.read_text(encoding="utf-8"))
        mode = config.get("library_sort") if isinstance(config, dict) else None
    except (OSError, ValueError):
        mode = None
    return mode if mode in LIBRARY_SORTS else "date"


def _set_app_config(key: str, value) -> None:
    """Set one key in config.json, keeping the others (notably data_dir).

    A file that exists but does not parse is left alone (ValueError) rather
    than replaced: rewriting it would silently drop the data folder setting.
    """
    path = espuino.APP_CONFIG_FILE
    config = {}
    if path.is_file():
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config.json is not a JSON object")
    config[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    espuino.write_text_atomically(path, json.dumps(config, indent=2) + "\n")


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

    parser = argparse.ArgumentParser(description="Funkuino Studio web app.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to bind (default {DEFAULT_PORT})")
    parser.add_argument("--host", help="ESPuino device host/IP override")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a browser tab on start")
    parser.add_argument("--no-cards", action="store_true",
                        help="Hide the card-printing tab and column (feature disabled)")
    parser.add_argument("--no-agent", action="store_true",
                        help="Hide the agent tab (feature disabled)")
    args = parser.parse_args(argv)

    token = parse_env(espuino.credentials_file()).get("SDK_TOKEN")
    if token:
        # So the Agent SDK subprocess inherits it even if not passed explicitly.
        os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", token)

    cfg = espuino.Config.from_env(host=args.host)
    studio = Studio(cfg, token, port=args.port, cards_enabled=not args.no_cards,
                    agent_enabled=not args.no_agent)
    app = studio.build_app()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Funkuino Studio at {url}"
          f"  (device: {cfg.host}, agent: {'on' if token else 'no token'})")
    if not espuino.find_ffmpeg():
        # Studio itself runs fine without it; downloading and merging do not.
        print(f"WARNING: ffmpeg not found — downloads and merging will fail.\n"
              f"         Install it with:  {espuino.ffmpeg_hint()}", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    web.run_app(app, host="127.0.0.1", port=args.port, print=None,
                handle_signals=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
