# Funkuino — ESPuino management

Tooling to manage an [ESPuino](https://espuino.de) (an ESP32-based, RFID-controlled
audio player for kids) over the network, so the media library and settings can be
driven from scripts instead of clicking through the web interface.

The primary users are German-speaking (the Studio UI and the embedded agent
talk German); **code and documentation stay in English.**

Single-maintainer repo: **commit straight to `main`**, no feature branches. Keep
the history clean — while iterating on feedback about a change, **amend** its
commit instead of stacking "fix the fix" commits on top.

## The device

- Reachable at `espuino.local` by default; any hostname/IP works via `--host`
  or `ESPUINO_HOST`.
- Firmware: ESPuino `dev` branch — a prebuilt GitHub build (reports e.g.
  `20260720-DEV`), installed via OTA through the device's own web interface, not
  compiled locally. FTP credentials default to `esp32` / `esp32`.
- The audio library lives on the attached storage. The same files are kept
  locally in `files/` (git-ignored — large & private); the mirror pushes `files/`
  to the device.
- Firmware source: [biologist79/ESPuino](https://github.com/biologist79/ESPuino)
  (`dev` branch). A local checkout is the reference for API/behaviour questions
  and the workbench for custom-firmware branches (see Roadmap; first shipped
  fix: upstream PR #444). PlatformIO builds need a venv with a PlatformIO
  version that resolves the pioarduino platform (6.2.0a3 works, stock 6.1.19
  does not).

## Layout

```
Funkuino/
  bin/funkuino          # command dispatcher: `funkuino <command>` -> scripts/<command>.py
  sync                  # wrapper: rsync-style mirror to the device
  download              # wrapper: fetch audio into files/ (yt-dlp)
  prepare               # wrapper: merge a folder of tracks into one audiobook
  covers                # wrapper: extract title images into card-covers/
  cards                 # wrapper: lay out card-covers/ into printable PDFs
  studio                # wrapper: Funkuino Studio web app (dashboard + agent)
  dev/shot.py           # headless screenshots of Studio (checkout-only, not shipped)
  pyproject.toml        # the wheel: pipx-installable (see Python package)
  hatch_build.py        # build hook: dependencies come from requirements.txt
  scripts/
    cli.py              # the command dispatcher (bin/funkuino and the console script)
    espuino.py          # HTTP API client + device quirks (the knowledge lives here)
    sync_state.py       # per-device upload manifest
    sync.py             # the rsync-style mirror CLI
    download.py         # yt-dlp download in the ESPuino naming conventions
    prepare.py          # concat + spoken intro (Hörspiel merge)
    covers.py           # extract embedded covers -> card-covers/
    cards.py            # compose printable A4 card sheets from card-covers/
    print_state.py      # manifest of already-printed covers
    studio.py           # Studio: aiohttp server, WS hub, sync/cards jobs
    studio_state.py     # Studio: library scan -> per-card pipeline status
    studio_agent.py     # Studio: Claude Agent SDK session manager
    studio_web/         # Studio: vanilla-JS frontend (index.html, app.js, style.css)
  extensions/           # git-ignored; commands not shipped with the app (see below)
  files/                # local media library (git-ignored) -> mirrored to device
  card-covers/          # git-ignored; full-res title images for printing cards
  print-sheets/         # git-ignored; generated printable PDFs
  requirements.txt      # requests, websocket-client, yt-dlp, Pillow, aiohttp, claude-agent-sdk
  .venv/                # git-ignored; created from requirements.txt
  status/               # git-ignored; sync-<mac>.json + print-history.json + README.txt
```

External tools: **ffmpeg** (audio conversion/merge) and macOS **`say`** (spoken
intros) must be on PATH; `id3v2` is not required. (The packaged app brings its
own ffmpeg — see *macOS app*.) Python deps (incl. **Pillow**
for the card sheets) come from `requirements.txt` in the venv.

**ffmpeg is checked up front, never discovered by a crash.** It is the one
dependency we cannot supply outside the macOS bundle, and *everything* audio
shells out to it. `espuino.require_ffmpeg(purpose)` exits with an install
command at the top of `prepare`/`download`/`covers` main() — before a long
download, not after; `ffmpeg_hint()` reads /etc/os-release so the Linux hint
names the right package manager. `ffmpeg_status()` additionally probes for
**libmp3lame** (a build without it passes "is it installed" and then fails every
conversion) and feeds Studio's `/api/state` → `tools.ffmpeg` → a persistent
banner, plus a stderr warning at server start. `say` is treated differently on
purpose: missing it only drops the spoken intro, so the merge continues.

### Commands: one dispatcher, two spellings

`funkuino <command> [args…]` is the single entry point; a command is simply
`scripts/<command>.py`, or `<data folder>/extensions/<command>.py` for anything
not shipped with the app (see *Extensions*). Plus one special command,
`funkuino python`, which is this installation's interpreter with the modules
importable. The `./sync`, `./download`, … wrappers in the root are one-liners
onto it and stay the convenient form **for humans**.

The dispatcher itself is `scripts/cli.py`. `bin/funkuino` only picks the
interpreter (bundle `runtime/` or `.venv/`) and calls into it; the installed
console script *is* `funkuino.cli:main`. Two copies of the command routing, one
in bash and one in Python, would drift apart. Commands run in-process via
`runpy`, so Ctrl+C lands on the command (sync's manifest handling depends on
that) instead of on a wrapper. Two things the bash version got for free and the
Python one has to do by hand: printing the message of a string-valued
`SystemExit` (`require_ffmpeg`, argparse) rather than swallowing it, and exiting
quietly on `BrokenPipeError` (`funkuino … | head`).

**The Studio agent must use the `funkuino <command>` form** (its sessions get
`bin/` on `PATH`). The reason is the permission layer: `ALLOWED_TOOLS` and
`ASK_RULES` are *text* patterns matched against the Bash command line, and the
`./` form only matches while the agent's cwd is the checkout — which stops being
true as soon as code and data live apart. A path handed over in an environment
variable would be worse still: `$FUNKUINO_HOME/sync`, `"${FUNKUINO_HOME}/sync"`
and the expanded absolute path are three different strings, so an ask rule would
be one spelling away from silently not firing. Both spellings are therefore
listed in `ASK_RULES`, and `_bash_head()` keeps the *subcommand* for the
dispatcher (`funkuino sync`), so an "always allow" on a harmless subcommand
cannot generalise to `funkuino sync`.

### Code root vs. data root

`espuino.REPO_ROOT` is where the **code** lives (wrappers, scripts, Studio web
assets) — read-only in a packaged app, so nothing may be written below it.
`espuino.BUNDLED_LAYOUT` says whether that root is one of *ours* (a checkout or
the bundle, both of which have `bin/funkuino` next to `scripts/`) or
site-packages from a pip install; it decides the data-root default and how our
own code spells a `funkuino <command>` invocation
(`dispatcher_argv()` / `dispatcher_bin_dir()`).
`espuino.DATA_ROOT` is where the **user's stuff** lives and is what everything
else derives from: `files/`, `card-covers/`, `print-sheets/`, `status/`,
`extensions/`, `CLAUDE.local.md`, and the agent session's cwd. **Nothing in it
is hidden**: someone copying a library by hand takes the folders they can see,
and a lost `status/` means re-uploading everything and reprinting every card
(it says so in its own README.txt). Its two manifests stay separate files —
the sync manifest is rewritten twice per uploaded file, and that write must not
be able to take the print history with it.

**Durability** (`espuino.write_text_atomically` / `keep_backup` /
`read_json_state`): temp file + rename is only half of it — the rename can
reach disk before the data, and a power cut then leaves an *empty* file where
the manifest was. So the temp file is fsynced before the rename and the
directory after it. Each run keeps one previous-good `.bak`, and a file that
does not parse is moved aside as `.corrupt-<ts>` rather than silently ignored:
the old code started fresh and overwrote the evidence on the next save, so a
corrupted manifest was indistinguishable from a new installation while quietly
re-uploading the whole library. Callers report the outcome (`load_status`,
`espuino.state_warning`). SQLite was considered and rejected: it drops `-wal` /
`-shm` siblings next to the file (the very thing a hand-copied library loses),
makes the state unreadable to the user, and would still need the pending-marker
semantics modelled by hand.

**Every text file is read and written with `encoding="utf-8"`** — never a bare
`read_text()` / `write_text()` / `open()`, which use the locale's charset.
Only on macOS and UTF-8 Linux is that the same thing; under Windows (cp1252) or
a Latin-1 locale it was not: the ffmpeg concat list (ffmpeg expects UTF-8)
either failed to encode an en dash or named files ffmpeg could not find, and a
`data_dir` with an umlaut in `config.json` (written as UTF-8 by the app's
JSONEncoder) resolved to a non-existent folder, so Studio showed an empty
library. `LC_ALL=de_DE.ISO8859-1` on a Mac reproduces such a system.

The **credentials do not live in the library**: `credentials.env` (`SDK_TOKEN=…`)
sits next to `config.json` in the config directory, because a media library gets
copied, shared and backed up and a token should not ride along. `.env` in the
data folder or the checkout is still read if present, for older setups.
Transient per-session progress files live there too, not in the user's music
folder. It resolves as **`FUNKUINO_DATA_DIR` > app config file
(`config.json`, `{"data_dir": …}`, in `FUNKUINO_CONFIG_DIR` or the platform's
config directory) > the checkout**. `_default_config_dir()` picks that per
platform — `~/Library/Application Support/Funkuino`, `%APPDATA%\Funkuino`,
else `$XDG_CONFIG_HOME/funkuino` — and asks `sys.platform`, never `os.uname()`,
which does not exist on Windows and would fail at *import* time.

Both roots are also dispatcher options — `funkuino --config-dir DIR --data-dir
DIR <command>` — which are applied to the environment before espuino.py is
imported, since it resolves them at *import* time (a flag parsed inside a script
would be too late). `--config-dir` is how the app's first-run flow is tested
repeatedly without disturbing the real installation. Default = the checkout, so
a plain `git clone` behaves exactly as before; the config file is how a packaged
app tells the CLI which folder it was pointed at, so both operate on one library
instead of diverging. **A pip install has no checkout to default to** — its
REPO_ROOT is site-packages, which is no place for a media library — so it falls
back to `~/Funkuino` (`_default_data_dir()`), visible in the home directory for
the same reason nothing inside the data folder is hidden. Use
`espuino.data_or_repo(name)` for the per-installation side files — they belong
to the data folder but may still sit in the checkout (the private overlay
symlinks them there).

Consequence for the agent: with a separate data folder its cwd holds no
CLAUDE.md, so `_system_append()` appends the code root's CLAUDE.md explicitly
(`setting_sources=["project"]` still picks up a data-folder CLAUDE.md on top).
Where that file *is* differs per installation, hence `espuino.knowledge_file()`:
above the code in a checkout and in the bundle, inside the package in a wheel
(there is no "above" in site-packages), which is why pyproject force-includes
it.

### Extensions

Commands that are not shipped with the app — private tooling, machine-local
helpers — live in `<data folder>/extensions/`:

```
extensions/topic.py                  # the command: `funkuino topic …`
extensions/topic.permissions.json    # {"allow": [...], "ask": [...]} for the agent
```

The script is run with `scripts/` on `PYTHONPATH`, so it imports `espuino`,
`download`, … like a shipped one (it is usually a symlink into another checkout,
and Python would otherwise resolve the sibling imports next to the *target*).
`studio_agent._extension_rules()` merges every `*.permissions.json` into
`ALLOWED_TOOLS`/`ASK_RULES`, so an extension carries its own gating instead of
this repo needing to know about it; an extension that declares nothing falls
through to the auto-mode classifier, which is the safe default. A broken
manifest is skipped, never fatal.

Why not `scripts/`: that is the code root, read-only in a packaged app. The
private overlay's `install` therefore links into `extensions/` and cleans up its
older `scripts/topic.py` / `.agent-allow.json` links (which still work in a
checkout — `.agent-allow.json` is still read — but have no place in an app).

## Quickstart

```bash
# one-time setup (development; an end user on Linux installs the wheel with pipx)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./sync                 # upload new/changed files from files/ to the device
./sync --dry-run       # show what would change, touch nothing
./sync --delete        # also remove device files not present locally
./sync --host <ip>     # target a specific device
```

Config precedence: CLI flags > `ESPUINO_*` env vars > defaults. Relevant vars:
`ESPUINO_HOST`, `ESPUINO_LOCAL_DIR`, `ESPUINO_REMOTE_ROOT`, and
`ESPUINO_HTTP_USER` / `ESPUINO_HTTP_PASSWORD` (only if the web UI is password
protected). `FUNKUINO_DATA_DIR` moves the whole data folder (see *Code root vs.
data root*): `FUNKUINO_DATA_DIR=/Volumes/Media/Funkuino ./cards --dry-run`.

## How the mirror works (and why)

One-way mirror `files/ -> device`, rsync-style:

1. **Existence** comes from a fast recursive HTTP directory listing.
2. **Change detection** comes from a local per-device manifest
   (`status/sync-<mac>.json`) recording the size+mtime of each file we last
   uploaded — the device offers no cheap way to read a remote file's size.
3. New and changed files are uploaded (strictly one at a time). `--delete`
   removes device files/dirs absent locally (with a guard: it refuses if the
   local side is empty; `/backup.txt` is never deleted).

**Crash / Ctrl+C safety (important).** A file is only ever trusted as synced
after its upload *confirmably* completed:

* Before an upload starts we write a `pending` marker for that path and save the
  manifest immediately; after success we overwrite it with size+mtime and save
  again. So the manifest is written incrementally, not once at the end.
* A `pending` entry left by an interrupted run means "we started this and never
  confirmed it" → it is re-uploaded next run, never skipped. This is why an
  aborted upload cannot leave a silently-truncated file that later looks done.
* `adopt` (trusting a file that is on the device but not in our manifest, e.g. a
  pre-populated library) is **off by default** — we cannot verify such a file is
  not a truncated leftover, so we re-upload it. Pass `--adopt-existing` only for
  the deliberate one-time seeding of a device already known to hold good files.

**Large files.** The device writes to SD slower than the network delivers, so a
big POST body fills the TCP window and the send blocks; a fixed short timeout
would abort it part-way (leaving a truncated file). The upload timeout is
therefore scaled to the file size (≥40 KiB/s floor) — needed for the ~30–40 MB
Hörspiel files. Observed throughput is ~700–800 KiB/s.

**Device identity:** each sync first calls `/info` and keys the manifest by the
device's **MAC address** (`status/sync-<mac>.json`), not the host name. So the
same physical device reached as `espuino.local` or by IP shares one manifest,
and two different ESPuinos get independent manifests automatically — just run
`./sync --host A` and `./sync --host B` (give the second device a distinct
hostname). The host name is stored only as a label.

## Downloading & preparing audio

`./download <url>` fetches audio into `files/` in the library's conventions
(128 kbps MP3, embedded cover + title/artist tags, umlauts/spaces kept). It
auto-detects the kind; override any name part with flags.

| Kind | Trigger | Result |
|------|---------|--------|
| Single song | single video | `Lieder/<Artist> - <Title>.mp3` |
| Music album | playlist | `<Artist> - <Album>/<NN> <Title>.mp3` (index as wide as the track count needs → keeps album order) |
| Hörspiel | playlist + `--audiobook` | `<Series>/<NNN> - <Title>.mp3` — all parts merged into ONE file with a spoken "<Series>. <Title>." intro (Folge index always 3 digits) |

Names come from source metadata. For an **album** the flat playlist is useless
(no artist; it titles itself `Album - <name>`), so `do_album` probes the first
track's tags for the real `artist`/`album` and strips any `Album -/EP -/Single -`
prefix — a plain `./download <album-url>` now yields a sane `<Artist> - <Album>/`
without a first failed attempt. Still pass `--artist`/`--album` when you want a
specific attribution: the track tag credits the *performer* (e.g. Der
Traumzauberbaum tags as `Helmut Müller-Lankow`), not always the composer/name a
human expects (`Reinhard Lakomy`). For a Hörspiel you must pass `--series`,
`--number`, `--title` (source titles are unreliable) — and often `--intro`,
because the folder name may not be speakable (e.g. folder `Die drei ??? Kids`
but intro `"Die drei Fragezeichen Kids. Panik im Paradies."`). yt-dlp cannot tell
a Hörspiel from a music album, so `--audiobook` is explicit — the assistant
picks it per URL. `--sync` mirrors to the device afterwards.

```bash
./download "<song-url>"
./download "<album-url>" --artist "Rolf Zuckowski" --album "Meine Lieder"
./download "<url>" --audiobook --series "Bibi und Tina" --number 1 --title "Das Fohlen"
```

### Multi-story Hörspiel-Folge (one folder, one card, stories navigable)

Some releases are a single *Folge* bundling several self-contained stories, each
split into `… - Teil 1/2/…` parts (e.g. *Kleiner Dodo - Folge 1* = 17 tracks = 7
stories). yt-dlp sees a plain music album; a plain `./download` lands them as 17
separate part-tracks. What we actually want (decided with the user):

- **One file per _story_** — not per part, not one giant merged Folge: group the
  tracks by the text before ` - ` and merge each story's parts with
  `ffmpeg -f concat -c copy` (no re-encode), named `NN <Story>.mp3` in the
  original appearance order.
- **Folder layout `files/<Series>/<Folge N>/`** — the *Folge* folder is the card:
  it plays as one playlist and the ESPuino's prev/next buttons step between the
  stories. So `files/Kleiner Dodo/Folge 1/01 Der seltsame Kasten.mp3`, ….
- **One cover for the whole folder** at `card-covers/<Series>/<Folge N>.jpg`
  (mirrors the folder = one card), *not* one per story. `do_album` already saves
  a single folder cover — just move it to the mirrored path.
- **No spoken intro, no embedded cover** (device has no display; stories are
  self-contained), and **drop any marketing parenthetical** from names, e.g.
  `(Das Original-Hörspiel zur TV-Serie)`.

There is no `./download` flag for this shape yet — download as an album, then run
the per-story regroup+merge (ad-hoc script; folder → `<Series>/<Folge N>/`).

`./prepare <folder>` runs the Hörspiel merge standalone on any folder of tracks:
render a spoken intro with macOS `say` (system default voice — do **not** pass a
named voice, they sound bad), and concat everything with `ffmpeg -c copy` (no
re-encode; the intro is rendered to the tracks' exact sample-rate/channels so
streams join cleanly). `spoken_title()` turns separators into sentence breaks so
`say` pauses (`"A - B"` → `"A. B."`).

**Cover art.** The device has **no display**, and a large cover at the very start
of a file makes the first seconds stutter (the decoder reads the whole ID3 tag
before audio). So audio is **not** given an embedded cover by default (`--embed-cover`
opts back in). Instead the full-resolution title image is written to
`card-covers/<same path>.jpg` — for printing RFID cards. `./covers` (re)extracts
these from any already-embedded covers in `files/` (e.g. the existing library);
new downloads save theirs automatically. Sync ignores image files, so covers
never reach the device.

One card == one RFID tag == one title image, so the granularity follows what a
card plays: a Hörspiel is one merged file → one cover; a **song** is one file →
one cover; an **album** is the whole *folder* played as one playlist → a **single**
cover at `card-covers/<Artist> - <Album>.jpg` (download's `do_album` saves just
one, not one-per-track). Note `./covers`, which walks `files/` per-mp3, would
re-create per-track covers for an album — so re-extract albums with care.

## Printing RFID cards

`./cards` lays the `card-covers/` images out into printable A4 **PDF sheets**
(`print-sheets/cards-<timestamp>.pdf`). The physical workflow: print a sheet,
stick a round RFID tag on the back of each image, laminate the whole page, then
cut it into square cards.

- Cards are **6×6 cm** squares, **packed edge-to-edge** (3 per row → 3×4 = 12 per
  page by default), so each internal cut is shared between two cards. Thin grey
  **tick marks** in the page margins mark every cut line (top/bottom and
  left/right) so a ruler can be laid edge-to-edge for one straight cut per line.
- Source covers are typically 16:9 video thumbnails: a square title image (with
  the episode title baked in) centred between solid colour bars. `cards.py`
  **trims the uniform bars and centre-crops to a square** (`prepare_card` →
  `_trim_uniform_border` + `_centre_square`), reproducing the manual crop. The
  title text is inside the square, so it survives; the bars are only left/right.
- **Manifest (`status/print-history.json`, `print_state.py`).** Printing is
  device-independent (a card maps to a *file*), so there is a single manifest
  next to the sync ones — not one per device like sync. It records size+mtime per printed
  cover. This is what makes **`./cards` with no arguments print only what's new**:
  a cover counts as new if it is absent from the manifest or changed. A real run
  marks the covers it placed as printed; the next plain `./cards` picks up only
  newly-added ones.

```bash
./cards                     # NEW covers -> a fresh PDF, then mark them printed
./cards --dry-run           # list what's new; write & mark nothing
./cards "Bibi und Tina"     # only this subpath (still new-only unless --all)
./cards --all               # include already-printed covers too
./cards --no-mark           # make a PDF but leave the manifest untouched
./cards --undo              # revert the last run: its covers are "new" again
./cards --reset ["subpath"] # forget print history -> those covers are "new" again
```

**Interactive picking lives in Studio.** Hand-picking covers onto a sheet (with
thumbnails, newest first, printed ones greyed/badged, backlog shortcut, undo
with hover-spotlight) is the **Kartendruck** tab of `./studio`. It reuses
`render_pages`/`save_pdf`/`PrintState` from cards.py verbatim, so its sheets are
identical to `./cards`' and revert with the same `./cards --undo`. (A separate
`./cards-ui` wrapper existed before Studio integrated it and was removed.)

**Full pages only.** You can only laminate whole A4 sheets, so a run whose last
page is partial (e.g. 10/12 cards) prints a `NOTE:` saying how many more covers
would fill it. The intended fix: **`./cards --undo`** drops that run (its covers
count as new again *and* its auto-generated sheet in `print-sheets/` is deleted —
a custom `--out` file is left in place), then download a couple more Hörspiele
and re-run `./cards` for a full sheet. Undo is stackable (repeat to step further
back); the manifest records each run's covers with their prior state so a run is
reverted exactly, and `--reset` prunes that history too.

Other flags: `--cols N`, `--card-cm F`, `--no-marks` (no cut ticks), `--no-trim`
(skip bar trimming), `--out FILE`. Uses **Pillow**; pages render at 300 DPI.

## Funkuino Studio (`./studio`)

One localhost web app (default `http://127.0.0.1:8800`; `--port`, `--host <device>`,
`--no-browser`) covering the whole workflow, with an embedded Claude agent. Tabs:
**Bibliothek** (per-card pipeline view: Download · Cover · Sync · Druck · Karte —
the status boxes are also the actions: Sync-box uploads the unit, Druck-box
selects for printing via a floating print bar, Karte-box assigns/unassigns RFID
cards), **Agent**, **Sync** (mirror with live log; Dry-Run always previews
`--delete`), **Kartendruck** (the full card picker, integrated; sheets are
`./cards --undo`-compatible).

- **Optional features can be switched off at start**: `--no-cards` hides the
  Kartendruck tab and the Druck column, `--no-agent` hides the Agent tab and the
  Bibliothek's URL field (URL downloads run through the agent). Their
  `/api/cards/*` and `/api/agent/*` endpoints then answer 404, and `/api/state`
  reports both under `features`. `h_index` marks the hidden elements in the
  served HTML, so they do not flash up before the first state arrives. The
  flags exist on the command line only; the macOS app starts Studio without
  them.
- **A unit = one RFID card**, derived from the `files/` naming conventions
  (album folder, Hörspiel file, `Folge N` subfolder, Lieder track). A top-level
  folder containing a **`.album` marker file** counts as an album too (used for
  compilations like `Kinderweihnacht` that lack the `Artist - Album` name form);
  the marker is in `espuino.IGNORE_PATTERNS`, so it never syncs to the device.
  Pipeline status comes from the existing manifests only — sync from the newest
  `status/sync-*.json`, print from `status/print-history.json`, covers from
  `card-covers/` — Studio adds no state files of its own.
- **Library sort**: a toolbar button switches between date (groups and rows
  newest-first, the default) and A–Z: series in German phone book order
  (`de-u-co-phonebk`, ä = ae), numbers natural (Folge 2 before Folge 10),
  `Folge` subfolders before single files within a series, the loose
  "Sonstiges" group last. The choice is stored as `library_sort` in
  `config.json` (`POST /api/prefs`, read back via `/api/state` → `prefs`), not
  in localStorage: that is keyed by origin *including the port*, and the macOS
  app starts Studio on a fresh port every launch.
- **RFID assignment lives in the Bibliothek**: Studio keeps a passive, persistent
  websocket to the device (3 s `{"ping":{"ping":"ping"}}` keepalive, reconnect
  with backoff) — it hears every card placement (`{"rfidId": …}` broadcast),
  shows a sticky banner, and a click on a row's Karte-box assigns the card via
  the device's `POST /rfid` (playMode defaults per kind: song 1, Hörspiel 3,
  album/Folge 5; hover popover edits the mode). This listener also drives the
  online/offline display (connection drop ⇒ offline within ~15 s).
