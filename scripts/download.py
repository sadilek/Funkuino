#!/usr/bin/env python3
"""Download audio into the local library (files/) in the ESPuino conventions.

Three kinds, matching how the library is organised:

* Single song   -> ``Lieder/<Artist> - <Title>.mp3``
* Music album   -> ``<Artist> - <Album>/<NN> <Title>.mp3``   (index as wide as
                    the track count needs, so album order is preserved)
* Hörspiel      -> ``<Series>/<NNN> - <Title>.mp3``          (all parts merged
                    into one file with a spoken "<Series>. <Title>." intro; the
                    episode index is always 3 digits — series can exceed 100)

A single video is treated as a song and a playlist as a music album, unless
``--audiobook`` is given. Names come from source metadata but any part can be
overridden — for Hörspiele you must give ``--series``, ``--title`` and
``--number`` (source titles are unreliable).

Examples:
    ./download "<song-url>"
    ./download "<album-url>" --artist "Rolf Zuckowski" --album "Meine Lieder"
    ./download "<url>" --audiobook --series "Bibi und Tina" --number 1 --title "Das Fohlen"
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

import espuino
import progress as _progress
from yt_dlp import YoutubeDL

# Opt-in intake-progress reporter (inert unless $FUNKUINO_PROGRESS_FILE is set).
# Wrappers that reuse this module's build_opts share this same object.
PROGRESS = _progress.get()

# Files yt-dlp may leave next to the .mp3 (thumbnails, sidecars, partials).
_LEFTOVER_EXTS = {
    ".webp", ".jpg", ".jpeg", ".png", ".part", ".ytdl", ".temp", ".tmp",
    ".info.json", ".description",
}


def _safe(name: str) -> str:
    """Filesystem-safe component; keeps umlauts/spaces, drops illegal chars."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name or "untitled"


def _first(info: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = info.get(k)
        if v:
            return str(v)
    return default


def _parse_browser(spec: str) -> tuple:
    """Parse yt-dlp's BROWSER[+KEYRING][:PROFILE][::CONTAINER] into the tuple
    (browser, profile, keyring, container) that the Python API expects, e.g.
    'chrome:Default' -> ('chrome', 'Default', None, None)."""
    container = None
    if "::" in spec:
        spec, container = spec.split("::", 1)
    profile = None
    if ":" in spec:
        spec, profile = spec.split(":", 1)
    keyring = None
    if "+" in spec:
        spec, keyring = spec.split("+", 1)
    return (spec, profile or None, keyring or None, container or None)


def _auth_opts(args) -> dict:
    """yt-dlp cookie options (content that needs a logged-in account)."""
    opts: dict = {}
    if getattr(args, "cookies_from_browser", None):
        opts["cookiesfrombrowser"] = _parse_browser(args.cookies_from_browser)
    if getattr(args, "cookies", None):
        opts["cookiefile"] = args.cookies
    return opts


def probe_flat(url: str, auth: dict) -> dict:
    """Cheap probe: playlist-vs-single, titles, entry count."""
    with YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True, **auth}) as ydl:
        return ydl.extract_info(url, download=False)


def probe_full(url: str, auth: dict) -> dict:
    """Full metadata for a single item (artist/track/album), no download.
    Premium-only tracks hide their formats without cookies but not their tags;
    the tags are all a probe needs, so a missing format is no error here."""
    with YoutubeDL({"quiet": True, "skip_download": True,
                    "ignore_no_formats_error": True, **auth}) as ydl:
        return ydl.extract_info(url, download=False)


def build_opts(out_dir: Path, filename_tmpl: str, quality: int, cover: bool,
               embed: bool, auth: dict) -> dict:
    postprocessors: list[dict] = [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)},
    ]
    if cover:
        postprocessors.append({"key": "FFmpegMetadata"})   # title / artist tags
        if embed:
            # A big cover at the file start makes the ESPuino stutter; embed a
            # small JPEG only when explicitly asked. The full-res cover is saved
            # separately (covers/) regardless — see _save_cover.
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
            postprocessors.append({"key": "EmbedThumbnail"})
    opts = {
        "format": "bestaudio/best",
        "outtmpl": filename_tmpl,
        "paths": {"home": str(out_dir)},
        "ignoreerrors": True,
        "writethumbnail": cover,   # keep the thumbnail so we can save covers/
        "postprocessors": postprocessors,
        # YouTube now gates many formats behind a JS "n-challenge"; this fetches
        # the official solver so yt-dlp can solve it with a JS runtime (deno).
        "remote_components": ["ejs:github"],
        "quiet": False,
        "noprogress": False,
        **auth,
    }
    if PROGRESS.active:   # opt-in live progress to $FUNKUINO_PROGRESS_FILE
        opts["progress_hooks"] = [PROGRESS.hook]
    return opts


