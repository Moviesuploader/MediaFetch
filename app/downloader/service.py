import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

import yt_dlp

from app.core.config import settings


class DownloadError(Exception):
    """Raised when media extraction or download fails."""


ProgressCallback = Callable[[float, str], Awaitable[None]]


async def download_media(
    url: str,
    output_dir: str,
    mode: str = "best",
    progress_callback: ProgressCallback | None = None,
) -> Path:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    def notify(percent: float, label: str) -> None:
        if progress_callback is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                progress_callback(percent, label),
                loop,
            )
        except RuntimeError:
            pass

    return await asyncio.to_thread(
        _download_sync,
        url,
        output_dir,
        mode,
        notify,
    )


def _download_sync(
    url: str,
    output_dir: str,
    mode: str,
    notify: Callable[[float, str], None],
) -> Path:
    formats = {
        # Prefer separate video/audio streams so "Best" really means the best
        # available quality. FFmpeg is present in the Docker image for merging.
        "best": "bestvideo+bestaudio/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720][ext=mp4]/best",
        "480p": "bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480][ext=mp4]/best",
        "audio": "bestaudio/best",
    }

    if mode not in formats:
        raise DownloadError("Unknown download mode.")

    opts = {
        "outtmpl": str(Path(output_dir) / "%(title).80s-%(id)s.%(ext)s"),
        "format": formats[mode],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "max_filesize": settings.max_file_mb * 1024 * 1024,
        "socket_timeout": 30,
        "progress_hooks": [],
    }

    if mode == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    last_update = 0.0

    def progress_hook(data: dict) -> None:
        nonlocal last_update
        if data.get("status") == "downloading":
            now = time.monotonic()
            if now - last_update < 2:
                return
            last_update = now

            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            speed = data.get("speed") or 0
            speed_mb = speed / (1024 * 1024) if speed else 0
            notify(percent, f"{percent:.0f}% • {speed_mb:.1f} MB/s")

        elif data.get("status") == "finished":
            notify(100, "processing media…")

    opts["progress_hooks"] = [progress_hook]

    before = set(Path(output_dir).glob("*"))

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            expected = Path(ydl.prepare_filename(info))

            candidates = [
                expected,
                expected.with_suffix(".mp4"),
                expected.with_suffix(".mp3"),
                expected.with_suffix(".m4a"),
                expected.with_suffix(".webm"),
            ]

            for candidate in candidates:
                if candidate.exists():
                    notify(100, "ready")
                    return candidate

            after = set(Path(output_dir).glob("*"))
            created = [
                p for p in after - before
                if p.is_file() and not p.name.endswith(".part")
            ]
            if created:
                result = max(created, key=lambda p: p.stat().st_mtime)
                notify(100, "ready")
                return result

            raise DownloadError("Downloaded file was not found.")
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(str(exc)) from exc