- **Download progress**: `download.py` writes a JSON status snapshot when
  `FUNKUINO_PROGRESS_FILE` is set (strictly inert otherwise — see
  `scripts/progress.py`); Studio sets it per agent session and streams the
  polled snapshots to the intake strip as a real per-track progress bar.
- **The agent is the Claude Agent SDK** (`claude-agent-sdk`), authenticated with
  the subscription token in `credentials.env` (`SDK_TOKEN=…`, created with
  `claude setup-token`, valid ~1 year — recreate it when sessions fail with auth
  errors). Sessions run in this repo with this CLAUDE.md loaded
  (`setting_sources=["project"]`); pasting a URL starts the guided intake flow
  (classify → download → verify naming/covers → report, questions in German).
- **Permissions** (deliberate design decision): `permission_mode="auto"` +
  guardrails. Read-only tools, the harmless commands (`funkuino download` /
  `prepare` / `covers`) and `.venv/bin/python` are allowlisted (deterministic,
  no classifier; a git-ignored `.agent-allow.json` may add machine-local
  patterns); an explicit **ask-list** (`funkuino sync`, `funkuino cards`, `rm`,
  `rmdir`, `sudo`, `git push` — via `--settings`
  JSON) always raises a browser permission card, because ask rules are
  evaluated *before* the mode; everything else is decided silently by the
  auto-mode classifier — it approves or **denies** (never asks; denials look
  like failed tool calls to the agent). `AskUserQuestion` is on the ask-list
  because auto mode routes around `can_use_tool`, which the question cards
  depend on. Any sync from the agent deliberately needs approval (it would
  bypass Studio's device lock — even a dry-run does the full recursive device
  listing) and is auto-denied while a Studio sync runs.