def cleanup_leftovers(out_dir: Path, since: float) -> None:
    """Remove thumbnail/sidecar/partial files created this run so they are not
    mirrored to the device."""
    if not out_dir.is_dir():
        return
    for f in out_dir.iterdir():
        if not f.is_file() or f.suffix.lower() == ".mp3":
            continue
        if f.suffix.lower() in _LEFTOVER_EXTS and f.stat().st_mtime >= since - 1:
            f.unlink(missing_ok=True)


def _download(args, out_dir: Path, filename_tmpl: str) -> int:
    """Download into out_dir. Does NOT clean up thumbnails — the caller needs
    them to save covers first."""
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = build_opts(out_dir, filename_tmpl, args.quality,
                      not args.no_cover, args.embed_cover, _auth_opts(args))
    with YoutubeDL(opts) as ydl:
        return ydl.download([args.url])


def _sibling_image(mp3: Path) -> Path | None:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cand = mp3.with_suffix(ext)
        if cand.exists():
            return cand
    return None


def _write_cover(src: Path | None, dest: Path) -> None:
    """Write the card cover and say where it went — or that it did not, so
    whoever runs this (often the Studio agent) does not have to go looking."""
    import prepare
    if src is None:
        print("WARNING: no cover written, the download came without a "
              "thumbnail or embedded image")
    elif prepare.make_jpg(src, dest, log=print):
        print(f"cover -> {dest}")


def _save_covers(args, lib: Path, out_dir: Path, since: float) -> None:
    """Save each freshly-downloaded track's full-res cover to covers/<rel>.jpg,
    then remove the leftover thumbnails from files/."""
    import prepare
    if not args.no_cover:
        for mp3 in sorted(out_dir.glob("*.mp3")):
            if mp3.stat().st_mtime < since - 1:
                continue
            src = _sibling_image(mp3) or (mp3 if prepare._has_cover(mp3) else None)
            _write_cover(src, espuino.cover_path_for(mp3, lib))
    cleanup_leftovers(out_dir, since)


def _save_album_cover(args, lib: Path, out_dir: Path, since: float) -> None:
    """An album is a single RFID card (the whole folder plays as one playlist),
    so save ONE title image for the folder — not one per track — at
    card-covers/<Artist> - <Album>.jpg. Then drop the per-track thumbnails."""
    import prepare
    if not args.no_cover:
        src = None
        for mp3 in sorted(out_dir.glob("*.mp3")):
            src = _sibling_image(mp3) or (mp3 if prepare._has_cover(mp3) else None)
            if src:
                break
        _write_cover(src, espuino.cover_path_for(out_dir, lib))
    cleanup_leftovers(out_dir, since)


# --- the three kinds --------------------------------------------------------
def do_song(args, lib: Path) -> int:
    info = probe_full(args.url, _auth_opts(args))
    artist, title = song_names(args, info)
    out_dir = lib / (args.into or "Lieder")
    tmpl = f"{artist} - {title}".replace("%", "%%") + ".%(ext)s"
    print(f"Song -> {out_dir.name}/{artist} - {title}.mp3")
    since = time.time()
    PROGRESS.phase("download")
    PROGRESS.item(1, 1, title)
    _download(args, out_dir, tmpl)
    _save_covers(args, lib, out_dir, since)
    return 0


def _strip_album_prefix(title: str) -> str:
    """YT Music auto-generated album playlists title themselves "Album - <name>"
    (also "EP - "/"Single - "). Drop that prefix so the folder is just <name>."""
    return re.sub(r"^(?:Album|EP|Single)\s*[-–]\s*", "", title or "", flags=re.I)


def _probe_first(args, entries: list[dict]) -> dict:
    return probe_full(entries[0].get("url") or entries[0]["id"], _auth_opts(args))


