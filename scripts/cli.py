"""`funkuino <command> [args…]` — the command dispatcher.

This is the single implementation. `bin/funkuino` (checkout and macOS bundle)
picks the right interpreter in shell and then calls straight into here, and the
installed console script is this module's ``main`` — so there is one dispatcher,
not one per packaging.

A command <name> is the module ``<name>.py`` next to this file — or, for
anything not shipped, ``<data folder>/extensions/<name>.py``. The extensions
directory is the supported place for private or machine-local tooling: the code
root is read-only both in the packaged app and in site-packages, so nothing may
be installed into the shipped set.

Why a dispatcher at all, rather than one console script per command: the Studio
agent's permission rules are TEXT patterns matched against the Bash command line
(studio_agent.py ALLOWED_TOOLS / ASK_RULES). A bare, canonical `funkuino sync …`
is what those patterns match, no matter where code and data live — and keeping
the *subcommand* in the matched string is what stops an "always allow" on a
harmless command from generalising to all of them.

Commands run in-process (``runpy``), not as a grandchild interpreter: it saves a
process spawn per invocation and keeps Ctrl+C landing on the command itself,
which matters for sync's crash-safe manifest handling.
"""
from __future__ import annotations

import codecs
import contextlib
import os
import runpy
import sys
from pathlib import Path

# Support modules, not commands.
INTERNAL = {"espuino", "sync_state", "print_state", "studio_state",
            "studio_agent", "progress", "cli"}

PKG_DIR = Path(__file__).resolve().parent


def _shipped_commands() -> list[str]:
    # Dotfiles are excluded as well as underscored ones: an archive unpacked
    # from macOS can carry AppleDouble siblings (`._cards.py`), which would
    # otherwise be offered as commands named `._cards`.
    return sorted(p.stem for p in PKG_DIR.glob("*.py")
                  if p.stem not in INTERNAL and not p.name.startswith((".", "_")))


def _extensions_dir() -> Path:
    # Imported here, never at module level: the global options below export
    # FUNKUINO_* into the environment and espuino.py resolves both roots at
    # *import* time, so importing it any earlier would freeze the wrong roots.
    import espuino
    return espuino.DATA_ROOT / "extensions"


def _usage() -> None:
    out = sys.stderr
    print("Usage: funkuino [--config-dir DIR] [--data-dir DIR] <command> [args…]", file=out)
    print(file=out)
    print("Global options:", file=out)
    print("  --config-dir DIR    where the installation's config.json lives", file=out)
    print("  --data-dir DIR      the media library, covers, manifests", file=out)
    print(file=out)
    print("Commands:", file=out)
    for name in _shipped_commands():
        print(f"  {name}", file=out)
    print("  python              # this installation's interpreter", file=out)
    try:
        ext_dir = _extensions_dir()
    except Exception:  # noqa: BLE001
        return  # --help is what one runs when something is broken; still show it
    extensions = sorted(ext_dir.glob("*.py")) if ext_dir.is_dir() else []
    if extensions:
        print(file=out)
        print(f"Extensions ({ext_dir}):", file=out)
        for script in extensions:
            print(f"  {script.stem}", file=out)


