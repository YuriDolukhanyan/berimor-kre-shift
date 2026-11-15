"""
berimor_media.py
================
Media helpers for the Telegram bot: download a YouTube clip (1080p 16:9,
H.264/AAC, fade in/out) and convert it to MP3.

Two-stage pipeline (robust against YouTube's per-client tokens):
  1. yt-dlp downloads exactly the requested [start, end) section to a temp
     file. yt-dlp sends the correct client User-Agent + PO token, so the
     googlevideo URLs don't 403. Uses Deno (JS challenge) + the EJS solver +
     the bgutil PO-token provider + the `mweb` client.
  2. ffmpeg re-encodes that LOCAL file to a 1920x1080 canvas (scale+pad, no
     stretching) with symmetric fade in/out, H.264 + AAC. No remote URLs are
     handed to ffmpeg, so there is nothing to 403.

All original FEATURES are preserved: 1080p H.264/AAC, 16:9 canvas with black
padding, fade in/out on video+audio, MP3 conversion.

Env overrides (optional):
  FFMPEG_PATH   path to ffmpeg (else auto-detected)
  DENO_PATH     path to deno   (default /home/ubuntu/.deno/bin/deno)
  X264_PRESET   libx264 preset (default "medium")
  X264_CRF      quality        (default "18")
  YT_COOKIES_B64 / YT_COOKIES / YT_COOKIES_FILE   cookies
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
import logging

log = logging.getLogger("berimor.media")


class MediaError(Exception):
    """Raised on a download/convert failure, carrying a user-friendly message."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_FADE_SEC = 0.5
CANVAS_W         = 1920
CANVAS_H         = 1080
PAD_COLOR        = "black"
AUDIO_BITRATE    = "192k"

DEFAULT_DENO_PATH = "/home/ubuntu/.deno/bin/deno"

# Prefer real 1080p H.264 + m4a AAC; fall back gracefully.
FORMAT_SELECTOR = (
    "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]"
    "/bv*[height<=1080][ext=mp4]+ba[ext=m4a]"
    "/bv*[height<=1080]+ba"
    "/b[height<=1080][ext=mp4]"
    "/b[height<=1080]"
)

# The full-video fallback is skipped for videos longer than this (seconds).
# Downloading a long video just to cut a few seconds takes too long and
# blocks other /clip requests. Override with FALLBACK_MAX_SEC.
FALLBACK_MAX_SEC = int(os.environ.get("FALLBACK_MAX_SEC", "3600"))

# YouTube player client that serves 1080p with a PO token from a server IP.
PLAYER_CLIENTS = ["mweb"]


@dataclass
class ClipResult:
    mp4_path: Path
    width: int | None
    height: int | None
    title: str
    video_id: str