def album_names(args, flat: dict, track: dict | None = None) -> tuple[str, str]:
    """(artist, album) for an album download: the flags, else the first track's
    tags (``track``, probed here if not given), else the playlist title."""
    entries = [e for e in (flat.get("entries") or []) if e]
    artist = _safe(args.artist) if args.artist else ""
    album = _safe(args.album) if args.album else ""
    # A YT Music album's flat playlist carries no artist and titles itself
    # "Album - <name>"; the real artist/album live in the per-track tags. Probe
    # the first track only when a name is still missing (both flags given = no
    # extra request). Falls back to the (prefix-stripped) playlist title.
    if (not artist or not album) and (track or entries):
        track = track or _probe_first(args, entries)
        if not artist:
            artist = _safe(_first(track, "album_artist", "artist", "creator",
                                  "uploader", "channel", default="Unknown"))
        if not album:
            album = _safe(_first(track, "album", default="")
                          or _strip_album_prefix(_first(flat, "title", default="Album")))
    if not artist:
        artist = "Unknown"
    if not album:
        album = _safe(_strip_album_prefix(_first(flat, "title", default="Album")))
    return artist, album


def song_names(args, info: dict) -> tuple[str, str]:
    """(artist, title) for a single song: the flags, else the source's tags."""
    artist = _safe(args.artist or _first(info, "artist", "creator", "uploader", "channel", default="Unknown"))
    title = _safe(args.title or _first(info, "track", "title", default="Unknown"))
    return artist, title


def do_album(args, flat: dict, lib: Path) -> int:
    entries = [e for e in (flat.get("entries") or []) if e]
    count = flat.get("playlist_count") or len(entries) or 1
    artist, album = album_names(args, flat)
    width = max(1, len(str(count)))
    out_dir = lib / f"{artist} - {album}"
    tmpl = f"%(playlist_index)0{width}d %(track,title)s.%(ext)s"
    print(f"Album -> {out_dir.name}/  ({count} tracks, index width {width})")
    since = time.time()
    PROGRESS.phase("download")
    PROGRESS.item(0, count, "")
    _download(args, out_dir, tmpl)
    _save_album_cover(args, lib, out_dir, since)
    return 0


def do_probe(args, flat: dict) -> int:
    """Print what the intake needs to classify and name a URL, as one JSON
    object: the playlist (title, entry titles and durations), the first track's
    tags, and the names a plain song/album download would pick. The Studio
    agent otherwise pieced this together from several `yt-dlp -J` pipelines."""
    import json
    entries = [e for e in (flat.get("entries") or []) if e]
    is_playlist = flat.get("_type") == "playlist"
    track = flat   # extract_flat only flattens playlists: a single is complete
    if is_playlist:
        try:
            track = _probe_first(args, entries) if entries else {}
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: could not read the first track: {exc}", file=sys.stderr)
            track = {}
    tags = {k: track.get(k) for k in ("title", "track", "artist", "album_artist",
                                       "album", "release_year", "channel",
                                       "uploader", "duration")
            if track.get(k)}
    if track.get("description"):
        tags["description"] = track["description"][:300]
    out: dict = {"url": args.url, "type": "playlist" if is_playlist else "single"}
    if is_playlist:
        durations = [e.get("duration") for e in entries]
        out.update({
            "title": flat.get("title"),
            "channel": _first(flat, "channel", "uploader") or None,
            "count": flat.get("playlist_count") or len(entries),
            "total_minutes": (round(sum(durations) / 60)
                              if entries and all(durations) else None),
            "entries": [f"{i:>3}  {_mmss(e.get('duration'))}  {e.get('title')}"
                        for i, e in enumerate(entries, 1)],
            "first_track": tags,
            "album_default": "{} - {}/".format(*album_names(args, flat, track or None)),
        })
    else:
        out.update({"track": tags,
                    "song_default": "Lieder/{} - {}.mp3".format(*song_names(args, track))})
    out = {k: v for k, v in out.items() if v is not None}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def _mmss(seconds) -> str:
    return f"{int(seconds) // 60:d}:{int(seconds) % 60:02d}" if seconds else "?:??"


