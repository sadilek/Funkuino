#!/usr/bin/env python3
"""A simulated ESPuino on localhost — for trying Studio without real hardware.

    .venv/bin/python dev/fake_espuino.py --port 8181 --name Alexander
    .venv/bin/python dev/fake_espuino.py --port 8182 --name Katharina --state /tmp/k.json

Point a Studio device at ``127.0.0.1:8181`` (an address may carry a port).
It speaks the part of the device API Studio uses: ``/info``, the RFID table
(``GET/POST/DELETE /rfid``), ``/explorer`` (listing, upload, mkdir, delete),
``/exploreraudio`` and the ``/ws`` websocket (pings, ``trackinfo``). The
card table persists in ``--state`` across restarts, so stopping and starting
a box plays "offline, then back".

Placing a card is simulated with ``POST /_fake/card?id=<12 digits>``: the box
broadcasts ``{"rfidId": …}`` as the reader would, and, if the card is
assigned, starts "playing" it (a ``trackinfo`` frame).

Never needed for the real device; checkout-only like dev/shot.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from pathlib import Path

from aiohttp import WSMsgType, web


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="ESPuino", help="hostname reported by /info")
    ap.add_argument("--state", type=Path, help="JSON file keeping the card table and files")
    args = ap.parse_args()

    mac = ":".join(hashlib.sha1(args.name.encode()).hexdigest()[i:i + 2].upper()
                   for i in range(0, 12, 2))
    state = {"rfid": {}, "files": []}
    if args.state and args.state.is_file():
        state.update(json.loads(args.state.read_text(encoding="utf-8")))
    sockets: set[web.WebSocketResponse] = set()

    def save() -> None:
        if args.state:
            args.state.write_text(json.dumps(state, indent=1, ensure_ascii=False),
                                  encoding="utf-8")

    def log(line: str) -> None:
        print(f"[{args.name}] {line}", flush=True)

    async def broadcast(obj: dict) -> None:
        for ws in list(sockets):
            try:
                await ws.send_json(obj)
            except Exception:  # noqa: BLE001
                sockets.discard(ws)

    async def info(request):
        return web.json_response({
            "software": {"version": "Software-revision: 20261120-DEV (fake)", "branch": "dev"},
            "hardware": {"model": "Simulator"},
            "memory": {"freeHeap": 123456},
            "wifi": {"ip": "127.0.0.1", "macAddress": mac, "rssi": -52},
            "battery": {"chargeLevel": 87, "currVoltage": 3.31},
            "hostname": args.name,
        })

    async def rfid_get(request):
        return web.json_response([{"id": k, **v} for k, v in sorted(state["rfid"].items())])

    async def rfid_post(request):
        body = await request.json()
        tag = str(body.get("id") or "")
        if not tag:
            return web.Response(status=500, text="Missing tag id")
        if body.get("modId") is not None:
            entry = {"modId": int(body["modId"])}
        else:
            entry = {"fileOrUrl": unicodedata.normalize("NFC", body.get("fileOrUrl") or "0"),
                     "playMode": int(body.get("playMode") or 0),
                     "lastPlayPos": 0, "trackLastPlayed": 0}
        state["rfid"][tag] = entry
        save()
        log(f"card {tag} -> {entry.get('fileOrUrl', entry.get('modId'))}")
        return web.json_response({"id": tag, **entry})

    async def rfid_delete(request):
        tag = request.query.get("id", "")
        if state["rfid"].pop(tag, None) is not None:
            save()
            log(f"card {tag} removed")
        return web.Response(text="OK")

    async def explorer_get(request):
        path = "/" + request.query.get("path", "/").strip("/")
        prefix = "" if path == "/" else path + "/"
        names: dict[str, bool] = {}
        for f in state["files"]:
            if f.startswith(prefix or "/"):
                rest = f[len(prefix):] if prefix else f[1:]
                head, _, tail = rest.partition("/")
                names[head] = names.get(head, False) or bool(tail)
        out = [{"name": "/", "root": "sd"}]
        out += [{"name": n, **({"dir": True} if d else {})} for n, d in sorted(names.items())]
        return web.json_response(out)

    async def explorer_post(request):
        path = unicodedata.normalize("NFC", request.query.get("path", ""))
        await request.read()
        if path not in state["files"]:
            state["files"].append(path)
            save()
        log(f"upload {path}")
        return web.Response(text="OK")

    async def explorer_delete(request):
        path = request.query.get("path", "")
        state["files"] = [f for f in state["files"] if f != path and not f.startswith(path + "/")]
        save()
        return web.Response(text="OK")

    async def ok(request):
        return web.Response(text="OK")

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "ping" in data:
                    await ws.send_json({"pong": "pong"})
                elif "trackinfo" in data:
                    await ws.send_json({"trackinfo": {"playMode": 0, "numberOfTracks": 0}})
        finally:
            sockets.discard(ws)
        return ws

    async def fake_card(request):
        tag = request.query.get("id", "")
        log(f"card placed: {tag}")
        await broadcast({"rfidId": tag})
        entry = state["rfid"].get(tag)
        if entry and entry.get("fileOrUrl"):
            await broadcast({"trackinfo": {"playMode": entry["playMode"], "numberOfTracks": 1,
                                           "name": entry["fileOrUrl"], "pausePlay": False}})
        return web.json_response({"ok": True, "known": entry is not None})

    app = web.Application()
    app.router.add_get("/info", info)
    app.router.add_get("/rfid", rfid_get)
    app.router.add_post("/rfid", rfid_post)
    app.router.add_delete("/rfid", rfid_delete)
    app.router.add_get("/explorer", explorer_get)
    app.router.add_post("/explorer", explorer_post)
    app.router.add_put("/explorer", ok)
    app.router.add_delete("/explorer", explorer_delete)
    app.router.add_post("/exploreraudio", ok)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/_fake/card", fake_card)
    log(f"listening on 127.0.0.1:{args.port} (MAC {mac})")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