- **Load-bearing SDK detail** (do not "simplify" away): sessions use
  `client.connect()` with no prompt plus one `client.query()` per turn. Passing
  a finite async-generator prompt instead makes the SDK close the input stream
  and `can_use_tool` silently never fires — questions/permissions would hang.
- Server binds 127.0.0.1 only; non-GET requests and the websocket require a
  localhost `Host`/`Origin` (CSRF guard for a localhost tool).
- A trivial agent turn costs ~$0.27 of subscription quota (the claude_code
  system preset + this CLAUDE.md ride along on every session) — fine for intake
  runs, just don't treat the Agent tab as a free chat window.

**Looking at the UI without a browser**: `.venv/bin/python dev/shot.py
[bibliothek|agent|sync|karten…]` starts a Studio against a throwaway library
(seeded with one song, one Hörspiel and one album), drives headless Chrome over
CDP, and writes PNGs to `dev/shots/`. `--no-ffmpeg` hides ffmpeg from the child
so the missing-tool banner can be captured; `--keep` leaves both processes up
with the URL printed. It holds **one** browser open and clicks the tabs, rather
than calling `chrome --headless --screenshot` per view: that one-shot form
writes its file and then often fails to exit (a loop over views wedges on the
second), and it can only ever reach the landing view, since Studio's tabs are
JS with no URL routing. Needs no new dependency — websocket-client is already
there for the device websocket.

