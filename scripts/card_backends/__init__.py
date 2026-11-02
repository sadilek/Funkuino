"""Card-printing backends: who puts a cover on a card.

``cards`` owns *which* covers are due and *marks* them printed; the medium owns
what a card looks like and where it goes. That split is the seam, and it exists
so that adding a medium does not mean adding conditionals to the command:

* ``card_backends.pdf`` — the shipped backend: square cards on A4 sheets.
* ``<data folder>/extensions/card_backends/<name>.py`` — anything private or
  machine-local (a card-printing service, a different printer, a preview that
  only writes files). Discovered at runtime, never shipped, never maintained by
  whoever maintains the A4 path.

A backend module provides, at module level:

    NAME: str          # the id on the command line and in config.json
    LABEL: str         # user-facing name, in the UI's language
    PAGE: bool         # True when the medium is a page (its UI counts pages)

    def add_arguments(parser): ...           # its own flags
    def preflight(args) -> int | None: ...   # optional: handle a flag that is
                                             # about the orders, not the covers,
                                             # and return an exit code
    def describe(covers, args) -> str | None  # extra lines for --dry-run
    def emit(covers, args, log=print) -> Artifact
    def notice() -> dict                     # optional: the medium sends the
                                             # covers to a third party; Studio
                                             # shows {title, text, url, linkText}
                                             # before it is first used

``emit`` does the work and returns the run's record; the marking, the counting
and the "nothing new" logic stay in ``cards`` so every medium feeds the one
manifest the same way (see ``Artifact``).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import NamedTuple

DEFAULT = "pdf"
_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,32}\Z")

# Loaded plugin modules by "<path>:<mtime_ns>", and the last registry() answer
# with the monotonic time it was made (see the docstrings).
_MODULES: dict[str, object] = {}
_REGISTRY: tuple[float, list[dict] | None] = (0.0, None)


class BackendError(RuntimeError):
    """A medium reporting why it could not do the job.

    Raised by a backend, printed by ``cards`` as one ``ERROR:`` line rather than
    a traceback — the failure is the medium's own (a service refused, a file is
    missing), not a bug in the command.
    """


class Artifact(NamedTuple):
    """What a backend's run produced, in the manifest's own terms.

    ``run_out`` becomes the run's ``out`` (the file a paper sheet was written
    to, or None for a medium that has no file); ``run_fields`` are extra keys
    merged into the same run — a card service records ``{"type": ..., "url":
    ...}``, which is also its order record. ``open_target`` is a file path or a
    URL to hand to the user, and is what ``--open`` acts on. ``placed`` lists
    the covers that actually made it onto the medium when that is fewer than
    were handed over (a card service refused one); only those are marked
    printed. None means all of them.
    """

    run_out: str | None = None
    run_fields: dict | None = None
    open_target: str | None = None
    placed: list[Path] | None = None


def package_dir() -> Path:
    """Where the shipped backends live (next to this module)."""
    return Path(__file__).resolve().parent


def plugin_dir() -> Path:
    """``<data folder>/extensions/card_backends/`` — where backends not shipped
    with the app live, the same place private commands live. Resolved lazily:
    espuino settles DATA_ROOT at *import* time, and the dispatcher's --data-dir
    is applied before that, so importing it here at module level would be one
    step too early in a child process."""
    import espuino
    return espuino.DATA_ROOT / "extensions" / "card_backends"


def _candidates() -> dict[str, Path]:
    """Every backend name -> the file implementing it. A shipped name wins, the
    same precedence the command dispatcher uses (cli._dispatch)."""
    found: dict[str, Path] = {}
    plugins = sorted(plugin_dir().glob("*.py")) if plugin_dir().is_dir() else []
    for path in plugins:
        if path.name.startswith((".", "_")):
            continue
        found.setdefault(path.stem, path)
    for path in sorted(package_dir().glob("*.py")):
        if path.name.startswith((".", "_")):
            continue
        found[path.stem] = path            # shipped overrides a plugin of the name
    return found


def _load_path(path: Path, name: str):
    """Import a plugin from its file, once per (path, mtime).

    Memoised because ``registry()`` is asked on every ``/api/state`` and a
    plugin is a real module with real imports — and keyed by mtime so editing
    one is picked up without a restart.
    """
    key = f"{path}:{path.stat().st_mtime_ns}"
    cached = _MODULES.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(f"card_backends_plugin_{name}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERROR: cannot load the backend {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULES[key] = module
    return module


def load(name: str):
    """The backend module called ``name``, or exit naming the ones available.

    Imported by name from the package for the shipped ones, by path for a
    plugin — a plugin must not be able to shadow a shipped module of its own
    name, and it needs no ``sys.path`` entry to be importable.
    """
    if not _NAME_RE.match(name or ""):
        raise SystemExit(f"ERROR: {name!r} is not a backend name.")
    candidates = _candidates()
    path = candidates.get(name)
    if path is None:
        known = ", ".join(sorted(candidates)) or "(none)"
        raise SystemExit(f"ERROR: unknown backend {name!r}. Available: {known}.")
    if path.parent == package_dir():
        return importlib.import_module(f"{package_name()}.{name}")
    return _load_path(path, name)


def package_name() -> str:
    return __name__


def registry() -> list[dict]:
    """The backend list for the UI: ``[{id, label, page, notice}]``.

    A backend that fails to import is left out rather than breaking the list
    (the UI is not the place to report a broken plugin) — the name then simply
    is not offered, and ``load`` says so if someone asks for it anyway.

    Memoised for a few seconds: it is answered on every ``/api/state``, and a
    backend installed while Studio runs should appear without a restart.
    """
    global _REGISTRY
    stamp, cached = _REGISTRY
    if cached is not None and time.monotonic() - stamp < 5.0:
        return [dict(entry) for entry in cached]
    entries: list[dict] = []
    for name, path in sorted(_candidates().items()):
        try:
            module = (importlib.import_module(f"{package_name()}.{name}")
                      if path.parent == package_dir() else _load_path(path, name))
        except Exception:  # noqa: BLE001 — a broken plugin must not break the list
            continue
        notice = getattr(module, "notice", None)
        try:
            text = notice() if callable(notice) else None
        except Exception:  # noqa: BLE001 — same rule as a failed import
            continue
        entries.append({"id": getattr(module, "NAME", name),
                        "label": getattr(module, "LABEL", name),
                        "page": bool(getattr(module, "PAGE", False)),
                        "notice": text})
    # The default medium comes first: it is what the UI falls back to when no
    # preference is stored, and what a person expects to see at the top of the
    # list. Alphabetical order would put an installed card service there.
    entries.sort(key=lambda entry: (entry["id"] != DEFAULT, entry["id"]))
    _REGISTRY = (time.monotonic(), entries)
    return [dict(entry) for entry in entries]


def peek_backend(argv: list[str] | None) -> str:
    """The ``--backend`` value, read before the real parser exists.

    A backend owns its own flags, so which flags are legal is only known after
    the backend is loaded: this is the cheap first pass that makes that
    possible (``cards --backend kartenschmiede --uuid …``).
    """
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--backend", default=DEFAULT)
    known, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.backend


def reveal(target: str | None) -> str | None:
    """Show what a run produced — a sheet, or an order's page; the reason as a
    string on failure.

    Three hand-offs, because the target is either a file or a URL and the
    platforms differ:

    * macOS gets ``/usr/bin/open`` explicitly — the same call Studio makes —
      because ``webbrowser`` in a bundled runtime can pick a terminal browser, or
      none.
    * A **file** elsewhere goes to the desktop's own opener (``xdg-open``, or
      ``os.startfile`` on Windows): a PDF belongs to the user's PDF viewer, not to
      a browser. This is the case that was missing — ``webbrowser`` got the sheet
      or nothing did, so on Linux a finished print landed in the void.
    * A **URL** elsewhere goes to ``webbrowser`` (it honours ``$BROWSER``), with
      the desktop opener as the fallback.

    Never raises: showing a finished run must not fail it.
    """
    if not target:
        return None
    try:
        if sys.platform == "darwin":
            return _spawn(["/usr/bin/open", target])
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]  # Windows only
            return None
        if target.startswith(("http://", "https://")):
            return None if webbrowser.open(target) else "no browser handled it"
        if opener := shutil.which("xdg-open"):
            return _spawn([opener, target])
        return None if webbrowser.open(target) else "no browser handled it"
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def _spawn(argv: list[str]) -> str | None:
    """Run a hand-off command; the reason as a string if it refused."""
    result = subprocess.run(argv, capture_output=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return result.stderr.strip() or f"{argv[0]} exited {result.returncode}"
    return None