# ---------------------------------------------------------------------------
# Tool / cookies discovery
# ---------------------------------------------------------------------------
def find_ffmpeg(explicit: str | os.PathLike | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("FFMPEG_PATH")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent
    candidates += [here / "ffmpeg.exe", here / "ffmpeg"]
    for c in candidates:
        if c and c.exists():
            return c
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return Path(found)
    raise MediaError("ffmpeg was not found. Install it or set FFMPEG_PATH.")


def _ytdlp_bin() -> str:
    cand = Path(sys.executable).with_name("yt-dlp")
    if cand.exists():
        return str(cand)
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise MediaError("yt-dlp executable not found. pip install -U yt-dlp")


def _deno_path() -> str | None:
    deno = os.environ.get("DENO_PATH", DEFAULT_DENO_PATH)
    if Path(deno).exists():
        return deno
    return shutil.which("deno")


def resolve_cookies(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
    env_path = os.environ.get("YT_COOKIES_FILE")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    b64 = os.environ.get("YT_COOKIES_B64")
    if b64:
        import base64
        try:
            content = base64.b64decode(b64).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise MediaError(f"YT_COOKIES_B64 is not valid base64: {e}") from e
        tmp = Path(tempfile.gettempdir()) / "berimor_cookies.txt"
        tmp.write_text(content, encoding="utf-8")
        return tmp
    content = os.environ.get("YT_COOKIES")
    if content:
        tmp = Path(tempfile.gettempdir()) / "berimor_cookies.txt"
        tmp.write_text(content, encoding="utf-8")
        return tmp
    here = Path(__file__).resolve().parent / "cookies.txt"
    if here.exists():
        return here
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def mmss(total: int) -> str:
    m, s = divmod(total, 60)
    return f"{m:02d}-{s:02d}"


def hhmmss(total: int) -> str:
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_stem(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return (text or "clip")[:limit]


def _clamped_fade(duration: int, fade: float) -> float:
    if fade <= 0 or duration <= 0:
        return 0.0
    f = min(fade, duration / 2)
    return f if f > 0.05 else 0.0


def build_video_filter(duration: int, fade: float) -> str:
    chain = [
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR}",
        "setsar=1",
    ]
    f = _clamped_fade(duration, fade)
    if f:
        out_start = max(0.0, duration - f)
        chain.append(f"fade=t=in:st=0:d={f}")
        chain.append(f"fade=t=out:st={out_start}:d={f}")
    return ",".join(chain)


def build_audio_filter(duration: int, fade: float) -> str | None:
    f = _clamped_fade(duration, fade)
    if not f:
        return None
    out_start = max(0.0, duration - f)
    return f"afade=t=in:st=0:d={f},afade=t=out:st={out_start}:d={f}"


# ---------------------------------------------------------------------------
# Stage 1: yt-dlp downloads the media to a local file
# ---------------------------------------------------------------------------
def _explain(stderr: str) -> str:
    """Pick the lines of yt-dlp/ffmpeg output that say WHY it failed."""
    lines = [l for l in (stderr or "").strip().splitlines() if l.strip()]
    keys = ("error", "403", "429", "forbidden", "sign in", "bot", "unavailable",
            "private", "http", "timed out", "not available")
    hits = [l for l in lines if any(k in l.lower() for k in keys)]
    picked = (hits or lines)[-6:]
    return "\n".join(l[:300] for l in picked) or "unknown error"


def _ytdlp_download(url: str, cookies_file: Path | None, work_dir: Path,
                    start: int | None = None, end: int | None = None) -> tuple[Path, dict]:
    """
    Download with yt-dlp into work_dir. Returns (media_path, info).
    With start/end: only that section (fast, uses yt-dlp's ffmpeg downloader).
    Without: the whole video via yt-dlp's native downloader (slower, but it
    sends the correct headers/PO token itself, so it survives cases where the
    section download fails).
    """
    ytdlp = _ytdlp_bin()
    out_tmpl = str(work_dir / "src.%(ext)s")

    cmd = [
        ytdlp,
        "-f", FORMAT_SELECTOR,
        "--extractor-args", "youtube:player_client=" + ",".join(PLAYER_CLIENTS),
        "--remote-components", "ejs:github",
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
        "--print-json",
        "--no-simulate",
        "--no-progress",
    ]
    if start is not None and end is not None:
        cmd += ["--download-sections", f"*{hhmmss(start)}-{hhmmss(end)}",
                "--force-keyframes-at-cuts"]
    else:
        # full download: yt-dlp skips (without downloading) anything longer
        # than FALLBACK_MAX_SEC, and anything with unknown length (live streams)
        cmd += ["--match-filter", f"duration <= {FALLBACK_MAX_SEC}"]
    deno = _deno_path()
    if deno:
        cmd += ["--js-runtimes", f"deno:{deno}"]
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    cmd += [url]

    mode = "section" if start is not None else "full"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=900)
    except subprocess.TimeoutExpired as e:
        raise MediaError("Timed out while downloading from YouTube.") from e

    if proc.returncode != 0:
        # full output goes to the server log (journalctl -u berimor)
        log.warning("yt-dlp %s download failed (exit %s) for %s\n--- stderr ---\n%s",
                    mode, proc.returncode, url, proc.stderr)
        raise MediaError(f"yt-dlp could not download the clip ({mode}):\n{_explain(proc.stderr)}")

    info: dict = {}
    import json
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                info = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    produced = [p for p in sorted(work_dir.glob("src.*"))
                if p.suffix.lower() not in (".json", ".part", ".ytdl")]
    if not produced and mode == "full":
        # yt-dlp exited OK but downloaded nothing -> --match-filter skipped it
        # (we don't match on its message text: --print-json makes it quiet)
        log.warning("full download skipped by length filter for %s", url)
        raise MediaError(
            f"skipped, the video is longer than {FALLBACK_MAX_SEC // 60} minutes "
            f"(or its length is unknown). Try the same /clip again in a few minutes.")
    if not produced:
        raise MediaError("yt-dlp finished but produced no file.")
    media = next((p for p in produced if p.suffix.lower() == ".mp4"), produced[0])
    return media, info


# ---------------------------------------------------------------------------
# Stage 2: ffmpeg re-encodes the LOCAL file (canvas + fade + H.264/AAC)
# ---------------------------------------------------------------------------
def _encode_local(ffmpeg: Path, src: Path, duration: int, fade: float, dst: Path,
                  offset: int = 0) -> None:
    """offset > 0: src is the FULL video, so cut [offset, offset+duration) here."""
    preset = os.environ.get("X264_PRESET", "medium")
    crf    = os.environ.get("X264_CRF", "18")

    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    if offset:
        cmd += ["-ss", str(offset)]
    cmd += ["-i", str(src), "-t", str(duration)]
    cmd += ["-vf", build_video_filter(duration, fade)]
    af = build_audio_filter(duration, fade)
    if af:
        cmd += ["-af", af]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:]) or "(no output)"
        log.warning("ffmpeg encode failed (exit %s)\n%s", proc.returncode, proc.stderr)
        raise MediaError(f"ffmpeg encode failed (exit {proc.returncode}):\n{tail}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def download_clip(url: str, start_sec: int, end_sec: int, out_dir: str | os.PathLike,
                  *, fade: float = DEFAULT_FADE_SEC,
                  cookies_file: str | os.PathLike | None = None,
                  ffmpeg_path: str | os.PathLike | None = None) -> ClipResult:
    if end_sec <= start_sec:
        raise MediaError("End time must be after start time.")

    ffmpeg   = find_ffmpeg(ffmpeg_path)
    cookies  = resolve_cookies(cookies_file)
    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = end_sec - start_sec

    with tempfile.TemporaryDirectory(prefix="berimor_dl_") as td:
        work = Path(td)
        offset = 0
        try:
            sec_dir = work / "section"
            sec_dir.mkdir()
            src, info = _ytdlp_download(url, cookies, sec_dir, start_sec, end_sec)
        except MediaError as first:
            # Section download failed (often "ffmpeg exited with code 8" =
            # YouTube refused ffmpeg's request). Fall back to downloading the
            # whole video with yt-dlp's own downloader and cutting it locally.
            log.warning("section download failed, trying full download: %s", first)
            full_dir = work / "full"
            full_dir.mkdir()
            try:
                src, info = _ytdlp_download(url, cookies, full_dir)
            except MediaError as second:
                raise MediaError(f"{first}\n\nFull-video fallback also failed:\n{second}") from second
            offset = start_sec

        title    = safe_stem(info.get("title", "clip"))
        video_id = info.get("id", "video")
        w        = info.get("width")
        h        = info.get("height")
        mp4_path = out_dir / f"{title} [{video_id}] {mmss(start_sec)}-{mmss(end_sec)}.mp4"

        _encode_local(ffmpeg, src, duration, fade, mp4_path, offset=offset)

    if not mp4_path.exists() or mp4_path.stat().st_size == 0:
        raise MediaError("The clip file was not produced (empty output).")

    return ClipResult(mp4_path,
                      int(w) if w else None,
                      int(h) if h else None,
                      title, video_id)


def convert_to_mp3(mp4_path: str | os.PathLike, *,
                   ffmpeg_path: str | os.PathLike | None = None,
                   bitrate: str = AUDIO_BITRATE) -> Path:
    ffmpeg   = find_ffmpeg(ffmpeg_path)
    mp4_path = Path(mp4_path)
    mp3_path = mp4_path.with_name(f"{mp4_path.stem}.mp3")

    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mp4_path),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        "-map_metadata", "0",
        str(mp3_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:]) or "(no output)"
        raise MediaError(f"MP3 conversion failed (exit {proc.returncode}):\n{tail}")

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise MediaError("The MP3 file was not produced (empty output).")

    return mp3_path