## Device HTTP API (what we use)

All file management uses the same endpoints as the device's own web UI
(`http://<host>/swagger`, spec in `REST-API.yaml` in the ESPuino source). Paths
here have **no** `/SD-Card` prefix (that prefix only exists over FTP).

- `GET  /explorer?path=<dir>` → JSON `[{name, dir?}, …]`. **No sizes.** First
  element is a filesystem marker (`{"name":"/","root":"sd"}`), skip it. A missing
  directory returns 404/501 (treat as empty).
- `POST /explorer?path=<full path incl. filename>` → upload. Body is the **raw
  file bytes**; `Content-Type: application/octet-stream` is **required** (without
  it the server returns 200 but silently drops the body). Parent dirs auto-created.
- `PUT /explorer?path=<dir>` → mkdir. `DELETE /explorer?path=<p>` → delete file or
  (recursively) dir; **stops playback first**. `PATCH /explorer?srcpath=&dstpath=`
  → rename/move.
- `GET /explorerdownload?path=<file>` → download (404 if missing).
- `GET /info` → device/software/hardware/memory JSON.
- Websocket `ws://<host>/ws`: `{"controls":{"action":<CMD>}}` triggers a control
  command (codes in the ESPuino source, `src/values.h`), e.g. 183 = restart,
  170 = play/pause. `{"ftpStatus":{"start":1}}` starts the FTP server.