def _run_python(args: list[str]) -> int:
    """`funkuino python …` — our interpreter, with the modules importable.

    The workhorse for yt-dlp probes, tag inspection and ad-hoc regroup scripts.
    Exposed because `.venv/bin/python` is a path relative to a checkout, which
    is not where callers necessarily are — and does not exist at all for a pip
    installation.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PKG_DIR), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    argv = [sys.executable, *args]
    if hasattr(os, "execve") and sys.platform != "win32":
        os.execve(sys.executable, argv, env)  # replaces this process
    import subprocess
    return subprocess.run(argv, env=env).returncode


# What our own messages need beyond Latin-1 / cp1252, spelled in ASCII.
_ASCII_FALLBACK = {"\u2014": "-", "\u2013": "-", "\u2026": "...",
                   "\u2192": "->", "\u2190": "<-"}


def _console_fallback(exc: UnicodeError) -> tuple[str, int]:
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    text = "".join(_ASCII_FALLBACK.get(ch) or ascii(ch)[1:-1]
                   for ch in exc.object[exc.start:exc.end])
    return text, exc.end


def _harden_stdio() -> None:
    """Never crash on a character the console's charset cannot show.

    stdout/stderr encode with the locale's charset. Our messages and file names
    are full of characters Latin-1 / cp1252 lack (the em dash in half the
    messages), and stdout's default `strict` turns each of them into a
    UnicodeEncodeError — under `LC_ALL=de_DE.ISO8859-1`, or on Windows as soon
    as stdout is a pipe. The charset itself is left alone: it is what the
    terminal (or whoever reads the pipe) decodes, so switching to UTF-8 would
    garble every umlaut it *can* show. Only the unshowable characters change,
    to ASCII look-alikes or a `\\u…` escape. A UTF-8 stream — macOS, Linux, an
    interactive Windows console, Studio's children — has nothing to fix, and
    an error handler picked in PYTHONIOENCODING (`latin-1:replace`) wins.
    """
    if ":" in os.environ.get("PYTHONIOENCODING", ""):
        return
    codecs.register_error("funkuino-console", _console_fallback)
    for stream in (sys.stdout, sys.stderr):
        try:
            if codecs.lookup(stream.encoding).name != "utf-8":
                stream.reconfigure(errors="funkuino-console")
        except (AttributeError, LookupError, TypeError, ValueError):
            pass  # None (pythonw), or replaced by something without reconfigure


def main(argv: list[str] | None = None) -> int:
    _harden_stdio()
    try:
        return _dispatch(argv)
    except BrokenPipeError:
        # `funkuino --help | head` closes the pipe on us. Exit like the shell
        # tools do rather than with a traceback — and point the remaining
        # buffered output at /dev/null, or the interpreter raises it again
        # while flushing at exit.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


def _dispatch(argv: list[str] | None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Flat imports (`import espuino`) are the one and only import style, in the
    # checkout and installed alike — so a module is never loaded twice under two
    # names, which would give the manifests two sets of in-memory state. Set up
    # before anything else here: even `--help` needs it, because listing the
    # extensions asks espuino where the data folder is.
    if str(PKG_DIR) not in sys.path:
        sys.path.insert(0, str(PKG_DIR))

    # Global options come before the command and take effect as environment,
    # not as arguments: they must be set before espuino.py is imported anywhere,
    # because it resolves both roots at import time. This is also the lever for
    # testing the first-run flow (`funkuino --config-dir /tmp/probe studio`).
    while len(args) >= 2 and args[0] in ("--config-dir", "--data-dir"):
        var = "FUNKUINO_CONFIG_DIR" if args[0] == "--config-dir" else "FUNKUINO_DATA_DIR"
        os.environ[var] = args[1]
        del args[:2]

    if not args:
        _usage()
        return 2

    cmd, rest = args[0], args[1:]
    if cmd in ("-h", "--help", "help"):
        _usage()
        return 0

    if cmd == "python":
        return _run_python(rest)

    path: Path | None = None
    if cmd in _shipped_commands():
        path = PKG_DIR / f"{cmd}.py"
    elif "/" in cmd or "\\" in cmd or cmd.startswith("."):
        pass  # never let a command name escape into a path lookup
    else:
        candidate = _extensions_dir() / f"{cmd}.py"
        if candidate.is_file():
            path = candidate

    if path is None:
        print(f"funkuino: unknown command '{cmd}'", file=sys.stderr)
        _usage()
        return 2

    sys.argv = [str(path), *rest]
    try:
        # An extension is typically a symlink into another checkout; run_path
        # follows it while the sibling imports still resolve here, because
        # PKG_DIR is on sys.path above.
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        # `raise SystemExit("message")` prints the message when the interpreter
        # unwinds it — catching it here would otherwise turn an actionable error
        # (require_ffmpeg, argparse) into a bare exit code.
        print(exc.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
