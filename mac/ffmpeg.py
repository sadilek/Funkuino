#!/usr/bin/env python3
"""Build the small, static ffmpeg that ships inside Funkuino.app.

    python3 mac/ffmpeg.py --out mac/build/ffmpeg

Why from source rather than a ready-made binary:

* There is no official macOS build. ffmpeg.org links third-party builders, and
  a signed app should not carry a binary nobody in this project has looked at.
* Homebrew's build is trustworthy but dynamically linked against 25 libraries
  (57 MB) that would have to be copied and relocated on every update — and it
  is configured --enable-gpl.
* Built here it is ~10 MB, LGPL (libmp3lame is LGPL; no GPL component is
  enabled), and this script *is* the "corresponding source" statement.

Only what this project actually calls is enabled — the tools decode the usual
download formats, encode MP3, write cover images and concatenate with the
concat demuxer. Notably there is **no network support**: yt-dlp downloads by
itself and hands ffmpeg local files, so https/HLS inside ffmpeg is dead weight
and would drag in a TLS library.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

FFMPEG_VERSION = "7.1.1"
FFMPEG_URL = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
# ffmpeg.org publishes a GPG signature but no checksum file. The digest is
# pinned here (and re-checked against the signature when gpg is available), so
# a changed tarball fails the build instead of ending up in a signed app.
FFMPEG_SHA256 = "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"

LAME_VERSION = "3.100"
LAME_URL = ("https://downloads.sourceforge.net/project/lame/lame/"
            f"{LAME_VERSION}/lame-{LAME_VERSION}.tar.gz")
LAME_SHA256 = "ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e"

# The exact surface this project uses: see prepare.py (concat demuxer, cover
# extraction and embedding, the spoken intro's apad/aresample) and download.py
# (yt-dlp's FFmpegExtractAudio → libmp3lame, FFmpegMetadata, thumbnails).
# Thumbnails arrive as WebP (YouTube) or PNG as often as JPEG; covers.py and
# prepare.make_jpg turn them into card covers.
DECODERS = ["mp3", "mp3float", "aac", "alac", "flac", "opus", "vorbis",
            "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_f32le", "pcm_u8",
            "mjpeg", "png", "webp"]
CONFIGURE = [
    "--disable-everything", "--disable-autodetect", "--disable-doc",
    "--disable-debug", "--disable-network", "--disable-shared", "--enable-static",
    "--disable-ffplay", "--enable-ffmpeg", "--enable-ffprobe",
    "--enable-libmp3lame",
    # The PNG decoder needs zlib, and --disable-autodetect stops configure from
    # finding the system's; without this line it drops png without a word.
    "--enable-zlib",
    "--enable-decoder=" + ",".join(DECODERS),
    "--enable-encoder=libmp3lame,pcm_s16le,mjpeg,png",
    "--enable-demuxer=mp3,mov,ogg,matroska,wav,aiff,flac,aac,image2,concat",
    "--enable-muxer=mp3,wav,image2,ipod",
    "--enable-parser=mpegaudio,aac,flac,opus,vorbis,png,mjpeg",
    "--enable-filter=aformat,anull,aresample,apad,atrim,aselect,asetnsamples,"
    "format,null,scale,copy",
    "--enable-protocol=file,pipe",
    "--enable-bsf=null,aac_adtstoasc,extract_extradata,mjpeg2jpeg",
    "--enable-swresample", "--enable-swscale",
]


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    print(f"  {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "funkuino-build"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def verify(path: Path, expected: str, tofu: bool) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected:
        if not tofu:
            raise SystemExit(
                f"No pinned checksum for {path.name}. Its digest is\n  {digest}\n"
                "Check it against the project's signature, then put it in this "
                "script (or re-run with --trust-on-first-use).")
        print(f"  pinning {path.name} at {digest}")
        return digest
    if digest != expected:
        raise SystemExit(f"Checksum mismatch for {path.name}:\n"
                         f"  expected {expected}\n  got      {digest}")
    print(f"  checksum ok ({digest[:16]}…)")
    return digest


def verify_signature(archive: Path, work: Path) -> None:
    """Best effort: only run if gpg and the FFmpeg release key are present."""
    if shutil.which("gpg") is None:
        print("  (gpg not installed — relying on the pinned checksum)")
        return
    signature = work / (archive.name + ".asc")
    try:
        fetch(FFMPEG_URL + ".asc", signature)
    except Exception as exc:  # noqa: BLE001
        print(f"  (signature unavailable: {exc})")
        return
    result = subprocess.run(["gpg", "--verify", str(signature), str(archive)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        print("  gpg signature ok")
    else:
        # An unknown key is not a failed signature; say which it is.
        print("  (gpg could not verify — is FFmpeg's release key in your "
              "keyring? Relying on the pinned checksum)")


def extract(archive: Path, into: Path) -> Path:
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="data")
    return next(into.iterdir())


def run(command: list[str], cwd: Path, env: dict | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(command[:4])} …")


def check_decoders(ffmpeg: Path) -> None:
    """configure drops an enabled component whose dependency is missing and
    still succeeds. That is how the first build shipped without png (and so
    could not turn PNG thumbnails into covers), so the result is checked."""
    listing = subprocess.run([str(ffmpeg), "-hide_banner", "-decoders"],
                             capture_output=True, text=True, check=True).stdout
    built = {line.split()[1] for line in listing.splitlines()
             if len(line.split()) > 1 and line.startswith(" ")}
    missing = [d for d in DECODERS if d not in built]
    if missing:
        raise SystemExit(f"configure silently dropped decoder(s): {', '.join(missing)}")
    print(f"  decoders ok ({len(DECODERS)})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Directory to place ffmpeg/ffprobe in")
    parser.add_argument("--trust-on-first-use", action="store_true",
                        help="Accept and print the ffmpeg tarball's digest when "
                             "none is pinned yet")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    work = out.parent / ".ffmpeg-build"
    prefix = work / "prefix"
    work.mkdir(parents=True, exist_ok=True)

    print("Sources…")
    lame_archive = work / f"lame-{LAME_VERSION}.tar.gz"
    fetch(LAME_URL, lame_archive)
    verify(lame_archive, LAME_SHA256, tofu=False)

    ffmpeg_archive = work / f"ffmpeg-{FFMPEG_VERSION}.tar.xz"
    fetch(FFMPEG_URL, ffmpeg_archive)
    digest = verify(ffmpeg_archive, FFMPEG_SHA256, tofu=args.trust_on_first_use)
    verify_signature(ffmpeg_archive, work)

    print("Building libmp3lame…")
    lame_src = extract(lame_archive, work / "lame")
    run(["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static",
         "--disable-frontend", "--disable-dependency-tracking"], cwd=lame_src)
    run(["make", f"-j{args.jobs}"], cwd=lame_src)
    run(["make", "install"], cwd=lame_src)

    print("Building ffmpeg…")
    ffmpeg_src = extract(ffmpeg_archive, work / "ffmpeg")
    run(["./configure", f"--prefix={prefix}",
         f"--extra-cflags=-I{prefix}/include", f"--extra-ldflags=-L{prefix}/lib",
         *CONFIGURE], cwd=ffmpeg_src)
    run(["make", f"-j{args.jobs}"], cwd=ffmpeg_src)

    check_decoders(ffmpeg_src / "ffmpeg")

    out.mkdir(parents=True, exist_ok=True)
    for tool in ("ffmpeg", "ffprobe"):
        shutil.copy2(ffmpeg_src / tool, out / tool)
        subprocess.run(["strip", "-S", str(out / tool)], check=False)
    size = sum((out / t).stat().st_size for t in ("ffmpeg", "ffprobe"))
    print(f"Built: {out}  ({size / 1e6:.0f} MB)")
    if not FFMPEG_SHA256:
        print(f"\nPin this in mac/ffmpeg.py:\n  FFMPEG_SHA256 = \"{digest}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