def do_audiobook(args, lib: Path) -> int:
    if not (args.series and args.title and args.number is not None):
        print("ERROR: --audiobook needs --series, --title and --number.", file=sys.stderr)
        return 2
    import prepare

    folge = f"{int(args.number):03d}"
    series_dir = lib / _safe(args.series)
    work = series_dir / f".__dl_{folge}__"          # isolated from other episodes
    output = series_dir / f"{folge} - {_safe(args.title)}.mp3"
    intro = args.intro or prepare.spoken_title(f"{args.series} - {args.title}")

    print(f"Hörspiel -> {series_dir.name}/{output.name}   intro: {intro!r}")
    PROGRESS.phase("download")
    _download(args, work, "%(playlist_index)03d %(title)s.%(ext)s")

    tracks = list(work.glob("*.mp3"))
    if not tracks:
        shutil.rmtree(work, ignore_errors=True)
        try:
            series_dir.rmdir()  # only if we just created it and it's empty
        except OSError:
            pass
        print("ERROR: no tracks were downloaded — nothing to merge. If the "
              "source needs a logged-in account, retry with "
              "--cookies-from-browser <browser> (see yt-dlp docs).",
              file=sys.stderr)
        return 1

    PROGRESS.phase("merge")
    try:
        prepare.make_audiobook(
            work, title=args.title, artist=args.series, album=args.series,
            intro=intro, output=output, tts=not args.no_tts, quality=args.quality,
            cover_dest=espuino.cover_path_for(output, lib),
            embed_cover=args.embed_cover and not args.no_cover,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download audio into files/ in the ESPuino naming conventions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="Video/playlist URL (anything yt-dlp supports)")
    parser.add_argument("--probe", action="store_true",
                        help="Download nothing: print the metadata needed to classify "
                             "and name the URL (entries, first track's tags, default "
                             "names) as JSON")
    parser.add_argument("--audiobook", action="store_true",
                        help="Hörspiel mode: merge all parts into one file with a spoken intro")
    parser.add_argument("--artist", help="Artist (song/album)")
    parser.add_argument("--album", help="Album name (album mode)")
    parser.add_argument("--series", help="Series / Reihe (audiobook mode)")
    parser.add_argument("--title", help="Song title, or episode title (audiobook)")
    parser.add_argument("--number", type=int, help="Episode number / Folge (audiobook)")
    parser.add_argument("--intro", help="Audiobook: exact spoken intro text "
                        "(default: '<Series>. <Title>.')")
    parser.add_argument("--into", default="Lieder", help="Collection folder for single songs")
    parser.add_argument("--quality", type=int, default=128, help="MP3 bitrate in kbps")
    parser.add_argument("--no-cover", action="store_true",
                        help="No cover at all (no covers/ image, no tags)")
    parser.add_argument("--embed-cover", action="store_true",
                        help="Also embed a small cover in the audio (default: only save "
                             "the full-res image to covers/, since the device has no display)")
    parser.add_argument("--no-tts", action="store_true", help="Audiobook: skip the spoken intro")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="Forwarded to yt-dlp: use this browser's cookies for "
                             "content that needs a logged-in account")
    parser.add_argument("--cookies", metavar="FILE", help="Use a cookies.txt file")
    parser.add_argument("--sync", action="store_true", help="Run ./sync afterwards")
    args = parser.parse_args(argv)
    if args.probe:   # reads metadata only, so no ffmpeg needed
        try:
            flat = probe_flat(args.url, _auth_opts(args))
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not read the URL: {exc}", file=sys.stderr)
            return 1
        return do_probe(args, flat)
    # Checked before the first network call: yt-dlp needs ffmpeg to turn what it
    # fetches into tagged MP3, so without it a long download ends in nothing.
    espuino.require_ffmpeg("convert downloaded audio to MP3")

    cfg = espuino.Config.from_env()
    lib = Path(cfg.local_dir)

    global PROGRESS
    PROGRESS = _progress.get()   # pick up $FUNKUINO_PROGRESS_FILE for this run
    PROGRESS.phase("probe")
    print(f"Probing {args.url} ...")
    try:
        flat = probe_flat(args.url, _auth_opts(args))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not read the URL: {exc}", file=sys.stderr)
        return 1
    is_playlist = flat.get("_type") == "playlist"

    if args.audiobook:
        rc = do_audiobook(args, lib)
    elif is_playlist:
        rc = do_album(args, flat, lib)
    else:
        rc = do_song(args, lib)
    if rc:
        return rc
    PROGRESS.phase("done")

    if args.sync:
        print("\n--- syncing to device ---")
        import sync
        return sync.main([])
    print("\nDone. Run ./sync to upload to the device.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
