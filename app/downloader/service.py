from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import time
import logging
import urllib.request
import urllib.error
import http.cookiejar
from html.parser import HTMLParser
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import yt_dlp
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

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
    # (mode_height, estimated_bytes). -1 height means a best/progressive
    # format. Values come from yt-dlp filesize/filesize_approx when available.
    estimated_sizes: tuple[tuple[int, int], ...] = ()

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
        "extractor_args": {},
        "timeout": settings.extraction_timeout_seconds,
        "retries": max(1, settings.ytdlp_max_retries),
        "fragment_retries": max(1, settings.ytdlp_max_retries),
        "extractor_retries": max(1, settings.ytdlp_max_retries),
        "file_access_retries": max(1, settings.ytdlp_max_retries),
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
        "js_runtimes": {"deno": {}},
        "remote_components": {"ejs:github"},
    }


def _cookie_platforms() -> set[str]:
    return {
        item.strip().lower()
        for item in settings.ytdlp_cookie_platforms.split(",")
        if item.strip()
    }


def _materialize_cookie_jar(encoded: str, filename: str, label: str) -> Path | None:
    """Decode a base64 Netscape cookie jar on an ephemeral host."""
    encoded = "".join(encoded.split())
    if not encoded:
        return None

    cookie_file = Path(filename)
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data:
            return None

        # Accept UTF-8 BOM/leading whitespace before the standard Netscape
        # cookie-file header. Never log the cookie contents.
        text = data.decode("utf-8", "replace").lstrip("\ufeff\r\n \t")
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if first_line not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
            logger.warning("%s cookie jar has an unsupported format; ignoring it", label)
            return None

        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_bytes(data)
        try:
            cookie_file.chmod(0o600)
        except OSError:
            pass

        logger.info("%s cookie jar loaded size=%d bytes", label, cookie_file.stat().st_size)
        return cookie_file
    except (binascii.Error, ValueError, OSError, UnicodeError) as exc:
        logger.warning(
            "Failed to materialize %s cookie jar error_type=%s",
            label,
            type(exc).__name__,
        )
        return None


def _materialize_cookie_file() -> Path | None:
    return _materialize_cookie_jar(
        settings.ytdlp_cookies_b64,
        settings.ytdlp_cookies_file,
        "YouTube/general",
    )


def _materialize_instagram_cookie_file() -> Path | None:
    return _materialize_cookie_jar(
        settings.ytdlp_instagram_cookies_b64,
        settings.ytdlp_instagram_cookies_file,
        "Instagram",
    )


def _materialize_facebook_cookie_file() -> Path | None:
    return _materialize_cookie_jar(
        settings.ytdlp_facebook_cookies_b64,
        settings.ytdlp_facebook_cookies_file,
        "Facebook",
    )


