"""
berimor_media.py
================
Media helpers for the Telegram bot: download a YouTube clip (1080p 16:9,
H.264/AAC, fade in/out) and convert it to MP3.

This is a headless port of the two standalone scripts:
  - berimor.py          (YouTube clip downloader)
  - berimor_convert.py  (MP4 -> MP3 converter)

All the download FEATURES are preserved exactly:
  * prefer real 1080p H.264 + AAC
  * scale to fit inside a 1920x1080 canvas (no stretching) + black padding
  * symmetric fade in / out on both video and audio
  * single ffmpeg pass with fast input seek (-ss before -i)
  * cookies support (for logged-in / bot-check bypass)

The CLI parts (typer, rich) are removed because there is no console in a
bot; status is reported back to Telegram instead. Nothing functional was
dropped. These functions are synchronous and BLOCKING on purpose — the bot
runs them inside asyncio.to_thread so the event loop stays responsive.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yt_dlp


class MediaError(Exception):
    """Raised on a download/convert failure, carrying a user-friendly message."""


# ---------------------------------------------------------------------------
# Configuration (override via environment variables where noted)
# ---------------------------------------------------------------------------
DEFAULT_FADE_SEC = 0.5          # fade in / out duration in seconds
CANVAS_W         = 1920         # output width  (16:9 · 1080p)
CANVAS_H         = 1080         # output height
PAD_COLOR        = "black"
AUDIO_BITRATE    = "192k"       # MP3 bitrate

# Prefer exact 1080p H.264 / AAC. Fall back gracefully if 1080p isn't offered.
FORMAT_SELECTOR = (
    "bv*[height=1080][vcodec^=avc1]+ba[acodec^=mp4a]"
    "/bv*[height=1080][ext=mp4]+ba[ext=m4a]"
    "/bv*[height=1080]+ba"
    "/bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]"
    "/bv*[height<=1080][ext=mp4]+ba[ext=m4a]"
    "/bv*[height<=1080]+ba"
    "/b[height<=1080][ext=mp4]"
    "/b[height<=1080]"
)

# Let yt-dlp manage its default player_client list (it changes per release to
# keep up with YouTube). With the bgutil PO-token provider installed this lets
# the `web` client serve 1080p+.
PLAYER_CLIENTS = ["default"]


@dataclass
class ClipResult:
    mp4_path: Path
    width: int | None
    height: int | None
    title: str
    video_id: str


# ---------------------------------------------------------------------------
# ffmpeg / cookies discovery (works on Windows dev box AND Linux server)
# ---------------------------------------------------------------------------
def find_ffmpeg(explicit: str | os.PathLike | None = None) -> Path:
    """
    Locate ffmpeg. Priority:
      1. explicit argument
      2. FFMPEG_PATH environment variable
      3. ffmpeg(.exe) next to this file  (your Windows _script folder)
      4. ffmpeg on the system PATH        (apt-installed on the server)
    """
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
    raise MediaError(
        "ffmpeg was not found. On the server install it via the apt buildpack "
        "(Aptfile with `ffmpeg`); locally put ffmpeg.exe next to the script or "
        "set the FFMPEG_PATH environment variable."
    )


def resolve_cookies(explicit: str | os.PathLike | None = None) -> Path | None:
    """
    Find a cookies file. Priority:
      1. explicit path argument
      2. YT_COOKIES_FILE env var (a path)
      3. YT_COOKIES_B64 env var (base64 of the file — recommended on Railway,
         because it stays a single line and no newlines get mangled)
      4. YT_COOKIES env var (the raw file *contents* — written to a temp file)
      5. cookies.txt next to this file (your local setup)
    Returns None if no cookies are configured.
    """
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
# Small helpers (ported verbatim from berimor.py)
# ---------------------------------------------------------------------------
def mmss(total: int) -> str:
    m, s = divmod(total, 60)
    return f"{m:02d}-{s:02d}"


def safe_stem(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return (text or "clip")[:limit]


def headers_to_arg(headers: dict | None) -> str:
    """Serialize http headers for ffmpeg's -headers option."""
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def source_dimensions(info: dict) -> tuple[int, int] | None:
    """Best-effort (width, height) of the video stream we'll actually download."""
    fmts = info.get("requested_formats")
    if fmts:
        video_fmt = next((f for f in fmts if f.get("vcodec") not in (None, "none")), fmts[0])
        w, h = video_fmt.get("width"), video_fmt.get("height")
        if w and h:
            return int(w), int(h)
    w, h = info.get("width"), info.get("height")
    if w and h:
        return int(w), int(h)
    return None


