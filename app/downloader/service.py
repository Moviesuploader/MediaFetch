import asyncio
from pathlib import Path

import yt_dlp

from app.core.config import settings


class DownloadError(Exception):
    """Raised when media extraction or download fails."""


async def download_media(url: str, output_dir: str) -> Path:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(_download_sync, url, output_dir)


def _download_sync(url: str, output_dir: str) -> Path:
    opts = {
        "outtmpl": str(Path(output_dir) / "%(title).80s-%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "max_filesize": settings.max_file_mb * 1024 * 1024,
        "socket_timeout": 30,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))

            if not path.exists():
                mp4 = path.with_suffix(".mp4")
                if mp4.exists():
                    path = mp4

            if not path.exists():
                raise DownloadError("Downloaded file was not found.")

            return path
    except Exception as exc:
        raise DownloadError(str(exc)) from exc
