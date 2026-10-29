#!/usr/bin/env python3
"""Turn a folder of audio tracks into a single "audiobook" MP3 for the ESPuino.

This is the Hörspiel workflow: many downloaded tracks are merged into one file
(so the SD stays tidy and a card maps to one linear recording), a spoken title
intro is prepended, and the album cover + ID3v2.3 tags are attached.

Used both standalone (on any folder of mp3s) and by ``download.py --audiobook``.

    ./prepare "files/Bibi und Tina - Folge 1"
    ./prepare <folder> --title "Pumuckl" --artist "Ellis Kaut" --no-tts

Design notes:
* The spoken intro uses macOS ``say`` with the system default voice (no ``-v``),
  which sounds far better than the named legacy voices.
* Merging uses ffmpeg's concat demuxer with ``-c copy`` (no re-encode, so track
  quality is untouched); the intro is rendered to the tracks' exact sample
  rate / channel layout so the streams concatenate cleanly.
* Filenames keep umlauts/spaces; the device handles them and ``sync`` normalises
  to NFC on upload.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import espuino


def spoken_title(title: str) -> str:
    """Format a title for the spoken intro so `say` pauses naturally, e.g.
    'Pumuckl - Das verkaufte Bett' -> 'Pumuckl. Das verkaufte Bett.'
    Turns spaced dashes and ':' / '|' separators into sentence breaks (but keeps
    intra-word hyphens), then ensures a trailing period."""
    text = re.sub(r"\s+[-–—]\s+|\s*[:|]\s*", ". ", title)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return f"{text}." if text else text


def _run(cmd: list[str]) -> None:
    try:
        # ffmpeg writes UTF-8 whatever the locale says; `text=True` would decode
        # it with the locale's charset and garble (or, cp1252, choke on) an
        # error message that names a file with an umlaut.
        res = subprocess.run(cmd, capture_output=True,
                             encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Reached when a caller uses these helpers as a library, past main()'s
        # up-front check — still worth an instruction rather than a bare errno.
        raise RuntimeError(f"{cmd[0]} is not installed. Install it with: "
                           f"{espuino.ffmpeg_hint()}") from None
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{res.stderr.strip()[-800:]}")


def _probe_params(mp3: Path) -> tuple[int, int]:
    """Return (sample_rate, channels) of the first audio stream."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp3)],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    vals = res.stdout.split()
    sr = int(vals[0]) if len(vals) >= 1 and vals[0].isdigit() else 44100
    ch = int(vals[1]) if len(vals) >= 2 and vals[1].isdigit() else 2
    return sr, ch


# Embedded covers sit at the very start of the file; the ESPuino must read the
# whole ID3 tag before audio, so a big cover starves the buffer and the first
# seconds stutter. Keep covers small (a few tens of KiB is plenty for any
# on-device display).
MAX_COVER_PX = 500


def _extract_cover(mp3: Path, dest: Path, max_px: int = MAX_COVER_PX) -> bool:
    """Extract an embedded cover image to ``dest`` (jpg), downscaled. False if
    the source has no embedded image."""
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3), "-an", "-frames:v", "1",
         "-vf", f"scale='min({max_px},iw)':-2", "-c:v", "mjpeg", str(dest)],
        capture_output=True,
    )
    return res.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def make_jpg(src: str | Path, dest: str | Path, max_px: int | None = None) -> bool:
    """Write a JPEG at ``dest`` from ``src`` (an image, or an audio file with an
    embedded cover). Optionally downscale to ``max_px``. Full resolution by
    default (for printing). False if ``src`` has no image."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale='min({max_px},iw)':-2"] if max_px else []
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-an", "-frames:v", "1", *vf, "-c:v", "mjpeg", str(dest)],
        capture_output=True,
    )
    return res.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _has_cover(mp3: Path) -> bool:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(mp3)],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    return "video" in res.stdout


def shrink_embedded_cover(mp3: str | Path, max_px: int = MAX_COVER_PX) -> bool:
    """Rewrite ``mp3`` in place with its embedded cover downscaled to a small
    JPEG (audio copied untouched). No-op / False if there is no cover."""
    mp3 = Path(mp3)
    if not _has_cover(mp3):
        return False
    tmp = mp3.with_name(mp3.stem + ".coverfix.mp3")
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3), "-map", "0:a", "-map", "0:v",
         "-c:a", "copy", "-c:v", "mjpeg", "-vf", f"scale='min({max_px},iw)':-2",
         "-id3v2_version", "3", "-disposition:v", "attached_pic",
         "-map_metadata", "0", str(tmp)],
        capture_output=True,
    )
    if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(mp3)
        return True
    tmp.unlink(missing_ok=True)
    return False


def _make_tts(text: str, dest_mp3: Path, sr: int, ch: int, quality: int,
              voice: str | None, log) -> bool:
    """Render a spoken intro to ``dest_mp3`` (matched params, ~0.7s trailing
    silence). Returns False if `say` is unavailable."""
    aiff = dest_mp3.with_suffix(".aiff")
    cmd = ["say", "-o", str(aiff)]
    if voice:
        cmd += ["-v", voice]
    cmd += [text]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        log(f"  (skipping spoken intro: {exc})")
        return False
    _run(["ffmpeg", "-y", "-i", str(aiff), "-af", "apad=pad_dur=0.7",
          "-ar", str(sr), "-ac", str(ch), "-c:a", "libmp3lame", "-b:a", f"{quality}k",
          str(dest_mp3)])
    aiff.unlink(missing_ok=True)
    return True


def _concat_line(path: Path) -> str:
    # ffmpeg concat list: file '...'; escape single quotes.
    return "file '%s'\n" % str(path.resolve()).replace("'", "'\\''")


def _find_cover_source(folder: Path, tracks: list[Path]) -> Path | None:
    """A source to take the album cover from: a downloaded thumbnail image in
    the folder, else the first track's embedded cover."""
    for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        imgs = sorted(folder.glob(pattern))
        if imgs:
            return imgs[0]
    if tracks and _has_cover(tracks[0]):
        return tracks[0]
    return None


