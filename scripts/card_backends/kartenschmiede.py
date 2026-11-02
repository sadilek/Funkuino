#!/usr/bin/env python3
"""Kartenschmiede — print the cards as PVC cards (an order at the card service).

The card-service backend: it turns "these covers have no card yet" into an order
at a Kartenschmiede deployment — a small web shop with a card printer behind it.
It ships with Funkuino, because a card order is a second medium for the same
command, not a second tool:

    funkuino cards --backend kartenschmiede                  # covers not yet printed
    funkuino cards --backend kartenschmiede --dry-run        # list them, contact the service
    funkuino cards --backend kartenschmiede "Bibi und Tina"  # only this subpath
    funkuino cards --backend kartenschmiede --all            # incl. already-printed covers
    funkuino cards --backend kartenschmiede --uuid U         # add to an order created earlier
    funkuino cards --backend kartenschmiede --list           # the orders the print history remembers
    funkuino cards --backend kartenschmiede --forget U       # drop U's page from the print history

The order stays a DRAFT — its page is what the customer reviews and pays on, and
the printing itself is the back-office's business.

**Configuration.** The default is the reference deployment of the service
(``DEFAULT_SERVER``, publicly resolvable) — this backend talks to a card *shop*,
and that shop is where the cards come from, so there is rarely a reason to point
it elsewhere. Point it elsewhere anyway with, in this order: ``--server URL``,
``KARTENSCHMIEDE_SERVER`` in the environment, or ``kartenschmiede_server`` in
``config.json``. The address is not a secret, so it does not go into
``credentials.env``.

**Third party.** The covers and their names leave the machine for somebody
else's server, so ``notice()`` describes the service and Studio shows that as a
dialog the first time the medium is chosen; the medium is usable there only
once it was accepted (``print_consent`` in ``config.json``).

The service being one operator's does not make this file operator-specific: a
second card shop would be a second backend beside this one, which is what
``card_backends``' seam is for. Keep it that way — the flexibility costs nothing.
"""

from __future__ import annotations

import os
import uuid as uuidlib
from pathlib import Path

import requests

import cards
import card_backends
import espuino
import print_state

NAME = "kartenschmiede"
LABEL = "Kartenschmiede (RFID-Karte)"
PAGE = False

# The service's print raster (PRINT_RASTER_WIDTH/HEIGHT) is the printer's own
# bitmap — the CR80 outline at 600 dpi

DEFAULT_SERVER = "https://kartenschmiede.home.tilman.baumann.name"
# The reference deployment of the service, and the default on purpose: this
# backend talks to a card SHOP, and that shop is where the cards come from — a
# user has almost no reason to point it elsewhere. 
#
# Why a shop exists to default to at all: card printers are expensive, so the
# service is run as a community offering — someone has to bite that bullet for
# everyone. Nothing here is tied to one operator, though: flexibility costs the
# operator nothing, and a second service belongs in a backend of its OWN,
# `card_backends/<other>.py` beside this file, exactly like a second medium. 
#
# Overridable, in this order: --server, KARTENSCHMIEDE_SERVER in the
# environment, `kartenschmiede_server` in config.json.
ENV_SERVER = "KARTENSCHMIEDE_SERVER"
CONFIG_KEY = "kartenschmiede_server"

# What a run in the shared print manifest says about itself. A run WITHOUT a
# type is the A4 sheet path — this one is the PVC card path. `url` is the
# order's page, which is also what makes the manifest the ONLY order record
# there is: `--list` reads the runs back and `--forget` strips those two keys.
RUN_TYPE = "pvc"

# A card write renders the rasters synchronously, so it is seconds, not
# milliseconds.
TIMEOUT = (5.0, 120.0)


# --- configuration ----------------------------------------------------------
def configured_server() -> str | None:
    """The address of the deployment, wherever it was configured.

    ``KARTENSCHMIEDE_SERVER`` in the environment, or ``kartenschmiede_server``
    in ``config.json``; None when neither is set (``DEFAULT_SERVER`` applies).
    """
    env = os.environ.get(ENV_SERVER)
    if env:
        return env
    try:
        value = espuino.read_app_config().get(CONFIG_KEY)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def _server(args) -> str:
    """The deployment this run talks to: flag, then configuration, then default."""
    server = (getattr(args, "server", None) or server_address() or "").rstrip("/")
    if not server:
        raise card_backends.BackendError(
            "no Kartenschmiede address configured — pass --server URL, set "
            f"{ENV_SERVER}, or add \"{CONFIG_KEY}\" to {espuino.APP_CONFIG_FILE}")
    return server


