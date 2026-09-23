from __future__ import annotations

import asyncio
import mimetypes
import time
import logging
import urllib.request
from html.parser import HTMLParser
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import yt_dlp

from app.core.config import settings


logger = logging.getLogger("mediafetch.downloader")


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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "retry_sleep_functions": {
            "http": "exp=1:8",
            "fragment": "exp=1:8",
            "extractor": "exp=1:8",
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Deno is already installed in the MediaFetch image. Allow yt-dlp to
        # fetch current EJS challenge components when the bundled package is
        # unavailable/outdated.
        "js_runtimes": ["deno"],
        "remote_components": ["ejs:github"],
    }

    # Optional admin-imported Netscape cookies. These are applied to every
    # yt-dlp extraction/download when the cookie file is present.
    cookie_file = Path(settings.ytdlp_cookies_file)
    if cookie_file.is_file() and cookie_file.stat().st_size > 0:
        opts["cookiefile"] = str(cookie_file)

    return opts


def _url_variants(url: str) -> list[str]:
    """Return conservative canonical/alternate URLs for supported platforms.

    These are only URL-shape fallbacks. They do not bypass authentication,
    private posts, DRM, or other access controls.
    """
    variants = [url]

    try:
        from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        raw_host = parts.netloc.lower().split(":")[0]
        host = raw_host[4:] if raw_host.startswith("www.") else raw_host
        path = parts.path or "/"

        def add_variant(new_host: str, new_path: str | None = None, query: dict | None = None) -> None:
            candidate = urlunsplit(
                (
                    parts.scheme or "https",
                    new_host,
                    new_path or path,
                    urlencode(query, doseq=True) if query is not None else parts.query,
                    "",
                )
            )
            if candidate not in variants:
                variants.append(candidate)

        # Instagram share links can contain item selectors that become stale.
        if host == "instagram.com" or host.endswith(".instagram.com"):
            query = parse_qs(parts.query, keep_blank_values=True)
            query.pop("img_index", None)
            query.pop("stkn", None)
            add_variant(raw_host, query=query)
            # Share/tracking parameters can change the HTML/API response.
            add_variant(raw_host, query={})

        # Facebook sometimes serves a different response shape from the
        # mobile host. Retry the same public URL on m.facebook.com.
        elif host == "facebook.com" or host.endswith(".facebook.com"):
            if raw_host != "m.facebook.com":
                add_variant("m.facebook.com")

        # X/Twitter has several legacy/mobile hostnames. Keep the canonical
        # x.com form as a second attempt.
        elif host in {"twitter.com", "mobile.twitter.com", "m.twitter.com", "x.com", "mobile.x.com"}:
            if raw_host != "x.com":
                add_variant("x.com")

        # Reddit's old/new/mobile frontends can return different HTML/API
        # responses. Retry through the normal www host.
        elif host in {"old.reddit.com", "new.reddit.com", "m.reddit.com", "reddit.com"}:
            if raw_host != "www.reddit.com":
                add_variant("www.reddit.com")

        # Threads has both threads.net and threads.com hostnames. Keep the
        # current canonical threads.net form as a fallback.
        elif host == "threads.com":
            add_variant("www.threads.net")

    except Exception:
        pass

    return variants


