from __future__ import annotations

import asyncio
import mimetypes
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import yt_dlp

from app.core.config import settings


class DownloadError(Exception):
    """Raised when media extraction or download fails."""


ProgressCallback = Callable[[float, str], Awaitable[None]]


@dataclass(frozen=True)
class MediaInfo:
    title: str
    duration: int | None
    uploader: str | None
    thumbnail: str | None
    heights: tuple[int, ...]
    is_photo: bool
    item_count: int = 1

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }


def _extract_info_sync(url: str) -> dict:
    opts = _base_opts()
    opts["noplaylist"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Image carousels may be represented as a playlist. Only retry the
    # playlist form when the single-item extraction exposed no video formats.
    if not _has_video_format(info):
        entries = info.get("entries") or []
        if not entries or len(entries) <= 1:
            opts["noplaylist"] = False
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
    return info


def inspect_media(url: str) -> MediaInfo:
    try:
        info = _extract_info_sync(url)
    except Exception as exc:
        raise DownloadError(str(exc)) from exc

    entries = _image_entries(info)
    formats = info.get("formats") or []
    heights = sorted(
        {
            int(fmt.get("height"))
            for fmt in formats
            if isinstance(fmt, dict)
            and fmt.get("vcodec") not in (None, "none")
            and fmt.get("height")
            and int(fmt.get("height")) > 0
        },
        reverse=True,
    )
    is_photo = not _has_video_format(info) and bool(_best_thumbnail(info) or entries)
    duration = info.get("duration")
    if duration is None and entries:
        duration = next((entry.get("duration") for entry in entries if entry.get("duration")), None)

    return MediaInfo(
        title=str(info.get("title") or "Media"),
        duration=int(duration) if duration else None,
        uploader=info.get("uploader") or info.get("channel"),
        thumbnail=(info.get("thumbnail") or (_best_thumbnail(info) or {}).get("url")),
        heights=tuple(heights),
        is_photo=is_photo,
        item_count=min(max(len(entries), 1), settings.max_carousel_items),
    )


async def get_media_info(url: str) -> MediaInfo:
    return await asyncio.to_thread(inspect_media, url)


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
        headers={"User-Agent": "Mozilla/5.0", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        content_type = response.headers.get_content_type()

    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise DownloadError(f"Image exceeds the {settings.max_file_mb} MB upload limit.")

    extension = mimetypes.guess_extension(content_type) or Path(url.split("?", 1)[0]).suffix
    if extension.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        extension = ".jpg"

    final_path = target.with_suffix(extension)
    final_path.write_bytes(data)
    return final_path


def _best_thumbnail(info: dict) -> dict | None:
    thumbnails = info.get("thumbnails") or []
    valid = [item for item in thumbnails if isinstance(item, dict) and item.get("url")]
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
        if isinstance(entry, dict):
            result.extend(_image_entries(entry))
    return result[: settings.max_carousel_items]


def _has_video_format(info: dict) -> bool:
    formats = info.get("formats") or []
    return any(
        isinstance(fmt, dict) and fmt.get("vcodec") not in (None, "none")
        for fmt in formats
    )


def _quality_selector(mode: str) -> str:
    if mode == "best":
        return "bv*+ba/b"
    if mode == "audio":
        return "bestaudio/best"
    if mode == "photo":
        return ""
    if mode.endswith("p") and mode[:-1].isdigit():
        height = int(mode[:-1])
        return f"bv*[height<=?{height}]+ba/b[height<=?{height}]"
    raise DownloadError("Unknown download mode.")


def _download_images(
    info: dict,
    output_dir: str,
    notify: Callable[[float, str], None],
) -> list[Path]:
    entries = _image_entries(info)
    images: list[Path] = []

    for index, entry in enumerate(entries, start=1):
        thumbnail = _best_thumbnail(entry)
        if not thumbnail:
            continue

        stem = entry.get("id") or info.get("id") or f"image-{index}"
        title = entry.get("title") or info.get("title") or "media"
        safe_title = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(title)
        )[:60]
        target = Path(output_dir) / f"{safe_title}-{stem}"
        image_path = _download_image(
            thumbnail["url"],
            target,
            thumbnail.get("http_headers"),
        )
        images.append(image_path)
        notify(index / max(len(entries), 1) * 100, f"photo {index}/{len(entries)}")

    if not images:
        raise DownloadError("No downloadable photo was found in this post.")
    return images


def _download_sync(
    url: str,
    output_dir: str,
    mode: str,
    notify: Callable[[float, str], None],
) -> Path | list[Path]:
    selector = _quality_selector(mode)
    opts = _base_opts()
    opts.update(
        {
            "outtmpl": str(Path(output_dir) / "%(title).80s-%(id)s.%(ext)s"),
            "format": selector or "best",
            "noplaylist": True,
            "merge_output_format": "mp4",
            "max_filesize": settings.max_file_mb * 1024 * 1024,
            "progress_hooks": [],
        }
    )

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
            if now - last_update < 1.5:
                return
            last_update = now
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            speed = data.get("speed") or 0
            notify(percent, f"{percent:.0f}% • {speed / (1024 * 1024):.1f} MB/s")
        elif data.get("status") == "finished":
            notify(100, "processing media…")

    opts["progress_hooks"] = [progress_hook]
    before = set(Path(output_dir).glob("*"))

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if mode == "photo" or (mode != "audio" and not _has_video_format(info)):
                if mode != "photo":
                    opts["noplaylist"] = False
                    with yt_dlp.YoutubeDL(opts) as image_ydl:
                        info = image_ydl.extract_info(url, download=False)
                notify(0, "fetching highest-resolution photo…")
                return _download_images(info, output_dir, notify)

            ydl.process_info(info)

            expected = Path(ydl.prepare_filename(info))
            candidates = [
                expected,
                expected.with_suffix(".mp4"),
                expected.with_suffix(".mp3"),
                expected.with_suffix(".m4a"),
                expected.with_suffix(".webm"),
                expected.with_suffix(".mkv"),
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