def _apply_cookie_policy(opts: dict, url: str) -> dict:
    """Apply only the cookie jar explicitly configured for the URL platform."""
    platform = _platform_from_url(url)

    if platform == "instagram":
        cookie_file = _materialize_instagram_cookie_file()
        if cookie_file and cookie_file.is_file() and cookie_file.stat().st_size > 0:
            opts["cookiefile"] = str(cookie_file)
        else:
            opts.pop("cookiefile", None)
        return opts

    if platform == "facebook":
        cookie_file = _materialize_facebook_cookie_file()
        if cookie_file and cookie_file.is_file() and cookie_file.stat().st_size > 0:
            opts["cookiefile"] = str(cookie_file)
            logger.info("Facebook cookies enabled size=%d bytes", cookie_file.stat().st_size)
        else:
            opts.pop("cookiefile", None)
        return opts

    cookie_file = _materialize_cookie_file() or Path(settings.ytdlp_cookies_file)
    if (
        platform in _cookie_platforms()
        and cookie_file.is_file()
        and cookie_file.stat().st_size > 0
    ):
        opts["cookiefile"] = str(cookie_file)
        logger.info(
            "yt-dlp cookies enabled platform=%s size=%d bytes",
            platform,
            cookie_file.stat().st_size,
        )
    else:
        opts.pop("cookiefile", None)
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

        if host == "instagram.com" or host.endswith(".instagram.com"):
            query = parse_qs(parts.query, keep_blank_values=True)
            query.pop("img_index", None)
            query.pop("stkn", None)
            add_variant(raw_host, query=query)
            add_variant(raw_host, query={})
            path_parts = [part for part in path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] in {"reel", "p", "tv"}:
                embed_path = f"/{path_parts[0]}/{path_parts[1]}/embed/"
                add_variant(raw_host, new_path=embed_path, query={})

        elif host == "facebook.com" or host.endswith(".facebook.com"):
            # Facebook share links can behave differently across the desktop,
            # mobile and basic public surfaces. Try URL-shape variants only;
            # access controls are still respected.
            for fb_host in ("www.facebook.com", "m.facebook.com", "mbasic.facebook.com"):
                if raw_host != fb_host:
                    add_variant(fb_host)

        elif host in {"twitter.com", "mobile.twitter.com", "m.twitter.com", "x.com", "mobile.x.com"}:
            if raw_host != "x.com":
                add_variant("x.com")

        elif host in {"old.reddit.com", "new.reddit.com", "m.reddit.com", "reddit.com"}:
            if raw_host != "www.reddit.com":
                add_variant("www.reddit.com")

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
    platform = _platform_from_url(url)
    profiles = [_base_opts()]

    if platform in {"facebook", "instagram", "threads", "pinterest", "reddit", "x", "tiktok"}:
        generic = _base_opts()
        generic["allowed_extractors"] = ["generic"]
        profiles.append(generic)

    if platform == "reddit":
        # Do not pass a raw string as yt-dlp's Python "impersonate" option.
        # That caused AssertionError before any request was sent. The short
        # share URL is resolved separately with curl-cffi browser impersonation.
        for profile in profiles:
            profile["http_headers"] = {
                **(profile.get("http_headers") or {}),
                "Referer": "https://www.reddit.com/",
            }

    if platform == "youtube":
        for clients in (["default", "web_embedded"], ["default", "mweb"]):
            youtube_profile = _base_opts()
            youtube_profile["extractor_args"] = {
                "youtube": {"player_client": clients},
            }
            profiles.append(youtube_profile)

    if platform == "facebook":
        # Facebook serves a different response to plain Python HTTP clients
        # when authenticated cookies are present. yt-dlp's Facebook extractor
        # currently recommends browser impersonation for this path.
        # curl-cffi is installed via yt-dlp[default,curl-cffi].
        for profile in profiles:
            profile["ignore_no_formats_error"] = True
            headers = dict(profile.get("http_headers") or {})
            headers.update({
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            })
            profile["http_headers"] = headers

    if platform == "instagram":
        instagram_generic = _base_opts()
        instagram_generic["allowed_extractors"] = ["generic"]
        instagram_generic["extractor_args"] = {
            "generic": {"impersonate": "chrome"},
        }
        profiles.append(instagram_generic)

        # Instagram photo-only posts/carousels currently make yt-dlp report
        # "No video formats found". Keep extraction metadata available so the
        # dedicated image fallback can handle those posts.
        for profile in profiles:
            profile["ignore_no_formats_error"] = True

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