def _platform_from_url(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(".facebook.com") or host in {"facebook.com", "fb.watch"}:
        return "facebook"
    if host.endswith(".instagram.com") or host == "instagram.com":
        return "instagram"
    if host.endswith(".threads.net") or host.endswith(".threads.com"):
        return "threads"
    if host.endswith(".pinterest.com") or host in {"pinterest.com", "pin.it"}:
        return "pinterest"
    if host.endswith(".reddit.com") or host in {"reddit.com", "redd.it"}:
        return "reddit"
    if host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "x"
    if host in {"youtube.com", "youtu.be", "youtube-nocookie.com"} or host.endswith(".youtube.com"):
        return "youtube"
    if host in {"tiktok.com", "vm.tiktok.com"} or host.endswith(".tiktok.com"):
        return "tiktok"
    return "generic"


def _extract_profiles(url: str) -> list[dict]:
    """Return ordered, non-bypass extraction profiles.

    The generic profile lets yt-dlp use OpenGraph/direct-media metadata when a
    site's dedicated extractor is temporarily broken. It does not authenticate
    or bypass private/DRM access.
    """
    platform = _platform_from_url(url)
    profiles = [_base_opts()]
    if platform in {"facebook", "instagram", "threads", "pinterest", "reddit", "x", "tiktok"}:
        generic = _base_opts()
        generic["allowed_extractors"] = ["generic"]
        profiles.append(generic)

    if platform == "youtube":
        # YouTube periodically changes which logged-out player clients expose
        # downloadable formats. Retry documented client combinations.
        for clients in (["default", "web_embedded"], ["default", "mweb"]):
            youtube_profile = _base_opts()
            youtube_profile["extractor_args"] = {
                "youtube": {"player_client": clients},
            }
            profiles.append(youtube_profile)

    if platform == "instagram":
        # Instagram may require browser-like TLS fingerprints for some
        # public requests. Scope impersonation to the Instagram generic
        # fallback so other platforms keep the normal request path.
        instagram_generic = _base_opts()
        instagram_generic["allowed_extractors"] = ["generic"]
        instagram_generic["extractor_args"] = {
            "generic": {"impersonate": "chrome"},
        }
        profiles.append(instagram_generic)
    return profiles


class _OpenGraphParser(HTMLParser):
    """Extract public OpenGraph metadata without authentication."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        data = {str(key).lower(): value for key, value in attrs if value is not None}
        key = data.get("property") or data.get("name")
        content = data.get("content")
        if key and content and key.lower() in {
            "og:title",
            "og:description",
            "og:image",
            "og:video",
            "og:video:url",
            "og:video:secure_url",
            "og:video:type",
            "og:video:width",
            "og:video:height",
        }:
            self.values.setdefault(key.lower(), content.strip())


def _instagram_web_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Recover public Instagram video from page metadata when its API path breaks."""
    try:
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0 Safari/537.36"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read(6 * 1024 * 1024).decode("utf-8", "replace")
        parser = _OpenGraphParser()
        parser.feed(html)

        video_url = (
            parser.values.get("og:video:secure_url")
            or parser.values.get("og:video:url")
            or parser.values.get("og:video")
        )
        if not video_url:
            return None
        video_url = urljoin(url, video_url)
        if urlsplit(video_url).scheme not in {"http", "https"}:
            return None

        post_id = next(
            (part for part in urlsplit(url).path.split("/") if part),
            "instagram",
        )
        title = parser.values.get("og:title") or "Instagram video"
        fmt: dict = {
            "format_id": "instagram-og",
            "url": video_url,
            "ext": "mp4",
            "vcodec": "unknown",
            "acodec": "unknown",
            "protocol": urlsplit(video_url).scheme,
            "http_headers": {
                "User-Agent": user_agent,
                "Referer": url,
            },
        }
        for key, field in (("og:video:width", "width"), ("og:video:height", "height")):
            value = parser.values.get(key)
            if value and value.isdigit():
                fmt[field] = int(value)

        return {
            "id": post_id,
            "title": title,
            "webpage_url": url,
            "thumbnail": parser.values.get("og:image"),
            "formats": [fmt],
        }, url, _base_opts()
    except Exception as exc:
        logger.warning(
            "Instagram public-page fallback failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None


def _extract_with_fallback(url: str) -> tuple[dict, str, dict]:
    last_error: Exception | None = None

    for candidate in _url_variants(url):
        for profile in _extract_profiles(candidate):
            try:
                opts = dict(profile)
                opts["noplaylist"] = True
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(candidate, download=False)

                # Image carousels may be represented as a playlist. Retry the
                # playlist form when a single-item extraction exposes no video.
                if not _has_video_format(info):
                    entries = info.get("entries") or []
                    if not entries or len(entries) <= 1:
                        playlist_opts = dict(opts)
                        playlist_opts["noplaylist"] = False
                        with yt_dlp.YoutubeDL(playlist_opts) as ydl:
                            info = ydl.extract_info(candidate, download=False)
                        opts = playlist_opts

                # A thumbnail is not proof that a video is downloadable.
                # Continue through alternate player clients when an extractor
                # returns metadata but no usable video formats.
                if _has_video_format(info):
                    return info, candidate, opts

                # Image-only posts and carousels are valid non-video media.
                candidate_platform = _platform_from_url(candidate)
                if candidate_platform in {"instagram", "pinterest"} and (
                    _best_thumbnail(info) or _image_entries(info)
                ):
                    return info, candidate, opts

                raise DownloadError("Extractor returned no downloadable media formats.")
            except Exception as exc:
                last_error = exc
                profile_name = "generic" if profile.get("allowed_extractors") else "native"
                logger.warning(
                    "yt-dlp extraction attempt failed platform=%s profile=%s url=%s error=%s",
                    _platform_from_url(candidate), profile_name, candidate, exc,
                )

    if _platform_from_url(url) == "instagram":
        for candidate in _url_variants(url):
            fallback = _instagram_web_fallback(candidate)
            if fallback:
                logger.info("Instagram public-page fallback succeeded url=%s", candidate)
                return fallback

    logger.error("yt-dlp extraction failed after all fallbacks url=%s error=%s", url, last_error)
    raise last_error or DownloadError("Unable to extract media.")


def _extract_info_sync(url: str) -> dict:
    info, _, _ = _extract_with_fallback(url)
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
    max_file_mb: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path | list[Path]:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    if max_file_mb is None:
        max_file_mb = settings.max_file_mb
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
        max_file_mb,
        notify,
    )


def _download_image(url: str, target: Path, max_file_mb: int, headers: dict[str, str] | None = None) -> Path:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        content_type = response.headers.get_content_type()

    if len(data) > max_file_mb * 1024 * 1024:
        raise DownloadError(f"Image exceeds the {max_file_mb} MB upload limit.")

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
    max_file_mb: int,
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
            max_file_mb,
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
    max_file_mb: int,
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
            "max_filesize": max_file_mb * 1024 * 1024,
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
        # Use the same URL fallback strategy during the actual download.
        info = None
        selected_url = url
        try:
            info, selected_url, extraction_opts = _extract_with_fallback(url)
            # Preserve download-specific options (format/output/progress) while
            # retaining the successful extraction profile.
            opts.update(extraction_opts)
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        if mode == "photo" or (mode != "audio" and not _has_video_format(info)):
            if mode != "photo":
                opts["noplaylist"] = False
                with yt_dlp.YoutubeDL(opts) as image_ydl:
                    info = image_ydl.extract_info(selected_url, download=False)
            notify(0, "fetching highest-resolution photo…")
            return _download_images(info, output_dir, notify, max_file_mb)

        # The extractor result was obtained from the selected URL, so process
        # that exact info object instead of re-extracting the original URL.
        with yt_dlp.YoutubeDL(opts) as ydl:
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