def make_audiobook(folder: str | Path, title: str, artist: str | None = None,
                   album: str | None = None, tts: bool = True, quality: int = 128,
                   voice: str | None = None, intro: str | None = None,
                   output: str | Path | None = None,
                   cover_dest: str | Path | None = None, embed_cover: bool = False,
                   log=print) -> Path:
    """Merge all mp3 tracks in ``folder`` into a single file with a spoken
    intro and tags, then remove the individual tracks.

    * ``output`` is the final file path (default ``folder/<title>.mp3``).
    * ``album`` / ``artist`` fill the ID3 tags (default: ``title``).
    * ``intro`` is the exact text to speak; if omitted it is derived from
      ``title`` via :func:`spoken_title`.
    * ``cover_dest``: if set, the full-resolution title image is written there
      (for printing cards). ``embed_cover``: also embed a small cover in the
      audio (off by default — the device has no display, and a large cover at
      the file start makes playback stutter)."""
    folder = Path(folder)
    artist = artist or title
    album = album or title
    out_path = Path(output) if output else folder / f"{title}.mp3"
    tracks = sorted(p for p in folder.glob("*.mp3") if p.resolve() != out_path.resolve())
    if not tracks:
        raise RuntimeError(f"No mp3 tracks to merge in {folder}")

    log(f"Merging {len(tracks)} track(s) -> {out_path.name}")
    sr, ch = _probe_params(tracks[0])
    tmp = folder / ".prepare_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        parts: list[Path] = []
        if tts:
            intro_text = intro or spoken_title(title)
            log(f"  spoken intro: {intro_text!r}")
            intro_mp3 = tmp / "intro.mp3"
            if _make_tts(intro_text, intro_mp3, sr, ch, quality, voice, log):
                parts.append(intro_mp3)
        parts.extend(tracks)

        cover_src = _find_cover_source(folder, tracks)
        if cover_src and cover_dest:
            if make_jpg(cover_src, cover_dest):
                log(f"  cover -> {cover_dest}")
        embed = False
        cover_small = tmp / "cover.jpg"
        if embed_cover and cover_src:
            embed = _extract_cover(cover_src, cover_small)

        listf = tmp / "list.txt"
        listf.write_text("".join(_concat_line(p) for p in parts), encoding="utf-8")

        tmp_out = tmp / "out.mp3"
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf)]
        if embed:
            cmd += ["-i", str(cover_small)]
        cmd += ["-map", "0:a"]
        if embed:
            cmd += ["-map", "1:v", "-disposition:v", "attached_pic"]
        cmd += ["-c", "copy", "-id3v2_version", "3",
                "-metadata", f"album_artist={artist}",
                "-metadata", f"album={album}",
                "-metadata", f"artist={artist}",
                "-metadata", f"title={title}"]
        cmd += [str(tmp_out)]
        _run(cmd)

        for t in tracks:
            t.unlink()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_out.replace(out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    mib = out_path.stat().st_size / 1_048_576
    log(f"Done: {out_path}  ({mib:.1f} MiB)")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge a folder of mp3 tracks into one audiobook file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("folder", help="Folder containing the mp3 tracks")
    parser.add_argument("--title", help="Audiobook title (default: folder name)")
    parser.add_argument("--artist", help="Artist / author (default: title)")
    parser.add_argument("--no-tts", action="store_true", help="Skip the spoken title intro")
    parser.add_argument("--intro", help="Exact text to speak (default: derived from title)")
    parser.add_argument("--voice", help="macOS 'say' voice (default: system voice)")
    parser.add_argument("--quality", type=int, default=128, help="MP3 bitrate for the intro")
    parser.add_argument("--embed-cover", action="store_true",
                        help="Also embed a small cover in the audio (off by default)")
    args = parser.parse_args(argv)
    espuino.require_ffmpeg("merge audio tracks")

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: not a folder: {folder}", file=sys.stderr)
        return 1
    title = args.title or folder.name
    out_path = folder / f"{title}.mp3"
    try:
        cover_dest = espuino.cover_path_for(out_path, espuino.DEFAULT_LOCAL_DIR)
    except ValueError:  # folder is outside files/
        cover_dest = espuino.DEFAULT_COVERS_DIR / f"{title}.jpg"
    try:
        make_audiobook(folder, title=title, artist=args.artist,
                       tts=not args.no_tts, quality=args.quality, voice=args.voice,
                       intro=args.intro, cover_dest=cover_dest,
                       embed_cover=args.embed_cover)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
