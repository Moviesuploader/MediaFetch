import asyncio
import mimetypes
import time
import urllib.request
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
) -> Path | list[Path]:
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


def _download_image(url: str, target: Path, headers: dict[str, str] | None = None) -> Path:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()

    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise DownloadError(
            f"Image exceeds the {settings.max_file_mb} MB upload limit."
        )

    content_type = response.headers.get_content_type()
    extension = mimetypes.guess_extension(content_type) or Path(url.split("?", 1)[0]).suffix
    if extension.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        extension = ".jpg"

    final_path = target.with_suffix(extension)
    final_path.write_bytes(data)
    return final_path


def _best_thumbnail(info: dict) -> dict | None:
    thumbnails = info.get("thumbnails") or []
    valid = [
        item for item in thumbnails
        if isinstance(item, dict) and item.get("url")
    ]
    if not valid and info.get("thumbnail"):
        return {"url": info["thumbnail"]}

    def score(item: dict) -> tuple[int, int, int]:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        preference = int(item.get("preference") or 0)
        return (width * height, preference, width + height)

    return max(valid, key=score, default=None)


def _image_entries(info: dict) -> list[dict]:
    entries = info.get("entries")
    if not entries:
        return [info]

    result: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        result.extend(_image_entries(entry))
    return result


def _download_images(info: dict, output_dir: str, notify: Callable[[float, str], None]) -> list[Path]:
    entries = _image_entries(info)
    images: list[Path] = []

    for index, entry in enumerate(entries, start=1):
        thumbnail = _best_thumbnail(entry)
        if not thumbnail:
            continue

        image_url = thumbnail["url"]
        headers = thumbnail.get("http_headers")
        stem = entry.get("id") or info.get("id") or f"image-{index}"
        title = entry.get("title") or info.get("title") or "media"
        safe_title = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(title))[:60]
        target = Path(output_dir) / f"{safe_title}-{stem}"
        image_path = _download_image(image_url, target, headers)
        images.append(image_path)
        notify(index / max(len(entries), 1) * 100, f"photo {index}/{len(entries)}")

    if not images:
        raise DownloadError("No downloadable photo was found in this post.")

    return images


def _has_video_format(info: dict) -> bool:
    formats = info.get("formats") or []
    return any(
        isinstance(fmt, dict) and fmt.get("vcodec") not in (None, "none")
        for fmt in formats
    )


def _download_sync(
    url: str,
    output_dir: str,
    mode: str,
    notify: Callable[[float, str], None],
) -> Path | list[Path]:
    formats = {
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
            info = ydl.extract_info(url, download=False)

            # Photo posts/carousels often have no video formats. In that case,
            # use the highest-resolution image exposed by the extractor.
            if mode != "audio" and not _has_video_format(info):
                notify(0, "fetching HD photo…")
                return _download_images(info, output_dir, notify)

            ydl.process_info(info)

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