def _clamped_fade(duration: int, fade: float) -> float:
    if fade <= 0 or duration <= 0:
        return 0.0
    f = min(fade, duration / 2)
    return f if f > 0.05 else 0.0


def build_video_filter(duration: int, fade: float) -> str:
    """Fit source inside the canvas, pad to 16:9, then optional fade in/out."""
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


class _QuietLogger:
    def debug(self, msg):   pass
    def info(self, msg):    pass
    def warning(self, msg): pass
    def error(self, msg):   pass


def _ydl_opts(cookies_file: Path | None) -> dict:
    opts: dict = {
        "format": FORMAT_SELECTOR,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}},
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    return opts


# ---------------------------------------------------------------------------
# Core download + cut (ported from berimor.py)
# ---------------------------------------------------------------------------
def _download_and_cut(ffmpeg: Path, info: dict, start: int, end: int,
                      fade: float, dst: Path) -> None:
    duration = end - start
    cmd: list[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]

    fmts = info.get("requested_formats")

    if fmts and len(fmts) >= 2:
        video_fmt = next((f for f in fmts if f.get("vcodec") not in (None, "none")), fmts[0])
        audio_fmt = next((f for f in fmts if f.get("acodec") not in (None, "none")
                          and f.get("vcodec") in (None, "none")), fmts[-1])

        v_hdrs = headers_to_arg(video_fmt.get("http_headers"))
        a_hdrs = headers_to_arg(audio_fmt.get("http_headers"))

        if v_hdrs:
            cmd += ["-headers", v_hdrs]
        cmd += ["-ss", str(start), "-i", video_fmt["url"]]

        if a_hdrs:
            cmd += ["-headers", a_hdrs]
        cmd += ["-ss", str(start), "-i", audio_fmt["url"]]

        cmd += ["-t", str(duration), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        hdrs = headers_to_arg(info.get("http_headers"))
        if hdrs:
            cmd += ["-headers", hdrs]
        cmd += ["-ss", str(start), "-i", info["url"], "-t", str(duration)]

    cmd += ["-vf", build_video_filter(duration, fade)]
    af = build_audio_filter(duration, fade)
    if af:
        cmd += ["-af", af]

    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]

    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Public API used by the bot
# ---------------------------------------------------------------------------
def download_clip(url: str, start_sec: int, end_sec: int, out_dir: str | os.PathLike,
                  *, fade: float = DEFAULT_FADE_SEC,
                  cookies_file: str | os.PathLike | None = None,
                  ffmpeg_path: str | os.PathLike | None = None) -> ClipResult:
    """
    Download [start_sec, end_sec) of `url` into `out_dir` as a 1920x1080
    H.264/AAC MP4 with fade in/out. Returns a ClipResult. Raises MediaError.
    """
    if end_sec <= start_sec:
        raise MediaError("End time must be after start time.")

    ffmpeg  = find_ffmpeg(ffmpeg_path)
    cookies = resolve_cookies(cookies_file)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    opts = _ydl_opts(cookies)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise MediaError(f"Could not read that video: {e}") from e

    dims = source_dimensions(info)
    w, h = dims if dims else (None, None)
    title    = safe_stem(info.get("title", "clip"))
    video_id = info.get("id", "video")
    mp4_path = out_dir / f"{title} [{video_id}] {mmss(start_sec)}-{mmss(end_sec)}.mp4"

    try:
        _download_and_cut(ffmpeg, info, start_sec, end_sec, fade, mp4_path)
    except subprocess.CalledProcessError as e:
        raise MediaError(f"ffmpeg failed while cutting the clip (exit {e.returncode}).") from e

    if not mp4_path.exists() or mp4_path.stat().st_size == 0:
        raise MediaError("The clip file was not produced (empty output).")

    return ClipResult(mp4_path, w, h, title, video_id)


def convert_to_mp3(mp4_path: str | os.PathLike, *,
                   ffmpeg_path: str | os.PathLike | None = None,
                   bitrate: str = AUDIO_BITRATE) -> Path:
    """
    Convert an existing MP4 to MP3 (libmp3lame, copies metadata). Returns the
    MP3 path. Raises MediaError on failure. (Ported from berimor_convert.py.)
    """
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
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise MediaError(f"MP3 conversion failed (exit {e.returncode}).") from e

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise MediaError("The MP3 file was not produced (empty output).")

    return mp3_path