## Device quirks (hard-won — do not relearn these)

- **mDNS is slow.** Each new TCP connection to `espuino.local` costs ~5 s of name
  resolution on macOS, and the server sends `Connection: close`. The client
  resolves the host to an IP **once** (`ESPuino._resolve`) → ~0.03 s per request.
  Symptom if you regress: everything takes ~5 s per call.
- **PN5180 loses a resting card briefly during playback** (observed: bursts,
  every few s to tens of s; album restarts, or short pauses with
  pauseIfRfidRemoved). NOT caused by Studio (reproduced with Studio fully off,
  Wi-Fi off, two tag types, good static range). Firmware (dev) already has the
  countermeasures: a 500 ms removal debounce ("PN5180 debounce" in the device
  web UI — raise to 1000–1500 ms if needed, restart required) and no-reset-
  while-present; if the web UI lacks that setting, the installed build predates
  the fix → OTA update first. Symptom despite debounce ⇒ dropouts >500 ms,
  most plausibly electrical (SD read bursts + amp current sagging the PN5180
  field during playback). Alternative: disable pauseIfRfidRemoved and enable
  dontAcceptRfidTwice (mutually exclusive) — kills restarts regardless of
  dropout length. Full analysis: src/RfidPn5180.cpp state machine, debounce at
  :198-206/:248-256, 100 ms command timeouts at :172-173.
  **Measured (2026-07-23, serial log, build 04f1cc1):** stable phases are
  random (4.5–66 s) but removal→re-detect is always **270–280 ms** — a
  deterministic reset→setupRF→rescan path, NOT RF recovery. Implication: the
  reader wedges (polls fail continuously) until the post-removal reset heals
  it; fix #428's no-reset-while-present prevents exactly that healing reset,
  so the debounce may never bridge the gap. Fork-patch idea: inside the
  debounce window, after ~250–300 ms of consecutive failures, do a silent
  reset()+setupRF() and keep polling — would absorb every observed event
  without declaring removal. (Old build 2b8df17 already contained #428; the
  20.7→22.7 build delta has no RFID commits — updating did not change the
  behaviour.)
  **SOLVED (2026-07-23), merged upstream as PR #444** — the fix ships in
  official `dev` prebuilts from the merge onward; devices on older builds can
  OTA a fork build (github.com/sadilek/ESPuino, branch
  `fix/pn5180-inwindow-reset`) until then. True root cause:
  after a successful read the ISO-14443 card sits SELECTed/ACTIVE and ignores
  the library's REQA polls (post-#428 nothing power-cycles it back to IDLE), so
  most polls at rest fail and only lucky field-cycles re-read; a >debounce
  unlucky streak looked like removal. Fix (commit 77bb765): poll with WUPA and
  park the card in HALT after each read (mifareHalt) → deterministic re-reads;
  plus a silent-heal fallback at the debounce boundary (commit c56ed12) that
  disambiguates wedge-vs-removal with one muted re-init sweep before ever
  declaring removal (real removal now ~0.8 s). Verified on hardware: resting
  card = silent log, no pauses/restarts; removal pauses promptly. Failed
  attempts documented in git history (v1 aadb555: extra setupRF broke the
  fast path; v2 d6d151d: healed fine but a 150 ms trigger fires on NORMAL
  sub-debounce dropout runs → constant churn). Build: PlatformIO 6.2.0a3
  (stock 6.1.19 can't resolve the pioarduino platform), env `complete`, OTA
  image `.pio/build/complete/firmware.bin`.
  **Upstream/forum state (researched 2026-07-23):** chronic issue since 2024
  (forum thread 3454, reports through 04/2026); PR #428 is the latest partial
  fix, no issue tracks persistence after it. PN5180-Library author (tueddy,
  ATrappmann/PN5180-Library#48) confirms the chip goes unresponsive after a
  corrupted response and needs an RF-field off/on — i.e. wedge-until-reset is
  real. Reported hardware factors: Neopixel ring near the reader (EMI, badly
  degrades range), feed PN5180 with 5 V not 3.3 V, lower rfidGain, module
  quality variance. Our 275 ms instrumentation is more precise than anything
  posted upstream — worth a #428 comment / forum post (draft on request).
- **Device traffic degrades playback.** Uploads and even recursive listings
  contend with active playback for Wi-Fi and SD on the ESP32 — the audio
  stutters until the traffic stops (observed live: a background sync test made
  a running Hörspiel skip repeatedly). The device cannot be asked whether it is
  playing, so never run device-touching tests or background jobs unprompted;
  assume a child may be listening and let the user time real syncs.
- **One storage operation at a time.** Upload sequentially. **Never half-read a
  streaming download** (`stream=True` + early `close()`): it wedges the SD writer
  and the next upload fails with HTTP 500. This — not FTP — was the cause of
  earlier upload failures. The client never does streaming reads.
- **No cheap remote file size.** Listing omits sizes; `HEAD /explorerdownload`
  returns a stale/constant length; a real GET means downloading the whole file.
  Hence the local manifest for change detection.
- **FTP is not useful here** and is **not used**:
  - The firmware's FTP lib (`Joe91/ESP-FTP-Server-Lib`) `LIST`/`MLSD` return only
    `.`/`..` (no file enumeration) and `SIZE` is unimplemented → can't diff.
  - It mounts storage under a virtual `/SD-Card/…` root (HTTP uses `/…`).
  - It must be re-enabled after every reboot.
  - It does **not** interfere with HTTP uploads (uploads return 200 with FTP on
    or off).
  - Enable it (if ever needed) with websocket `{"ftpStatus":{"start":1}}` — works
    any time. The control command `CMD_ENABLE_FTP_SERVER` (150) instead only works
    within 30 s of boot (a deliberate "child protection" in `src/Cmd.cpp`).
- Restarting drops the connection and takes ~10–20 s (Wi-Fi reconnect) to come
  back; FTP is off again afterwards.
- **Unicode NFC vs NFD.** macOS gives local filenames in decomposed form (NFD);
  the device stores them composed (NFC). `join_remote` normalises every remote
  path to NFC so accented names match (else every "Frühling"/"schönsten" file
  looks missing). Files uploaded to the device by *other* tools in NFD can leave
  a second, byte-distinct folder that looks identical in the web UI (we saw an
  orphan NFD "Die schönsten Herbstlieder" alongside the real NFC one). Because we
  normalise, `--delete` cannot see such NFD-orphan folders — clean them manually
  (list root, find the entry whose codepoints are decomposed, `DELETE` it).
  macOS junk (`._*`, `.DS_Store`) on the device *is* removed by `--delete`.
- The web UI's file tree caches; after cleaning up on the device, reload it or it
  shows stale duplicates.

## Python package (the wheel)

The distribution for Linux (and, without the shell wrappers, Windows). Built
with hatchling; `python -m build --wheel` produces it.

Published on PyPI as **funkuino**, so the documented install is
`pipx install funkuino` (see *Releasing* for how it gets there). The README is
the long description, which is why its links and images are absolute URLs:
relative ones resolve against the repository and break on PyPI.

- **`scripts/` installs as the package `funkuino`** (`sources = {"scripts" =
  "funkuino"}`), but **nothing imports it as a package**. The console script is
  `funkuino.cli:main`, and `cli.main` puts its own directory on `sys.path` so
  the flat `import espuino` style keeps working everywhere. That is deliberate,
  not laziness: `espuino` loaded both flat *and* as `funkuino.espuino` would be
  two module objects with two DATA_ROOTs and two sets of manifest state — and
  the sync manifest is precisely the thing that must not exist twice. It also
  means extensions (which `import espuino`) work unchanged in an install.
- **One dependency list.** `hatch_build.py` is an in-tree metadata hook that
  reads `requirements.txt`, so the wheel and the documented venv flow (and
  `mac/runtime.py`, which pours the same file into the embedded interpreter)
  cannot drift apart. An in-tree hook keeps hatchling the only build
  requirement.
- **One version**: `pyproject.toml` owns it and `mac/Makefile` derives `VERSION`
  from it, so a release cannot ship a DMG and a wheel that disagree.
- **Assets are found relative to the module**, not to the code root
  (`studio.WEB_DIR`): installed, there is no `scripts/` below the root.
- **Floor is Python 3.10** (verified by installing and running the wheel on
  3.10) — that covers Ubuntu 22.04; the macOS bundle ships 3.13.
- Not covered by the wheel: the `./sync`-style wrappers (shell scripts, checkout
  convenience) and ffmpeg, which the user installs — see the ffmpeg note under
  *Layout*.

## macOS app (`mac/`)

Studio packaged as a double-clickable app, distributed as a notarised DMG (not
the App Store: the sandbox cannot read a library the user points at anywhere,
and yt-dlp must stay updatable, which App Store rules forbid).

```
mac/Funkuino/FunkuinoApp.swift   # the whole shell: window, server child, drag, reload
mac/Makefile                     # build, sign, notarise, DMG — `make release`
mac/runtime.py                   # assembles the embedded Python runtime
mac/plist.py                     # generates Info.plist
mac/icon.py                      # renders the icon from the wordmark's wave motif
```

- **The shell holds no logic.** It starts `funkuino studio` on a free localhost
  port as a child process and shows it in a WKWebView once it answers, so the
  app can be dropped without touching the tool. Page and window are joined:
  no native title bar (it would repeat the wordmark), the header doubles as the
  title bar and reports drags via `?shell=mac` — see *Commands* in style.css.
- **Never let the server outlive the app**: the shell handles SIGTERM/SIGINT,
  and `FUNKUINO_EXIT_WITH_PARENT` makes studio.py exit when its parent
  disappears (covers a crash or a force-quit). An orphan keeps the port and the
  device websocket.
- **Embedded runtime** (`mac/runtime.py`, ~150 MB): a pinned
  python-build-standalone release, verified against the release's SHA256SUMS,
  plus `requirements.txt`. It drops the Claude Code CLI that claude-agent-sdk
  ships (245 MB — the SDK falls back to the user's own installation, which is
  the deliberate design: the agent is optional and its CLI is the user's to
  install). Kept across `make clean`; `make distclean` removes it.
- **ffmpeg is built from source** (`mac/ffmpeg.py`, ~8 MB, ffmpeg 7.1.1 +
  libmp3lame, both pinned by checksum): there is no official macOS binary, and
  Homebrew's is dynamically linked against 25 libraries (57 MB) and configured
  `--enable-gpl`. Ours is static, links only system frameworks, stays LGPL, and
  enables exactly what prepare.py and yt-dlp call — **no network support**,
  since yt-dlp downloads by itself and hands over local files. The bundle's
  copy goes first on the child's PATH, ahead of any Homebrew build. Verified
  against Homebrew's on the real pipelines (cover extract/embed/scale, concat
  with `-c copy`, the spoken intro, m4a and webm/opus decode).
- **One architecture per build.** pip has to run the interpreter it installs
  into, so the runtime is single-arch and the shell is built to match — a
  universal shell would only mean an Intel Mac launches and then fails at the
  first Python call. An Intel build is the same `make release` on an Intel Mac.
- **Signing** batches the runtime's hundreds of `.so` files into few codesign
  calls (each invocation costs a timestamp round-trip). The disk image needs
  its own signature *and* notarisation — an unsigned but notarised DMG is
  rejected with "no usable signature". Stapling retries: Apple's ticket service
  lags its own "Accepted" by minutes.
- **Testing a fresh install**: `--config-dir` / `--data-dir` (see *Code root vs.
  data root*) point a run at scratch directories. Only a quarantined copy
  exercises Gatekeeper — plain `cp` does not set the attribute, so nothing is
  checked; `xattr -w com.apple.quarantine "0083;00000000;Safari;" <app>`
  simulates a download.

## Releasing

Two artifacts, one version: `pyproject.toml` owns it and `mac/Makefile` derives
`VERSION` from it, so bumping it there is the entire version step.

1. Bump `version` in `pyproject.toml` and commit. **PyPI versions are
   immutable** — a mistake costs a new number, it can never be overwritten.
2. Build the wheel in a scratch venv holding `build` and `twine`: they are
   release tools and deliberately not in `requirements.txt`, which is the
   runtime list the app's embedded interpreter installs.
   `python -m build` → `dist/`.
3. **List the sdist before uploading**: `tar -tzf dist/*.tar.gz`. Hatchling
   matches include patterns gitignore-style, and unanchored ones once pulled the
   private overlay checked out beside this repo (`private/scripts/…`,
   `private/README.md`) into the archive — `.git/info/exclude` does not protect
   you, hatchling only reads `.gitignore`. The patterns are anchored now, but a
   publish cannot be taken back, so look at the file list every time.
4. `twine check --strict dist/*`, then `twine upload dist/*`. TestPyPI is a
   *separate* registry with its own account, and `~/.pypirc` has only `[pypi]`;
   a rehearsal there needs that set up first. Cheaper substitute, and what was
   actually done for 0.1.0: install the built wheel into a throwaway venv and
   run it before uploading.
5. The app: `make release` in `mac/` (needs `DEV_ID` and `NOTARY_PROFILE`) —
   signs, notarises, staples, and produces the signed DMG. One architecture per
   build, so an Intel DMG means the same target on an Intel Mac.
6. Attach the DMG(s) to the GitHub release. Then install the *published* wheel
   somewhere clean (`pipx install funkuino` in a container) before announcing
   it — the artifact on PyPI is what users get, not the one in `dist/`.

**Why this stays local** (decided 2026-08-03, revisit if it chafes): moving it
to GitHub Actions would mainly buy the Intel DMG, since `macos-13` runners
still exist and the repo is public so macOS minutes are free. The price is the
Developer ID certificate and an App Store Connect key living in repository
secrets, where anything the workflow runs can sign in your name — worth it only
with tag-only triggers and an environment protection rule.

## Roadmap / notes

- **Optional custom firmware:** the single most useful change would be adding a
  `size` field to the `GET /explorer` listing (`entry["size"] = file.size()` in
  `src/Web.cpp`). That would give bulk remote sizes in one fast call and let the
  mirror drop the manifest for true stateless size-based diffing. Would require
  building/flashing (or OTA) a fork.
- **Firmware fork idea: stable battery gauge (state-based coulomb counting).**
  Background (example setup): a LiFePO4 pack (3.2 V / 6000 mAh, protected;
  charge end 3.65 V), indicator sliders set to 2.9 V (one LED) / 3.3 V (all
  LEDs) / 3.0 V (warning) — correct for the chemistry. The encoder-click LED
  gauge jumps randomly (20–24 of 24 LEDs at nominally 93 %) because
  `Animation_BatteryMeasurement` (`src/Led.cpp`) takes a fresh unfiltered
  voltage reading per click and maps it linearly (`Battery_EstimateLevel`,
  `src/BatteryMeasureVoltage.cpp`): with a 0.4 V window one LED ≈ 17 mV, while
  load transients (Wi-Fi, SD, the lit ring itself) move the voltage 30–70 mV;
  on the flat LiFePO4 plateau (3.2–3.3 V covers ~20–90 % SoC) voltage says
  almost nothing anyway. The web UI's "Zeitabstand der Messung" only drives the
  cyclic warning/MQTT check (`Battery_Cyclic`), not the click gauge. Planned
  approach — voltage-anchored coulomb counting in software (how real fuel
  gauges work, minus the current sensor):
  - Per-state current table (deepsleep ~2–5 mA, idle+Wi-Fi ~70–100 mA, playback
    ~130–200 mA, plus ~5–15 mA per lit LED); integrate `mAh += I(state)·Δt` in
    `Battery_CyclicInner`; SoC = 1 − used/6000. Persist counter to NVS every
    few minutes and on shutdown (flash wear).
  - **Anchors fix drift** (voltage IS meaningful at the curve's ends): >3.4 V
    sustained ⇒ charging; back-to-plateau after ≥3.4 V (or ≥3.4 V at boot) ⇒
    reset SoC = 100 %. <3.0 V under light load ⇒ clamp ~10 % (the knee).
    Even ±20 % table error ⇒ only ~3 % SoC drift per 10 h between anchors.
  - Minimal version (80 % of the value): single average current + "hours since
    full anchor" — monotone, reproducible display. Full version can
    self-calibrate the table from counted mAh over a full→knee cycle.
  - Sanity-clamp raw readings: observed live (2026-07-23) a stuck 10.42 V
    reading that persisted until reboot — impossible for the cell AND beyond
    the measurement chain's ~4.4 V full scale (300k/100k divider, ADC_0db,
    GPIO 35 = ADC1), i.e. wedged ADC/driver state, not noise. The gauge must
    reject values outside ~2.5–3.7 V instead of displaying them.
  - New module behind the existing `Battery_EstimateLevel()` interface so LED
    ring, web UI and MQTT pick it up unchanged. During charging only "charging"
    can be shown (charge current unknown). Real per-cent accuracy would need a
    fuel-gauge chip (LC709203/MAX17048) — hardware change, out of scope.
