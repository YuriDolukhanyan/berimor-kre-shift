"""
berimor_media.py
================
Media helpers for the Telegram bot: download a YouTube clip (16:9, H.264/AAC,
fade in/out) and convert it to MP3. Headless port of berimor.py + berimor_convert.py.

All download FEATURES are preserved: 1080p-first selection, scale-to-fit inside a
16:9 canvas with black padding, symmetric fade in/out on video+audio, single
ffmpeg pass with fast input seek, cookies support.

Full quality by default (1080p, libx264 preset "medium", all cores). Everything
is env-overridable, and ffmpeg failures report the real error — a signal kill
(exit -9 = out of memory on an undersized host) is called out explicitly.

Environment overrides (all optional):
  FFMPEG_PATH        path to ffmpeg (else auto-detected)
  MAX_HEIGHT         target height, default 1080
  X264_PRESET        libx264 preset, default "medium"
  X264_CRF           quality, default "18" (lower = better/bigger)
  FFMPEG_THREADS     encoder threads, default "0" (0 = all cores)
  YT_COOKIES_B64 / YT_COOKIES / YT_COOKIES_FILE   cookies (see resolve_cookies)
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
# Configuration (env-overridable)
# ---------------------------------------------------------------------------
DEFAULT_FADE_SEC = 0.5
PAD_COLOR        = "black"
AUDIO_BITRATE    = "192k"


def _even(n: int) -> int:
    return n - (n % 2)


def _max_height() -> int:
    try:
        h = int(os.environ.get("MAX_HEIGHT", "1080"))
    except ValueError:
        h = 1080
    return max(240, min(h, 2160))


def _canvas() -> tuple[int, int]:
    h = _max_height()
    w = _even(round(h * 16 / 9))
    return w, h


def _format_selector() -> str:
    """Prefer H.264+AAC at (or below) MAX_HEIGHT so we don't fetch more than needed."""
    h = _max_height()
    return (
        f"bv*[height={h}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height={h}][ext=mp4]+ba[ext=m4a]"
        f"/bv*[height={h}]+ba"
        f"/bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
        f"/bv*[height<={h}]+ba"
        f"/b[height<={h}][ext=mp4]"
        f"/b[height<={h}]"
    )


PLAYER_CLIENTS = ["default"]


@dataclass
class ClipResult:
    mp4_path: Path
    width: int | None
    height: int | None
    title: str
    video_id: str


# ---------------------------------------------------------------------------
# ffmpeg / cookies discovery
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
    raise MediaError(
        "ffmpeg was not found. On Railway set RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg; "
        "locally put ffmpeg.exe next to the script or set FFMPEG_PATH."
    )


def resolve_cookies(explicit: str | os.PathLike | None = None) -> Path | None:
    """
    Priority: explicit path -> YT_COOKIES_FILE (path) -> YT_COOKIES_B64 (base64,
    recommended on Railway) -> YT_COOKIES (raw contents) -> ./cookies.txt.
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
# Small helpers
# ---------------------------------------------------------------------------
def mmss(total: int) -> str:
    m, s = divmod(total, 60)
    return f"{m:02d}-{s:02d}"


def safe_stem(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return (text or "clip")[:limit]


def headers_to_arg(headers: dict | None) -> str:
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def source_dimensions(info: dict) -> tuple[int, int] | None:
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
    cw, ch = _canvas()
    chain = [
        f"scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR}",
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
        "format": _format_selector(),
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}},
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    return opts


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    """Run ffmpeg, capturing stderr; raise a helpful MediaError on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode == 0:
        return
    if proc.returncode < 0:
        sig = -proc.returncode
        if sig == 9:
            raise MediaError(
                f"{what}: ffmpeg was killed (SIGKILL) — the container almost "
                "certainly ran out of memory. Try a shorter clip, set "
                "MAX_HEIGHT=720, or give the Railway service more RAM."
            )
        raise MediaError(f"{what}: ffmpeg was killed by signal {sig}.")
    tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:]) or "(no ffmpeg output)"
    raise MediaError(f"{what}: ffmpeg exit {proc.returncode}\n{tail}")


# ---------------------------------------------------------------------------
# Core download + cut
# ---------------------------------------------------------------------------
def _download_and_cut(ffmpeg: Path, info: dict, start: int, end: int,
                      fade: float, dst: Path) -> None:
    duration = end - start
    preset  = os.environ.get("X264_PRESET", "medium")
    crf     = os.environ.get("X264_CRF", "18")
    threads = os.environ.get("FFMPEG_THREADS", "0")  # 0 = ffmpeg auto (all cores)

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
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-threads", threads, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]

    _run_ffmpeg(cmd, "Cutting the clip")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def download_clip(url: str, start_sec: int, end_sec: int, out_dir: str | os.PathLike,
                  *, fade: float = DEFAULT_FADE_SEC,
                  cookies_file: str | os.PathLike | None = None,
                  ffmpeg_path: str | os.PathLike | None = None) -> ClipResult:
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

    _download_and_cut(ffmpeg, info, start_sec, end_sec, fade, mp4_path)

    if not mp4_path.exists() or mp4_path.stat().st_size == 0:
        raise MediaError("The clip file was not produced (empty output).")

    return ClipResult(mp4_path, w, h, title, video_id)


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
    _run_ffmpeg(cmd, "Converting to MP3")

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise MediaError("The MP3 file was not produced (empty output).")

    return mp3_path