def _facebook_authenticated_photo_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Recover an accessible Facebook photo post using the configured session.

    This only uses the user's exported Facebook cookies and does not bypass
    Facebook audience/privacy controls.
    """
    cookie_file = _materialize_facebook_cookie_file()
    if not cookie_file or not cookie_file.is_file():
        return None

    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with opener.open(request, timeout=30) as response:
            final_url = response.geturl()
            if "/login/" in urlsplit(final_url).path or "/login.php" in urlsplit(final_url).path:
                return None
            html = response.read(12 * 1024 * 1024).decode("utf-8", "replace")

        parser = _OpenGraphParser()
        parser.feed(html)
        image_url = parser.values.get("og:image")
        video_url = (
            parser.values.get("og:video:secure_url")
            or parser.values.get("og:video:url")
            or parser.values.get("og:video")
        )
        if video_url or not image_url:
            return None

        image_url = urljoin(final_url, image_url)
        if urlsplit(image_url).scheme not in {"http", "https"}:
            return None

        post_id = next(
            (part for part in urlsplit(final_url).path.split("/") if part),
            "facebook-photo",
        )
        title = parser.values.get("og:title") or "Facebook photo"
        headers = {"User-Agent": user_agent, "Referer": final_url}
        info = {
            "id": post_id,
            "title": title,
            "webpage_url": final_url,
            "thumbnail": image_url,
            "image_url": image_url,
            "formats": [{
                "format_id": "facebook-auth-image",
                "url": image_url,
                "ext": "jpg",
                "vcodec": "none",
                "acodec": "none",
                "protocol": urlsplit(image_url).scheme,
                "http_headers": headers,
            }],
        }
        opts = _apply_cookie_policy(_base_opts(), final_url)
        logger.info("Facebook authenticated photo fallback succeeded url=%s", final_url)
        return info, final_url, opts
    except Exception as exc:
        logger.warning(
            "Facebook authenticated photo fallback failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None


def _meta_public_page_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Recover public Meta/Threads media from OpenGraph page metadata.

    This is deliberately public-page only: it does not bypass private posts,
    login gates, DRM, or audience restrictions.
    """
    platform = _platform_from_url(url)
    if platform not in {"facebook", "threads"}:
        return None

    try:
        browser_user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0 Safari/537.36"
        )
        # Public Facebook share links sometimes send a normal browser to a
        # login interstitial while still exposing OpenGraph data to link
        # preview crawlers. This does not authenticate or bypass private
        # content; it only asks for metadata Facebook makes public.
        user_agents = [browser_user_agent]
        if platform == "facebook":
            user_agents.extend(
                [
                    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                    "Facebot",
                ]
            )

        html = None
        fetch_error: Exception | None = None
        for user_agent in user_agents:
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    final_url = response.geturl()
                    # A login page is not media metadata. Try the next public
                    # preview profile instead of treating it as a valid page.
                    if platform == "facebook" and "/login/" in urlsplit(final_url).path:
                        continue
                    html = response.read(8 * 1024 * 1024).decode("utf-8", "replace")
                    break
            except Exception as exc:
                fetch_error = exc

        if html is None:
            if fetch_error:
                raise fetch_error
            return None

        parser = _OpenGraphParser()
        parser.feed(html)

        video_url = (
            parser.values.get("og:video:secure_url")
            or parser.values.get("og:video:url")
            or parser.values.get("og:video")
        )
        image_url = parser.values.get("og:image")

        # Do not turn a video thumbnail into a fake "photo" result.
        if not video_url and not image_url:
            return None

        post_id = next(
            (part for part in urlsplit(url).path.split("/") if part),
            platform,
        )
        title = parser.values.get("og:title") or (
            f"{platform} video" if video_url else f"{platform} photo"
        )

        if video_url:
            video_url = urljoin(url, video_url)
            if urlsplit(video_url).scheme not in {"http", "https"}:
                return None
            fmt = {
                "format_id": f"{platform}-og-video",
                "url": video_url,
                "ext": "mp4",
                "vcodec": "unknown",
                "acodec": "unknown",
                "protocol": urlsplit(video_url).scheme,
                "http_headers": {
                    "User-Agent": browser_user_agent,
                    "Referer": url,
                },
            }
        else:
            image_url = urljoin(url, image_url)
            if urlsplit(image_url).scheme not in {"http", "https"}:
                return None
            image_host = urlsplit(image_url).netloc.lower()
            # Facebook often exposes og:image through lookaside.fbsbx.com.
            # Resolve that redirect with the same browser-like stack, but only
            # trust it when it lands on actual image bytes / Meta image CDN.
            if platform == "facebook" and "lookaside.fbsbx.com" in image_host and curl_requests is not None:
                try:
                    resolved = curl_requests.get(
                        image_url,
                        impersonate="chrome",
                        allow_redirects=True,
                        timeout=20,
                        headers={"Referer": url, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
                    )
                    resolved_type = (resolved.headers.get("content-type") or "").lower()
                    resolved_url = str(resolved.url)
                    resolved_host = urlsplit(resolved_url).netloc.lower()
                    if resolved.status_code < 400 and resolved_type.startswith("image/") and (
                        "fbcdn.net" in resolved_host or "scontent" in resolved_host
                    ):
                        image_url = resolved_url
                        image_host = resolved_host
                        logger.info("Facebook lookaside image resolved host=%s", resolved_host)
                    else:
                        logger.warning(
                            "Facebook lookaside did not resolve to image status=%s content_type=%s final_host=%s",
                            resolved.status_code, resolved_type or "unknown", resolved_host,
                        )
                        return None
                except Exception as exc:
                    logger.warning("Facebook lookaside resolver failed error_type=%s error=%r", type(exc).__name__, exc)
                    return None
            elif platform == "facebook" and not (
                "fbcdn.net" in image_host or "scontent" in image_host
            ):
                logger.warning("Facebook OpenGraph image rejected non-CDN host=%s", image_host)
                return None
            fmt = {
                "format_id": f"{platform}-og-image",
                "url": image_url,
                "ext": "jpg",
                "vcodec": "none",
                "acodec": "none",
                "protocol": urlsplit(image_url).scheme,
                "http_headers": {
                    "User-Agent": browser_user_agent,
                    "Referer": url,
                },
            }

        for key, field in (
            ("og:video:width", "width"),
            ("og:video:height", "height"),
        ):
            value = parser.values.get(key)
            if value and value.isdigit():
                fmt[field] = int(value)

        return {
            "id": post_id,
            "title": title,
            "webpage_url": url,
            "thumbnail": image_url,
            "image_url": image_url if not video_url else None,
            "formats": [fmt],
        }, url, _base_opts()
    except Exception as exc:
        logger.warning(
            "%s public-page fallback failed error_type=%s error=%s",
            platform.capitalize(),
            type(exc).__name__,
            exc,
        )
        return None


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
        image_url = parser.values.get("og:image")
        if not video_url and not image_url:
            return None

        post_id = next(
            (part for part in urlsplit(url).path.split("/") if part),
            "instagram",
        )
        title = parser.values.get("og:title") or (
            "Instagram video" if video_url else "Instagram photo"
        )

        if video_url:
            video_url = urljoin(url, video_url)
            if urlsplit(video_url).scheme not in {"http", "https"}:
                return None
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
        else:
            image_url = urljoin(url, image_url)
            if urlsplit(image_url).scheme not in {"http", "https"}:
                return None
            fmt = {
                "format_id": "instagram-og-image",
                "url": image_url,
                "ext": "jpg",
                "vcodec": "none",
                "acodec": "none",
                "protocol": urlsplit(image_url).scheme,
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
            "image_url": image_url if not video_url else None,
            "formats": [fmt],
        }, url, _base_opts()
    except Exception as exc:
        logger.warning(
            "Instagram public-page fallback failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None


def _facebook_image_urls(info: dict) -> list[tuple[str, dict[str, str]]]:
    """Collect image candidates already exposed in Facebook/yt-dlp metadata."""
    found: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            headers = value.get("http_headers")
            safe_headers = headers if isinstance(headers, dict) else {}
            for key in ("image_url", "original_url", "thumbnail"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    if candidate not in seen:
                        seen.add(candidate)
                        found.append((candidate, safe_headers))
            for thumb in value.get("thumbnails") or []:
                if isinstance(thumb, dict):
                    candidate = thumb.get("url")
                    if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                        if candidate not in seen:
                            seen.add(candidate)
                            thumb_headers = thumb.get("http_headers")
                            found.append((
                                candidate,
                                thumb_headers if isinstance(thumb_headers, dict) else safe_headers,
                            ))
            for nested_key in ("entries", "formats", "images", "attachments"):
                nested = value.get(nested_key)
                if isinstance(nested, (dict, list, tuple)):
                    walk(nested)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(info)
    return found


def _facebook_photo_from_metadata(info: dict, url: str, opts: dict) -> tuple[dict, str, dict] | None:
    candidates = _facebook_image_urls(info)
    if not candidates:
        return None

    # Do not use Facebook page/profile URLs as image downloads. Prefer CDN
    # image URLs; thumbnail metadata can otherwise contain the post URL itself,
    # which later returns HTTP 400 when _download_image tries to fetch it.
    def image_score(item: tuple[str, dict[str, str]]) -> tuple[int, int]:
        candidate, _ = item
        parts = urlsplit(candidate)
        host = parts.netloc.lower()
        path = parts.path.lower()
        cdn = int("fbcdn.net" in host or "scontent" in host)
        image_ext = int(path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")))
        return (cdn, image_ext)

    candidates = [
        item for item in candidates
        if _platform_from_url(item[0]) != "facebook"
        and urlsplit(item[0]).scheme in {"http", "https"}
    ]
    if not candidates:
        return None
    image_url, headers = max(candidates, key=image_score)
    merged_headers = {
        "User-Agent": (_base_opts().get("http_headers") or {}).get("User-Agent", "Mozilla/5.0"),
        "Referer": info.get("webpage_url") or url,
        **headers,
    }
    photo_info = dict(info)
    photo_info["thumbnail"] = image_url
    photo_info["image_url"] = image_url
    photo_info["formats"] = [{
        "format_id": "facebook-metadata-image",
        "url": image_url,
        "ext": "jpg",
        "vcodec": "none",
        "acodec": "none",
        "protocol": urlsplit(image_url).scheme,
        "http_headers": merged_headers,
    }]
    photo_info.setdefault("title", "Facebook photo")
    logger.info("Facebook photo metadata fallback succeeded url=%s", url)
    return photo_info, url, opts


def _has_image_media(info: dict) -> bool:
    if _best_thumbnail(info) or _direct_image_url(info):
        return True
    entries = info.get("entries") or []
    return any(
        isinstance(entry, dict)
        and (_best_thumbnail(entry) or _direct_image_url(entry))
        for entry in entries
    )


def _resolve_reddit_short_url(url: str) -> str:
    """Resolve Reddit /s/ share links with a real browser-like HTTP stack."""
    parts = urlsplit(url)
    if _platform_from_url(url) != "reddit" or "/s/" not in parts.path:
        return url

    if curl_requests is not None:
        try:
            response = curl_requests.get(
                url,
                impersonate="chrome",
                allow_redirects=True,
                timeout=20,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            resolved = str(response.url)
            if response.status_code < 400 and resolved and _platform_from_url(resolved) == "reddit":
                logger.info("Resolved Reddit share URL to %s", resolved)
                return resolved
            logger.warning(
                "Reddit browser resolver returned status=%s final_host=%s",
                response.status_code,
                urlsplit(resolved).netloc,
            )
        except Exception as exc:
            logger.warning(
                "Reddit browser resolver failed error_type=%s error=%r",
                type(exc).__name__,
                exc,
            )

    # Keep a plain HTTP fallback for environments where curl-cffi is absent.
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            resolved = response.geturl()
        if resolved and _platform_from_url(resolved) == "reddit":
            logger.info("Resolved Reddit share URL to %s", resolved)
            return resolved
    except Exception as exc:
        logger.warning("Reddit plain resolver failed error_type=%s error=%r", type(exc).__name__, exc)
    return url


def _reddit_json_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Fetch public Reddit post metadata from JSON surfaces.

    This does not bypass private/quarantined/login-only content. It is a
    fallback for Reddit blocking the normal HTML/share-link surface on
    datacenter IPs.
    """
    if curl_requests is None:
        return None
    candidates = [url]
    parts = urlsplit(url)
    if "/s/" not in parts.path:
        clean = url.split("?", 1)[0].rstrip("/")
        candidates.extend([
            clean + ".json?raw_json=1",
            clean + "/.json?raw_json=1",
        ])
    # Reddit's share URL can be blocked while old.reddit sometimes exposes the
    # same public redirect/metadata surface.
    if "/s/" in parts.path:
        candidates.append(url.replace("www.reddit.com", "old.reddit.com"))

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            response = curl_requests.get(
                candidate,
                impersonate="chrome",
                allow_redirects=True,
                timeout=20,
                headers={
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.reddit.com/",
                },
            )
            final_url = str(response.url)
            if response.status_code >= 400:
                continue
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                # A successful old.reddit redirect is still useful.
                if "/s/" not in urlsplit(final_url).path and _platform_from_url(final_url) == "reddit":
                    logger.info("Reddit fallback resolved canonical URL=%s", final_url)
                    return _extract_with_fallback(final_url)
                continue
            payload = response.json()
            listing = payload[0] if isinstance(payload, list) and payload else payload
            children = (((listing or {}).get("data") or {}).get("children") or [])
            if not children:
                continue
            post = (children[0] or {}).get("data") or {}
            media_url = (
                post.get("url_overridden_by_dest")
                or post.get("url")
            )
            preview = (((post.get("preview") or {}).get("images") or [{}])[0].get("source") or {}).get("url")
            if isinstance(media_url, str):
                media_url = media_url.replace("&amp;", "&")
            if isinstance(preview, str):
                preview = preview.replace("&amp;", "&")
            is_video = bool(post.get("is_video"))
            reddit_video = (((post.get("secure_media") or {}).get("reddit_video") or {}).get("fallback_url"))
            if reddit_video:
                media_url = reddit_video
                is_video = True
            direct = media_url or preview
            if not direct or not str(direct).startswith(("http://", "https://")):
                continue
            if is_video:
                fmt = {
                    "format_id": "reddit-json-video",
                    "url": direct,
                    "ext": "mp4",
                    "vcodec": "unknown",
                    "acodec": "unknown",
                    "protocol": urlsplit(direct).scheme,
                }
                image_url = preview
            else:
                # For link/self posts, use preview only when destination isn't image media.
                image_url = direct if _looks_like_image_url(str(direct)) else preview
                if not image_url:
                    continue
                fmt = {
                    "format_id": "reddit-json-image",
                    "url": image_url,
                    "ext": "jpg",
                    "vcodec": "none",
                    "acodec": "none",
                    "protocol": urlsplit(image_url).scheme,
                }
            info = {
                "id": str(post.get("id") or "reddit"),
                "title": str(post.get("title") or "Reddit media"),
                "uploader": post.get("author"),
                "webpage_url": final_url,
                "thumbnail": preview,
                "image_url": image_url if not is_video else None,
                "formats": [fmt],
            }
            logger.info("Reddit JSON fallback succeeded id=%s video=%s", info["id"], is_video)
            return info, final_url, _base_opts()
        except Exception as exc:
            logger.warning("Reddit JSON fallback failed error_type=%s error=%r", type(exc).__name__, exc)
    return None


def _extract_with_fallback(url: str) -> tuple[dict, str, dict]:
    last_error: Exception | None = None
    if _platform_from_url(url) == "reddit":
        url = _resolve_reddit_short_url(url)

    for candidate in _url_variants(url):
        for profile in _extract_profiles(candidate):
            try:
                opts = _apply_cookie_policy(dict(profile), candidate)
                opts["noplaylist"] = True
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(candidate, download=False)

                if not _has_video_format(info):
                    entries = info.get("entries") or []
                    # Facebook photo posts are not playlists; repeating the
                    # request only adds latency and can trigger another Meta
                    # anti-bot response.
                    if _platform_from_url(candidate) != "facebook" and (not entries or len(entries) <= 1):
                        playlist_opts = dict(opts)
                        playlist_opts["noplaylist"] = False
                        with yt_dlp.YoutubeDL(playlist_opts) as ydl:
                            info = ydl.extract_info(candidate, download=False)
                        opts = playlist_opts

                if _has_video_format(info):
                    return info, candidate, opts

                candidate_platform = _platform_from_url(candidate)
                if candidate_platform == "facebook":
                    photo = _facebook_photo_from_metadata(info, candidate, opts)
                    if photo:
                        return photo

                if candidate_platform in {"facebook", "instagram", "pinterest", "threads"} and _has_image_media(info):
                    return info, candidate, opts

                raise DownloadError("Extractor returned no downloadable media formats.")
            except Exception as exc:
                last_error = exc
                profile_name = "generic" if profile.get("allowed_extractors") else "native"
                logger.warning(
                    "yt-dlp extraction attempt failed platform=%s profile=%s url=%s error=%s",
                    _platform_from_url(candidate), profile_name, candidate, f"{type(exc).__name__}: {exc!r}",
                )

    platform = _platform_from_url(url)

    if platform == "reddit":
        fallback = _reddit_json_fallback(url)
        if fallback:
            return fallback

    if platform == "instagram":
        for candidate in _url_variants(url):
            fallback = _instagram_web_fallback(candidate)
            if fallback:
                logger.info("Instagram public-page fallback succeeded url=%s", candidate)
                return fallback

    if platform == "facebook":
        for candidate in _url_variants(url):
            fallback = _facebook_authenticated_photo_fallback(candidate)
            if fallback:
                return fallback

    if platform in {"facebook", "threads"}:
        for candidate in _url_variants(url):
            fallback = _meta_public_page_fallback(candidate)
            if fallback:
                logger.info(
                    "%s public-page fallback succeeded url=%s",
                    platform.capitalize(),
                    candidate,
                )
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

    estimated: dict[int, int] = {}
    progressive_best = 0
    for fmt in formats:
        if not isinstance(fmt, dict) or fmt.get("vcodec") in (None, "none"):
            continue
        size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        if size <= 0:
            continue
        height = int(fmt.get("height") or 0)
        if height > 0:
            # Keep the smallest known file at each resolution so the user
            # limit check does not reject a quality merely because another
            # codec/format at the same resolution is larger.
            estimated[height] = min(estimated.get(height, size), size)
        if fmt.get("acodec") not in (None, "none"):
            progressive_best = max(progressive_best, size)

    estimated_sizes = tuple(sorted(
        [(height, size) for height, size in estimated.items()],
        reverse=True,
    ))
    if progressive_best:
        estimated_sizes = ((-1, progressive_best),) + estimated_sizes
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
        estimated_sizes=estimated_sizes,
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
    request_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except Exception as exc:
        logger.warning(
            "Image download failed host=%s error_type=%s error=%s",
            urlsplit(url).netloc,
            type(exc).__name__,
            exc,
        )
        raise

    if len(data) > max_file_mb * 1024 * 1024:
        raise DownloadError(f"Image exceeds the {max_file_mb} MB upload limit.")

    # Never trust a .jpg suffix alone. Meta/CDN URLs can return an HTML
    # interstitial/error page with HTTP 200; saving that as .jpg later makes
    # Telegram and Pillow fail with Image_process_failed/UnidentifiedImageError.
    image_signatures = (
        data.startswith(b"\\xff\\xd8\\xff"),
        data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"),
        data.startswith((b"GIF87a", b"GIF89a")),
        data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    )
    content_type = (content_type or "").lower()
    if not content_type.startswith("image/") or not any(image_signatures):
        sample = data[:160].lstrip().lower()
        logger.warning(
            "Rejected non-image response host=%s content_type=%s bytes=%d html_like=%s",
            urlsplit(url).netloc,
            content_type or "unknown",
            len(data),
            sample.startswith((b"<html", b"<!doctype", b"<script")),
        )
        raise DownloadError("The source returned a webpage instead of image bytes.")

    extension = mimetypes.guess_extension(content_type) or Path(url.split("?", 1)[0]).suffix
    if data.startswith(b"\\xff\\xd8\\xff"):
        extension = ".jpg"
    elif data.startswith(b"\\x89PNG"):
        extension = ".png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        extension = ".gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        extension = ".webp"
    if extension.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        extension = ".jpg"

    final_path = target.with_suffix(extension)
    final_path.write_bytes(data)
    return final_path


def _looks_like_image_url(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    parts = urlsplit(value)
    host = parts.netloc.lower()
    path = parts.path.lower()
    return (
        "fbcdn.net" in host
        or "scontent" in host
        or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))
    )


def _direct_image_url(entry: dict) -> str | None:
    # Synthetic/photo fallbacks deliberately put the actual CDN URL here.
    # Prefer it over original_url, which yt-dlp uses for the Facebook POST
    # webpage itself.
    value = entry.get("image_url")
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value

    formats = entry.get("formats") or []
    image_formats = [
        fmt for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("url")
        and fmt.get("vcodec") in (None, "none")
        and fmt.get("acodec") in (None, "none")
        and _looks_like_image_url(str(fmt.get("url")))
    ]
    if image_formats:
        best = max(
            image_formats,
            key=lambda fmt: (
                int(fmt.get("width") or 0) * int(fmt.get("height") or 0),
                int(fmt.get("preference") or 0),
            ),
        )
        value = best.get("url")
        if isinstance(value, str):
            return value

    # Only accept original_url/url when it actually looks like image media.
    # This prevents https://www.facebook.com/.../posts/... HTML from being
    # downloaded and renamed to .jpg.
    for key in ("url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and _looks_like_image_url(value):
            return value
    return None


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
        return f"bv*[height<={height}]+ba/b[height<={height}]"
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
        direct_url = _direct_image_url(entry)
        image_url = direct_url or (thumbnail or {}).get("url")
        if not image_url:
            continue

        stem = entry.get("id") or info.get("id") or f"image-{index}"
        title = entry.get("title") or info.get("title") or "media"
        safe_title = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(title)
        )[:60]
        target = Path(output_dir) / f"{safe_title}-{stem}"
        image_headers = (thumbnail or {}).get("http_headers") or entry.get("http_headers")
        if direct_url:
            for fmt in entry.get("formats") or []:
                if isinstance(fmt, dict) and fmt.get("url") == direct_url:
                    image_headers = fmt.get("http_headers") or image_headers
                    break
        image_path = _download_image(
            image_url,
            target,
            max_file_mb,
            image_headers,
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
    platform = _platform_from_url(url)
    if platform == "facebook" and mode != "audio":
        if mode == "best":
            selector = "bv+ba/b[vcodec!=none][ext=mp4]/b[vcodec!=none]"
        elif mode.endswith("p") and mode[:-1].isdigit():
            height = int(mode[:-1])
            selector = (
                f"bv[height<={height}]+ba/"
                f"b[vcodec!=none][height<={height}][ext=mp4]/"
                f"b[vcodec!=none][height<={height}]"
            )
    opts = _apply_cookie_policy(_base_opts(), url)
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
        info = None
        selected_url = url
        try:
            info, selected_url, extraction_opts = _extract_with_fallback(url)

            # A /live channel URL can resolve to an actively broadcasting
            # stream. There is no finite file to finish downloading, and a
            # live stream can grow far beyond the bot's upload/storage limit.
            # Reject it early instead of consuming Koyeb disk/network for an
            # unbounded download. Finished live videos remain downloadable.
            if info.get("is_live"):
                raise DownloadError(
                    "This YouTube link points to an active live stream. "
                    "Please send the finished video URL after the live ends."
                )

            opts.update(extraction_opts)
            # Extraction options are authoritative for cookies/client selection;
            # restore download-only options that must survive the merge.
            opts["outtmpl"] = str(Path(output_dir) / "%(title).80s-%(id)s.%(ext)s")
            opts["format"] = selector or "best"
            opts["noplaylist"] = True
            opts["merge_output_format"] = "mp4"
            opts["max_filesize"] = max_file_mb * 1024 * 1024
            opts["progress_hooks"] = [progress_hook]
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        if mode == "photo" or (mode != "audio" and not _has_video_format(info)):
            # If extraction/fallback already produced an image URL, use it
            # directly. Re-extracting Facebook photo posts just sends them
            # back through yt-dlp's video-oriented Facebook extractor.
            if mode != "photo" and not _has_image_media(info):
                opts["noplaylist"] = False
                with yt_dlp.YoutubeDL(opts) as image_ydl:
                    info = image_ydl.extract_info(selected_url, download=False)
            notify(0, "fetching highest-resolution photo…")
            return _download_images(info, output_dir, notify, max_file_mb)

        # Let yt-dlp perform the actual format selection/download from the URL.
        # Calling process_info() on metadata extracted with download=False can
        # reuse an audio-only requested format, which is why YouTube was being
        # returned as M4A even for "Best" video mode.
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([selected_url])

            expected = Path(ydl.prepare_filename(info))
            if mode == "audio":
                candidates = [
                    expected.with_suffix(".mp3"),
                    expected.with_suffix(".m4a"),
                    expected,
                ]
            else:
                candidates = [
                    expected.with_suffix(".mp4"),
                    expected.with_suffix(".mkv"),
                    expected.with_suffix(".webm"),
                    expected,
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