def server_address() -> str:
    """The deployment a run without ``--server`` talks to."""
    return (configured_server() or DEFAULT_SERVER).rstrip("/")


def notice() -> dict:
    """What a user is told before the first order: the covers go to a third party.

    Studio shows it as a dialog the first time this medium is chosen (in the
    UI's language, like LABEL) and keeps the medium unusable until it is
    accepted; ``url`` is the service's own page, linked from the dialog.
    """
    server = server_address()
    host = server.split("://", 1)[-1]
    return {
        "title": "Kartenschmiede: ein Dienst außerhalb von Funkuino",
        "text": [
            "Kartenschmiede ist ein kleiner Kartendruck-Dienst eines unabhängigen "
            "Betreibers, nicht Teil von Funkuino. Er druckt eure Cover auf "
            "Plastik-RFID-Karten und schickt sie euch zu; bestellt und bezahlt wird "
            "auf seiner eigenen Bestellseite.",
            f"Beim Bestellen schickt Funkuino die ausgewählten Cover-Bilder und ihre "
            f"Namen (Ordner und Titel aus eurer Bibliothek) an den Server {host}. "
            f"Was dort mit den Daten geschieht, bestimmt sein Betreiber.",
        ],
        "url": server,
        "linkText": host,
    }


# --- the service ------------------------------------------------------------
class KartenschmiedeError(card_backends.BackendError):
    """The service refused, could not be reached, or answered where we did not ask.

    ``status`` is the HTTP status when there was one, so callers can react to a
    specific code (the cards read treats 404 as "nothing there yet") without
    string-matching a message.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Client:
    """The little bit of the Kartenschmiede API this backend needs.

    All of it is the credential-free surface a *customer* may use while the
    order is a draft: the order uuid is the credential. No operator token.
    """

    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()

    # -- plumbing --
    def _send(self, method: str, path: str, **kw):
        """One request. Redirects are REFUSED, and an HTTP error raises as ours.

        Refused rather than followed because a 301/302 turns a POST into a GET —
        browser semantics, and requests implements them — while the Location need
        not even carry the request path. Following one would therefore create
        nothing and still look like a success.
        """
        try:
            response = self.session.request(method, f"{self.server}{path}",
                                            timeout=TIMEOUT, allow_redirects=False, **kw)
        except requests.RequestException as exc:
            raise KartenschmiedeError(
                f"{self.server} not reachable ({exc.__class__.__name__}: {exc})") from None
        if 300 <= response.status_code < 400:
            where = response.headers.get("Location") or "somewhere else"
            raise KartenschmiedeError(
                f"{self.server} redirects to {where} — use that address instead "
                f"(--server, or {ENV_SERVER}); a redirect drops the method and the path, so an "
                f"order sent through it would create nothing")
        return response

    def _call(self, method: str, path: str, **kw):
        response = self._send(method, path, **kw)
        if not response.ok:
            raise KartenschmiedeError(self._detail(method, path, response),
                                      status=response.status_code)
        return response

    @staticmethod
    def _detail(method: str, path: str, response) -> str:
        detail = " ".join(response.text.split())[:300] or "(no body)"
        return f"{method} {path} -> HTTP {response.status_code}: {detail}"

    @staticmethod
    def _json(response) -> dict:
        try:
            return response.json()
        except ValueError:
            raise KartenschmiedeError("the service answered something that is not JSON") from None

    # -- the calls --
    def shop_config(self) -> dict:
        """Public shop settings: prices, ribbon stock, whether payment works at all."""
        return self._json(self._call("GET", "/api/shop/config"))

    def create_order(self, order_id: str) -> bool:
        """Create the order at a uuid we choose; False when it already existed.

        ``POST /api/prefixes`` is the credential-free creation path, and a repeat
        answers 409 — not an error here, but how ``--uuid`` adds more cards to an
        order from an earlier run. Nothing but the uuid is sent (no ribbon: the
        shop's default applies and the customer changes it on the order page).
        """
        response = self._send("POST", "/api/prefixes", json={"uuid": order_id})
        if response.status_code == 409:
            return False
        if not response.ok:
            raise KartenschmiedeError(self._detail("POST", "/api/prefixes", response),
                                      status=response.status_code)
        return True

    def cards(self, order_id: str) -> list[dict]:
        """The order's cards. An order nobody has written yet reads as an empty draft."""
        try:
            response = self._call("GET", f"/api/prefixes/{order_id}/cards")
        except KartenschmiedeError as exc:
            if exc.status == 404:
                return []
            raise
        data = self._json(response)
        cards_in_order = data.get("cards")
        return cards_in_order if isinstance(cards_in_order, list) else []

    def upload_card(self, order_id: str, name: str, blob: bytes) -> dict:
        """Add one card to the order: the front image plus its name, single-sided."""
        return self._json(self._call(
            "POST", f"/api/prefixes/{order_id}/cards",
            data={"name": name, "back": "none"},
            files={"image": ("card.jpg", blob, "image/jpeg")}))


def shop_summary(config: dict) -> str:
    """A readable line about the target: can it print, what ribbons cost, what ships."""
    lines = []
    enabled = config.get("enabled")
    lines.append(f"payment: {'enabled' if enabled else 'disabled'}"
                 f"  currency: {config.get('currency', '?')}")
    prices = config.get("prices")
    if isinstance(prices, list):
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            price = entry.get("price_cents")
            unit = entry.get("unit", "card")
            amount = "?" if not isinstance(price, int) else f"{price / 100:.2f}"
            stock = "in stock" if entry.get("available") else "NOT in stock"
            lines.append(f"{entry.get('ribbon', '?')}: {amount} per {unit} ({stock})")
    shipping = config.get("shipping_cents")
    if isinstance(shipping, int):
        countries = config.get("countries")
        where = ""
        if isinstance(countries, list) and countries:
            where = f", to {', '.join(str(c) for c in countries)}"
        lines.append(f"shipping: {shipping / 100:.2f}{where}")
    return "\n".join(lines)


# --- the card face ----------------------------------------------------------
def face_bytes(path: Path) -> bytes:
    """The cover image, exactly as it is on disk: no trim, no crop, no re-encode.
    """
    return path.read_bytes()


# --- the order record (the shared manifest's own runs) ----------------------
def order_id_of(url: str) -> str:
    """The uuid an order page URL ends in (``…/p/<uuid>``)."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def order_bookmarks(state: print_state.PrintState) -> list[dict]:
    """The orders the print manifest remembers, newest first.

    A PVC run carries ``type`` and ``url``, and nothing else writes an order
    down — so this reads them back out of the one manifest instead of keeping a
    second file that could disagree with it.

    Runs are merged by the order's **uuid**, not by URL: the same order reached
    through two addresses (the short alias, http vs https, another host) is still
    one order, and the newest run's address is the one to open. Their covers are
    unioned in run order.
    """
    merged: dict[str, dict] = {}
    for run in state.runs:
        url = run.get("url")
        if not url:
            continue
        order_id = order_id_of(url)
        entry = merged.setdefault(order_id, {"uuid": order_id, "url": url,
                                            "at": run.get("at"), "covers": []})
        entry["url"] = url                     # runs are walked oldest-first: newest wins
        entry["at"] = run.get("at") or entry["at"]
        for rel in (run.get("changes") or {}):
            if rel not in entry["covers"]:
                entry["covers"].append(rel)
    return sorted(merged.values(), key=lambda entry: entry.get("at") or "", reverse=True)


def forget_bookmark(state: print_state.PrintState, order_id: str) -> int:
    """Drop one order page from the manifest: the runs stay, their bookmark keys go.

    Returns how many runs were unbooked. The covers stay printed — this forgets
    the page, not the print — which is why it strips two keys instead of removing
    the runs.
    """
    wanted = order_id.strip().lower()
    dropped = 0
    for run in state.runs:
        url = run.get("url")
        if url and order_id_of(url).lower() == wanted:
            run.pop("url", None)
            run.pop("type", None)
            dropped += 1
    return dropped


# --- helpers ----------------------------------------------------------------
def relative(cover: Path) -> str:
    """A cover's path relative to card-covers/, as the manifest spells it."""
    covers_dir = espuino.DEFAULT_COVERS_DIR
    return str(Path(cover).resolve().relative_to(covers_dir.resolve()))


def card_name(rel: str) -> str:
    """A card's name in the order: the cover's path without ``.jpg`` (the service
    wants a non-empty name, and the path is what a human reads in the order page)."""
    return rel[:-4] if rel.lower().endswith(".jpg") else rel


# --- the backend interface --------------------------------------------------
def add_arguments(parser) -> None:
    parser.add_argument("--uuid", help="Add to this order instead of creating a new one")
    parser.add_argument("--server",
                        help="Base URL of the card service "
                             f"(default: ${ENV_SERVER} or \"{CONFIG_KEY}\" in config.json, "
                             f"else {DEFAULT_SERVER})")
    parser.add_argument("--list", action="store_true",
                        help="List the orders the print history remembers and exit")
    parser.add_argument("--forget", metavar="UUID",
                        help="Drop UUID's order page from the print history and exit "
                             "(the order itself and its covers are untouched)")


def preflight(args) -> int | None:
    """``--list`` / ``--forget``: about the orders, not about the covers.

    They must run before the cover selection, so they are handled here and the
    exit code returned — the two flags the medium owns that place no cards.
    """
    state = print_state.PrintState.load(cards.STATE_FILE)
    if args.forget:
        dropped = forget_bookmark(state, args.forget)
        if not dropped:
            raise card_backends.BackendError(
                f"no order page for {args.forget} in the print history "
                f"({cards.STATE_FILE}) — see --list.")
        state.save()
        print(f"Forgot the order page {args.forget} in {dropped} print run(s). The order "
              f"itself is untouched, and its covers stay printed.")
        return 0
    if args.list:
        bookmarks = order_bookmarks(state)
        if not bookmarks:
            print(f"No orders in the print history ({cards.STATE_FILE}).")
            return 0
        print(f"{len(bookmarks)} order(s) in the print history ({cards.STATE_FILE}):")
        for entry in bookmarks:
            covers = entry.get("covers") or []
            print(f"  {entry.get('at', '?')}  {len(covers)} cover(s)  {entry.get('uuid', '?')}")
            print(f"      {entry.get('url', '?')}")
        return 0
    return None


def describe(covers, args) -> str:
    """The dry run: what the service is, and what the face will be."""
    server = _server(args)
    try:
        shop = Client(server).shop_config()
    except KartenschmiedeError as exc:
        raise card_backends.BackendError(str(exc)) from None
    return (f"{server}\n{shop_summary(shop)}\n"
            f"Card face: the cover image as it is on disk, untouched — the service "
            f"fits and renders it (no trim, no crop).")


def emit(covers, args, log=print) -> card_backends.Artifact:
    """Put the covers into an order and hand back its page as the run's record."""
    server = _server(args)
    client = Client(server)

    if args.uuid:
        try:
            uuidlib.UUID(args.uuid)
        except ValueError:
            raise card_backends.BackendError(f"--uuid wants a uuid, not {args.uuid!r}.") from None

    order_id = args.uuid or str(uuidlib.uuid4())
    created = client.create_order(order_id)
    log(f"\n{server}: order {order_id} ({'created' if created else 'already existed'}).")

    # A re-run must not duplicate cards — each one costs a card and a ribbon
    # panel — so ask what is in the order before uploading.
    already = {str(card.get("name")) for card in client.cards(order_id)}

    uploaded: list[str] = []
    placed: list[Path] = []
    failed: list[str] = []
    for cover in covers:
        rel = relative(cover)
        name = card_name(rel)
        if name in already:
            log(f"  in the order already: {rel}")
            uploaded.append(rel)
            placed.append(cover)
            continue
        try:
            client.upload_card(order_id, name, face_bytes(cover))
        except (KartenschmiedeError, OSError) as exc:
            log(f"  FAILED  {rel}: {exc}")
            failed.append(rel)
            continue
        log(f"  card    {rel}")
        uploaded.append(rel)
        placed.append(cover)

    if not uploaded:
        raise card_backends.BackendError(
            f"nothing was added to the order; {len(failed)} cover(s) failed.")

    url = f"{server}/p/{order_id}"
    try:
        total = len(client.cards(order_id))
    except (KartenschmiedeError, OSError):
        total = len(uploaded)
    log(f"\nOrder has {total} card(s).")
    if failed:                    # not in `placed`, so cards.py leaves them unmarked
        log(f"NOTE: {len(failed)} cover(s) failed; they still count as new: {', '.join(failed)}")
    log(f"\nReview and pay here: {url}")
    return card_backends.Artifact(run_out=None,
                                  run_fields={"type": RUN_TYPE, "url": url},
                                  open_target=url,
                                  placed=placed)
