from __future__ import annotations

import re
import json
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


def _yt_dlp_plugin_dirs() -> list[str]:
    """Return explicit parent directories containing yt-dlp plugin namespaces.

    The bgutil package is installed as a namespace package. Explicitly passing
    its parent directory avoids environment-dependent plugin discovery in
    containers and makes the loaded POT provider deterministic.
    """
    try:
        import importlib.util

        spec = importlib.util.find_spec("yt_dlp_plugins")
        locations = list(spec.submodule_search_locations or []) if spec else []
        return [str(Path(location).parent) for location in locations if Path(location).is_dir()]
    except Exception:
        return []


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

    plugin_dirs = _yt_dlp_plugin_dirs()
    if plugin_dirs:
        opts["plugin_dirs"] = plugin_dirs


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
    if host in {"threads.net", "threads.com"} or host.endswith(".threads.net") or host.endswith(".threads.com"):
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
    if platform == "threads" and _threads_proxy():
        profiles[0]["proxy"] = _threads_proxy()

    if platform in {"facebook", "instagram", "threads", "pinterest", "reddit", "x", "tiktok"}:
        generic = _base_opts()
        generic["allowed_extractors"] = ["generic"]
        if platform == "threads" and _threads_proxy():
            generic["proxy"] = _threads_proxy()
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
        # Current yt-dlp guidance recommends mweb + a PO-token provider.
        # Datacenter IPs may still require account cookies, so try the
        # configured YouTube cookie jar with mweb BEFORE clean fallbacks.
        profile_timeout = max(8, min(settings.youtube_profile_timeout_seconds, 20))
        provider_mode = settings.youtube_pot_provider_mode.strip().lower()
        cookie_opts = _apply_cookie_policy(_base_opts(), url)
        has_cookies = "cookiefile" in cookie_opts

        def add_youtube_profile(
            clients: list[str],
            *,
            cookies: bool = False,
            fetch_pot: bool = False,
            skip_webpage: bool = False,
        ) -> None:
            youtube_profile = _apply_cookie_policy(_base_opts(), url) if cookies else _base_opts()
            youtube_profile["socket_timeout"] = profile_timeout
            youtube_profile["timeout"] = profile_timeout
            youtube_args: dict[str, object] = {"player_client": clients}
            if fetch_pot:
                youtube_args["fetch_pot"] = ["always"]
                youtube_args["pot_trace"] = ["true"]

            # Skipping the initial webpage request is a useful final fallback
            # for cloud IPs that are blocked before Innertube player requests.
            if skip_webpage:
                youtube_args["player_skip"] = ["webpage"]

            youtube_profile["extractor_args"] = {"youtube": youtube_args}

            if fetch_pot and settings.youtube_pot_provider_enabled:
                if provider_mode == "script":
                    youtube_profile["extractor_args"]["youtubepot-bgutilscript"] = {
                        "server_home": settings.youtube_pot_provider_home.rstrip("/")
                    }
                elif settings.youtube_pot_provider_url:
                    youtube_profile["extractor_args"]["youtubepot-bgutilhttp"] = {
                        "base_url": settings.youtube_pot_provider_url.rstrip("/")
                    }
            profiles.append(youtube_profile)

        # 1) Recommended path: authenticated mweb + per-video POT.
        if has_cookies:
            add_youtube_profile(["mweb"], cookies=True, fetch_pot=True)

        # 2) Clean mweb + per-video POT. Useful for public videos when the
        # exported account cookie has gone stale.
        add_youtube_profile(["mweb"], fetch_pot=True)

        # 3) web_safari can expose HLS formats that currently avoid GVS POT.
        add_youtube_profile(["web_safari"], cookies=has_cookies, fetch_pot=True)

        # 4) No-POT clients. Keep these after mweb so they don't hide a
        # provider/cookie failure in the primary path.
        add_youtube_profile(["tv"], cookies=False)
        add_youtube_profile(["android_vr"], cookies=False)

        # 5) Cloud-IP fallback: skip the initial webpage request. This is
        # intentionally last because it can reduce metadata completeness.
        add_youtube_profile(["tv", "web_embedded"], skip_webpage=True)

        # Account-authenticated embedded clients remain useful for restricted
        # videos, but only add them when a real cookie jar exists.
        if has_cookies:
            add_youtube_profile(["tv_embedded"], cookies=True)
            add_youtube_profile(["web_embedded"], cookies=True)

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


def _facebook_curl_photo_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Fetch an accessible Facebook photo page with curl-cffi + exported cookies.

    urllib receives HTTP 400 for some Facebook share/story URLs while a
    browser-TLS request succeeds. This keeps the fallback fast and only uses
    the user's own authenticated session.
    """
    if curl_requests is None:
        return None
    cookie_file = _materialize_facebook_cookie_file()
    if not cookie_file or not cookie_file.is_file():
        return None

    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {cookie.name: cookie.value for cookie in jar}
        response = curl_requests.get(
            url,
            impersonate="chrome",
            allow_redirects=True,
            timeout=12,
            cookies=cookies,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        final_url = str(response.url)
        if response.status_code >= 400:
            raise DownloadError(f"Facebook browser request returned HTTP {response.status_code}.")
        if "/login" in urlsplit(final_url).path.lower():
            raise DownloadError("Facebook session was redirected to login.")

        html = response.text
        parser = _OpenGraphParser()
        parser.feed(html)
        video_url = (
            parser.values.get("og:video:secure_url")
            or parser.values.get("og:video:url")
            or parser.values.get("og:video")
        )
        image_url = parser.values.get("og:image")
        if video_url:
            logger.info("Facebook browser fallback found video metadata; leaving video handling to extractor")
            return None
        if not image_url:
            # Logged-in Facebook pages often omit OpenGraph tags but embed the
            # full-resolution CDN URL inside Relay/Comet JSON. Extract only
            # Meta CDN image URLs; never treat arbitrary page URLs as photos.
            import re
            from html import unescape
            decoded = unescape(html).replace("\\/", "/").replace("\\u0025", "%").replace("\\u0026", "&")
            raw_candidates = re.findall(
                r'https?://[^"\\\\\s<>]+',
                decoded,
                flags=re.IGNORECASE,
            )
            image_candidates: list[str] = []
            for candidate in raw_candidates:
                candidate = candidate.rstrip("),]}")
                host = urlsplit(candidate).netloc.lower()
                low = candidate.lower()
                if ("fbcdn.net" in host or "scontent" in host) and (
                    ".jpg" in low or ".jpeg" in low or ".png" in low or ".webp" in low
                ):
                    if candidate not in image_candidates:
                        image_candidates.append(candidate)
            if image_candidates:
                # Prefer CDN URLs structurally attached to this exact Facebook
                # post/story object. A Comet page contains many unrelated
                # images (avatars, recommendations, icons), so global size
                # ranking can return a perfectly valid but wrong photo.
                post_tokens = set(re.findall(r'(?<!\\d)\\d{8,25}(?!\\d)', final_url))
                post_tokens.update(re.findall(
                    r'(?i)(?:story_fbid|fbid|post_id)[^0-9]{0,40}(\\d{8,25})',
                    decoded,
                ))
                structured_candidates: list[str] = []
                for candidate in image_candidates:
                    positions = [m.start() for m in re.finditer(re.escape(candidate), decoded)]
                    for pos in positions[:4]:
                        context = decoded[max(0, pos - 2200):pos + len(candidate) + 900].lower()
                        positive = any(marker in context for marker in (
                            '"photo_image"', '"viewer_image"', '"full_image"',
                            '"preview_image"', '"image":{"uri"', '"image": {"uri"',
                            '"media":{"image"', '"media": {"image"',
                            '"__typename":"photo"', '"__typename": "photo"',
                            '"attachments"', '"subattachments"',
                        ))
                        negative = any(marker in context for marker in (
                            '"profile_picture"', '"profilepic"', '"icon_image"',
                            '"emoji"', '"sprite"', '"avatar"',
                        ))
                        token_match = any(token in context for token in post_tokens)
                        host = urlsplit(candidate).netloc.lower()
                        real_media_cdn = "scontent" in host or (
                            "fbcdn.net" in host and not host.startswith("static.")
                        )
                        if real_media_cdn and positive and not negative and (token_match or '"__typename":"photo"' in context):
                            structured_candidates.append(candidate)
                            break
                structured_candidates = list(dict.fromkeys(structured_candidates))
                if structured_candidates:
                    image_candidates = structured_candidates
                    logger.info(
                        "Facebook structured post-photo candidates=%d post_tokens=%d",
                        len(structured_candidates),
                        len(post_tokens),
                    )

                # Facebook Comet/Relay repeats the actual attachment URL close
                # to media fields (image/uri/photo_image). Rank that structural
                # context before probing generic CDN assets.
                contextual_candidates: list[str] = []
                for candidate in image_candidates:
                    pos = decoded.find(candidate)
                    if pos < 0:
                        continue
                    context = decoded[max(0, pos - 700):pos + len(candidate) + 300].lower()
                    host = urlsplit(candidate).netloc.lower()
                    is_post_cdn = "scontent" in host or ("fbcdn.net" in host and not host.startswith("static."))
                    # Only call a candidate contextual when it is on a real
                    # media CDN and sits next to attachment/photo fields.
                    if is_post_cdn and any(marker in context for marker in (
                        '"photo_image"', '"viewer_image"', '"full_image"',
                        '"preview_image"', '"image":{"uri"', '"image": {"uri"',
                        '"media":{"image"', '"media": {"image"',
                    )) and not any(marker in context for marker in (
                        '"profile_picture"', '"profilepic"', '"icon_image"',
                        '"emoji"', '"sprite"', '"avatar"',
                    )):
                        contextual_candidates.append(candidate)
                if contextual_candidates and not structured_candidates:
                    image_candidates = contextual_candidates + [
                        item for item in image_candidates if item not in contextual_candidates
                    ]
                    logger.info(
                        "Facebook post-media contextual candidates=%d total=%d",
                        len(contextual_candidates),
                        len(image_candidates),
                    )

                # Validate candidates instead of guessing by URL length.
                valid_images: list[tuple[int, int, int, str]] = []
                # Prefer candidates structurally tied to the post media. Meta
                # pages also contain avatars, reaction icons and UI sprites.
                # Dimensions encoded in CDN query params are a much stronger
                # signal than byte size alone.
                def _candidate_dims(candidate: str) -> tuple[int, int]:
                    import re
                    decoded_candidate = unescape(candidate)
                    patterns = (
                        r"(?:[?&_]|\\u0026)width(?:=|%3D)(\\d+).*?(?:[?&_]|\\u0026)height(?:=|%3D)(\\d+)",
                        r"(?:[?&_]|\\u0026)w(?:=|%3D)(\\d+).*?(?:[?&_]|\\u0026)h(?:=|%3D)(\\d+)",
                        r"_(\\d+)x(\\d+)",
                    )
                    for pattern in patterns:
                        match = re.search(pattern, decoded_candidate, re.IGNORECASE)
                        if match:
                            return int(match.group(1)), int(match.group(2))
                    return (0, 0)

                # Contextual post-media candidates are first; probe more than
                # the old 80-item cap because Comet pages can contain many UI
                # assets before the actual attachment.
                for candidate in image_candidates[:200]:
                    try:
                        probe = curl_requests.get(
                            candidate,
                            impersonate="chrome",
                            allow_redirects=True,
                            timeout=6,
                            cookies=cookies,
                            headers={
                                "Referer": final_url,
                                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                            },
                        )
                        blob = probe.content
                        ctype = (probe.headers.get("content-type") or "").lower()
                        magic = (
                            blob.startswith(bytes.fromhex("ffd8ff"))
                            or blob.startswith(bytes.fromhex("89504e470d0a1a0a"))
                            or blob.startswith((b"GIF87a", b"GIF89a"))
                            or (blob.startswith(b"RIFF") and len(blob) >= 12 and blob[8:12] == b"WEBP")
                        )
                        if probe.status_code < 400 and ctype.startswith("image/") and magic:
                            width, height = _candidate_dims(candidate)
                            # CDN URLs often omit dimensions. Read the actual
                            # image header so ranking uses real pixels instead
                            # of guessing from URL/query parameters.
                            if not width or not height:
                                try:
                                    from PIL import Image
                                    from io import BytesIO
                                    with Image.open(BytesIO(blob)) as im:
                                        width, height = im.size
                                except Exception:
                                    width, height = (0, 0)
                            area = width * height
                            # Strongly demote Facebook static/UI assets. The
                            # actual uploaded post image is normally served by
                            # scontent/fbcdn, while static.xx.fbcdn.net carries
                            # interface icons and sprites.
                            host = urlsplit(candidate).netloc.lower()
                            post_cdn = int("scontent" in host or ("fbcdn.net" in host and not host.startswith("static.")))
                            contextual = int(candidate in contextual_candidates)
                            # Encode contextual attachment evidence into the
                            # rank while preserving the existing tuple shape.
                            rank_area = area + (10**12 if contextual and post_cdn else 0)
                            valid_images.append((post_cdn, rank_area, len(blob), candidate))
                    except Exception:
                        continue
                if not valid_images:
                    logger.warning(
                        "Facebook embedded CDN candidates found but none returned valid image bytes candidates=%d",
                        len(image_candidates),
                    )
                    return None
                post_candidates = [
                    item for item in valid_images
                    if item[0] == 1 and item[1] >= 160000
                ]
                # If metadata context was too strict, keep real large scontent
                # images eligible; avatars/icons are filtered by pixel area.
                if not post_candidates:
                    post_candidates = [
                        item for item in valid_images
                        if item[0] == 1 and item[2] >= 100000
                    ]
                # Never fall back to static.xx.fbcdn.net/UI assets. Facebook
                # pages contain icons, sprites and profile chrome alongside the
                # actual post media; sending those is worse than a clean
                # extraction failure.
                if not post_candidates:
                    logger.warning(
                        "Facebook post photo not found among CDN candidates candidates=%d valid=%d; refusing static/UI asset fallback",
                        len(image_candidates),
                        len(valid_images),
                    )
                    return None

                _, area, byte_size, image_url = max(
                    post_candidates,
                    key=lambda item: (item[1], item[2]),
                )
                logger.info(
                    "Facebook post photo selected candidates=%d valid=%d post_cdn=%d rank_area=%d bytes=%d host=%s",
                    len(image_candidates),
                    len(valid_images),
                    len(post_candidates),
                    area,
                    byte_size,
                    urlsplit(image_url).netloc,
                )
            else:
                logger.warning(
                    "Facebook browser fallback page has no photo CDN candidate status=%s final_host=%s body_bytes=%d",
                    response.status_code,
                    urlsplit(final_url).netloc,
                    len(response.content),
                )
                return None

        image_url = urljoin(final_url, image_url)
        if urlsplit(image_url).scheme not in {"http", "https"}:
            return None

        headers = {
            "User-Agent": (_base_opts().get("http_headers") or {}).get("User-Agent", "Mozilla/5.0"),
            "Referer": final_url,
        }
        post_id = next((part for part in urlsplit(final_url).path.split("/") if part), "facebook-photo")
        info = {
            "id": post_id,
            "title": parser.values.get("og:title") or "Facebook photo",
            "webpage_url": final_url,
            "thumbnail": image_url,
            "image_url": image_url,
            "formats": [{
                "format_id": "facebook-browser-image",
                "url": image_url,
                "ext": "jpg",
                "vcodec": "none",
                "acodec": "none",
                "protocol": urlsplit(image_url).scheme,
                "http_headers": headers,
            }],
        }
        opts = _apply_cookie_policy(_base_opts(), final_url)
        logger.info("Facebook browser photo fallback succeeded final_host=%s", urlsplit(final_url).netloc)
        return info, final_url, opts
    except Exception as exc:
        logger.warning(
            "Facebook browser photo fallback failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None


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

        # Do not turn a Facebook Reel/video share thumbnail into a fake
        # photo. If OpenGraph exposes no playable video URL, let yt-dlp and the
        # authenticated browser path handle it instead.
        path_lower = urlsplit(url).path.lower()
        facebook_video_share = platform == "facebook" and (
            "/share/r/" in path_lower or "/share/v/" in path_lower or "/reel/" in path_lower or "/videos/" in path_lower
        )
        if facebook_video_share and not video_url:
            logger.info("Facebook public preview has thumbnail only for video share; skipping photo fallback")
            return None

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



def _instagram_structured_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Resolve public Instagram posts with the current web GraphQL document.

    This follows Instaloader's current post-metadata route. It preserves each
    sidecar child separately and uses image_versions2/video_versions instead
    of the single OpenGraph cover image.
    """
    if curl_requests is None:
        return None

    match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", urlsplit(url).path)
    if not match:
        return None
    shortcode = match.group(1)
    canonical_url = f"https://www.instagram.com/p/{shortcode}/"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    cookies: dict[str, str] = {}
    cookie_file = _materialize_instagram_cookie_file()
    if cookie_file and cookie_file.is_file():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = {cookie.name: cookie.value for cookie in jar}
        except Exception as exc:
            logger.warning("Instagram structured cookie load failed error_type=%s", type(exc).__name__)

    def best(items: list | None) -> dict | None:
        valid = [
            item for item in (items or [])
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item["url"].startswith(("http://", "https://"))
        ]
        return max(
            valid,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("width") or 0) + int(item.get("height") or 0),
            ),
            default=None,
        )

    def build(post: dict) -> tuple[dict, str, dict] | None:
        caption = post.get("caption")
        title = (
            (caption.get("text") if isinstance(caption, dict) else "")
            or f"Instagram post {shortcode}"
        ).strip().split("\n", 1)[0][:72]
        media_items = post.get("carousel_media") or [post]
        entries: list[dict] = []
        seen_media: set[str] = set()

        for index, item in enumerate(media_items, 1):
            if not isinstance(item, dict):
                continue
            video = best(item.get("video_versions"))
            versions = item.get("image_versions2")
            image = best(versions.get("candidates") if isinstance(versions, dict) else None)
            media_headers = {"User-Agent": ua, "Referer": canonical_url}
            child_id = str(item.get("pk") or item.get("id") or item.get("code") or f"{shortcode}_{index}")

            if video:
                media_url = video["url"]
                if media_url in seen_media:
                    continue
                seen_media.add(media_url)
                fmt = {
                    "format_id": f"instagram-structured-video-{index}",
                    "url": media_url, "ext": "mp4",
                    "vcodec": "unknown", "acodec": "unknown",
                    "width": video.get("width"), "height": video.get("height"),
                    "protocol": urlsplit(media_url).scheme,
                    "http_headers": media_headers,
                }
                entry = {
                    "id": child_id, "title": title, "webpage_url": canonical_url,
                    "formats": [fmt], "http_headers": media_headers,
                    "_mediafetch_direct_video": media_url,
                    "_mediafetch_direct_headers": media_headers,
                }
                if image:
                    entry["thumbnail"] = image["url"]
                entries.append(entry)
                continue

            if image:
                media_url = image["url"]
                if media_url in seen_media:
                    logger.warning(
                        "Instagram duplicate sidecar media skipped shortcode=%s index=%d",
                        shortcode, index,
                    )
                    continue
                seen_media.add(media_url)
                fmt = {
                    "format_id": f"instagram-structured-image-{index}",
                    "url": media_url, "ext": "jpg",
                    "vcodec": "none", "acodec": "none",
                    "width": image.get("width"), "height": image.get("height"),
                    "protocol": urlsplit(media_url).scheme,
                    "http_headers": media_headers,
                }
                entries.append({
                    "id": child_id, "title": title, "webpage_url": canonical_url,
                    "image_url": media_url, "thumbnail": media_url,
                    "thumbnails": [{
                        "url": media_url, "width": image.get("width"),
                        "height": image.get("height"), "http_headers": media_headers,
                    }],
                    "formats": [fmt], "http_headers": media_headers,
                })

        if not entries:
            return None
        logger.info(
            "Instagram structured media recovered shortcode=%s items=%d carousel=%s",
            shortcode, len(entries), len(media_items) > 1,
        )
        if len(entries) == 1:
            return entries[0], canonical_url, _base_opts()
        return {
            "id": shortcode, "title": title, "webpage_url": canonical_url,
            "entries": entries, "formats": [],
        }, canonical_url, _base_opts()

    # Instaloader currently obtains Post metadata with this doc_id and reads
    # data.xdt_api__v1__media__shortcode__web_info.items[0].
    attempts = [cookies] if cookies.get("sessionid") else []
    attempts.append({})
    seen: set[tuple[tuple[str, str], ...]] = set()
    for request_cookies in attempts:
        fingerprint = tuple(sorted(request_cookies.items()))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            session = curl_requests.Session(impersonate="chrome")
            if request_cookies:
                session.cookies.update(request_cookies)

            # Establish the web session first so csrftoken/mid and the request
            # fingerprint belong to the same session as the GraphQL POST.
            home = session.get(
                "https://www.instagram.com/",
                allow_redirects=True,
                timeout=20,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            csrf = session.cookies.get("csrftoken") or request_cookies.get("csrftoken") or ""
            variables = json.dumps({
                "shortcode": shortcode,
                "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
            }, separators=(",", ":"))
            gql = session.post(
                "https://www.instagram.com/graphql/query",
                allow_redirects=False,
                timeout=25,
                headers={
                    "User-Agent": ua,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": canonical_url,
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                },
                data={
                    "variables": variables,
                    "doc_id": "27128499623469141",
                    "server_timestamps": "true",
                },
            )
            if gql.status_code != 200:
                logger.info(
                    "Instagram structured GraphQL unavailable shortcode=%s status=%s authenticated=%s",
                    shortcode, gql.status_code, bool(request_cookies.get("sessionid")),
                )
                continue
            payload = gql.json()
            web_info = (payload.get("data") or {}).get("xdt_api__v1__media__shortcode__web_info") or {}
            items = web_info.get("items") or []
            post = items[0] if items and isinstance(items[0], dict) else None
            if post:
                result = build(post)
                if result:
                    return result
            logger.info(
                "Instagram structured GraphQL empty shortcode=%s authenticated=%s",
                shortcode, bool(request_cookies.get("sessionid")),
            )
        except Exception as exc:
            logger.warning(
                "Instagram structured GraphQL failed shortcode=%s authenticated=%s error_type=%s error=%s",
                shortcode, bool(request_cookies.get("sessionid")),
                type(exc).__name__, exc,
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


def _threads_crawler_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Extract exact public Threads media from Meta's data-sjs JSON blobs."""
    if curl_requests is None:
        return None

    ua = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    def has_media(node):
        if not isinstance(node, dict):
            return False
        if isinstance(node.get("video_versions"), list) and node.get("video_versions"):
            return True
        if isinstance(node.get("carousel_media"), list) and node.get("carousel_media"):
            return True
        iv = node.get("image_versions2")
        return bool(isinstance(iv, dict) and isinstance(iv.get("candidates"), list) and iv.get("candidates"))

    def media_source(post):
        if has_media(post):
            return post
        app_info = post.get("text_post_app_info")
        if isinstance(app_info, dict):
            linked = app_info.get("linked_inline_media")
            if isinstance(linked, dict) and has_media(linked):
                return linked
            share = app_info.get("share_info")
            if isinstance(share, dict):
                for key in ("quoted_attachment_post", "quoted_post", "reposted_post"):
                    nested = share.get(key)
                    if isinstance(nested, dict) and has_media(nested):
                        return nested
        return post

    def best_variant(items):
        candidates = [x for x in (items or []) if isinstance(x, dict) and isinstance(x.get("url"), str)]
        if not candidates:
            return None
        return max(candidates, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0))

    try:
        response = curl_requests.get(
            url, allow_redirects=True, timeout=20,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )
        if response.status_code >= 400:
            logger.warning("Threads page request status=%s url=%s", response.status_code, url)
            return None

        final_url = str(response.url)
        # Share links resolve to /@user/post/CODE?...
        match = re.search(r"/post/([A-Za-z0-9_-]+)", urlsplit(final_url).path)
        if not match:
            logger.warning("Threads canonical post code missing final_url=%s", final_url)
            return None
        post_code = match.group(1)

        # The /share/ redirect response is not always the same document as the
        # canonical public post. Refetch the clean /@user/post/CODE URL exactly
        # as the proven data-sjs parser does; signed xmt/slof params can produce
        # a shell where the post exists but its media lives in a different node.
        canonical_path = urlsplit(final_url).path.rstrip("/")
        canonical_url = f"https://www.threads.com{canonical_path}"
        if canonical_url != final_url:
            nav_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
            canonical_response = curl_requests.get(canonical_url, allow_redirects=True, timeout=20, headers=nav_headers, impersonate="chrome")
            if canonical_response.status_code < 400:
                response = canonical_response
                final_url = canonical_url
                logger.info("Threads canonical browser page loaded code=%s", post_code)

        class _ThreadsJsonParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.attrs = None
                self.body = None
                self.scripts = []
            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self.attrs = dict(attrs)
                    self.body = []
            def handle_data(self, data):
                if self.body is not None:
                    self.body.append(data)
            def handle_endtag(self, tag):
                if tag == "script" and self.body is not None:
                    self.scripts.append((self.attrs or {}, "".join(self.body)))
                    self.attrs = None
                    self.body = None

        parser = _ThreadsJsonParser()
        parser.feed(response.text)
        post = None
        blob_count = 0
        for attrs, body in parser.scripts:
            if attrs.get("type") != "application/json" or "data-sjs" not in attrs:
                continue
            if not body.lstrip().startswith("{"):
                continue
            blob_count += 1
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            wrapper = None
            for node in walk(payload):
                if node.get("code") != post_code:
                    continue
                if wrapper is None:
                    wrapper = node
                if any(key in node for key in ("video_versions", "image_versions2", "carousel_media")):
                    post = node
                    break
            if post is None and wrapper is not None:
                post = wrapper
            if post is not None:
                break

        if post is None:
            # Some Threads video pages omit the post node from data-sjs for
            # Googlebot while still exposing the progressive MP4 in page data.
            decoded = (response.text or "").replace("\\/","/").replace("\\u0026","&").replace("&amp;","&")
            video_urls = []
            for candidate in re.findall(r'https?://[^"<>\\s]+', decoded):
                low = candidate.lower()
                if (".mp4" in low or "video" in low) and ("cdninstagram.com" in low or "fbcdn.net" in low):
                    if candidate not in video_urls:
                        video_urls.append(candidate)
            if video_urls:
                media_url = video_urls[0]
                logger.info("Threads progressive video recovered code=%s candidates=%d", post_code, len(video_urls))
                fmt = {
                    "format_id": "threads-progressive-video", "url": media_url, "ext": "mp4",
                    "vcodec": "unknown", "acodec": "unknown",
                    "protocol": urlsplit(media_url).scheme,
                    "http_headers": {"Referer": "https://www.threads.com/", "User-Agent": ua},
                }
                return {"id": post_code, "title": f"Threads video {post_code}", "webpage_url": final_url, "formats": [fmt], "_mediafetch_direct_video": media_url, "_mediafetch_direct_headers": fmt["http_headers"]}, final_url, _base_opts()
            logger.warning("Threads exact post not found code=%s data_sjs_blobs=%d", post_code, blob_count)
            return None

        caption = post.get("caption")
        description = caption.get("text", "") if isinstance(caption, dict) else ""
        title = (description.strip().split("\n", 1)[0][:72] if isinstance(description, str) else "") or f"Threads post {post_code}"
        source = media_source(post)
        if not has_media(source):
            # Some current Threads payloads place media below a wrapper carrying
            # the shortcode. Prefer a media-bearing descendant before falling
            # through to GraphQL/yt-dlp.
            for child in walk(post):
                if child is not post and has_media(child):
                    source = child
                    logger.info("Threads nested media node recovered code=%s", post_code)
                    break
        items = source.get("carousel_media") or [source]
        entries = []
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            video = best_variant(item.get("video_versions"))
            if video:
                media_url = video["url"]
                fmt = {
                    "format_id": f"threads-video-{idx}", "url": media_url, "ext": "mp4",
                    "vcodec": "unknown", "acodec": "unknown",
                    "width": video.get("width"), "height": video.get("height"),
                    "protocol": urlsplit(media_url).scheme,
                    "http_headers": {"Referer": "https://www.threads.com/", "User-Agent": ua},
                }
                entries.append({
                    "id": f"{post_code}_{idx}", "title": title, "webpage_url": final_url,
                    "formats": [fmt],
                    "_mediafetch_direct_video": media_url,
                    "_mediafetch_direct_headers": fmt["http_headers"],
                })
                continue
            iv = item.get("image_versions2")
            image = best_variant(iv.get("candidates") if isinstance(iv, dict) else None)
            if image:
                media_url = image["url"]
                fmt = {
                    "format_id": f"threads-image-{idx}", "url": media_url, "ext": "jpg",
                    "vcodec": "none", "acodec": "none",
                    "width": image.get("width"), "height": image.get("height"),
                    "protocol": urlsplit(media_url).scheme,
                    "http_headers": {"Referer": "https://www.threads.com/", "User-Agent": ua},
                }
                entries.append({"id": f"{post_code}_{idx}", "title": title, "webpage_url": final_url,
                                "image_url": media_url, "thumbnail": media_url, "formats": [fmt]})

        if not entries:
            logger.warning("Threads post found but no media code=%s", post_code)
            return None
        logger.info("Threads exact media recovered code=%s media=%d", post_code, len(entries))
        if len(entries) == 1:
            return entries[0], final_url, _base_opts()
        return {"id": post_code, "title": title, "webpage_url": final_url, "entries": entries, "formats": []}, final_url, _base_opts()
    except Exception as exc:
        logger.warning("Threads structured extraction failed error_type=%s error=%s", type(exc).__name__, exc)
        return None



def _threads_proxy() -> str | None:
    """Optional proxy used only for Threads; credentials are never logged."""
    value = (settings.threads_proxy_url or "").strip()
    return value or None


def _threads_curl_proxy_kwargs() -> dict:
    proxy = _threads_proxy()
    return {"proxy": proxy} if proxy else {}


def _threads_graphql_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Resolve Threads media through Meta's own Barcelona post GraphQL query."""
    if curl_requests is None:
        return None
    try:
        resolve = curl_requests.get(
            url, allow_redirects=True, timeout=20,
            **_threads_curl_proxy_kwargs(),
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
        )
        final_url = str(resolve.url)
        match = re.search(r"/post/([A-Za-z0-9_-]+)", urlsplit(final_url).path)
        if not match:
            return None
        code = match.group(1)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        post_id = 0
        for ch in code:
            digit = alphabet.find(ch)
            if digit < 0:
                return None
            post_id = post_id * 64 + digit

        cookie_file = _materialize_instagram_cookie_file()
        cookies = {}
        if cookie_file and cookie_file.is_file():
            jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = {cookie.name: cookie.value for cookie in jar}

        # Current Threads web query (BarcelonaPostPageContentQuery).
        # Important: obtain the per-page LSD token first; Meta rejects stale
        # hard-coded LSD/doc combinations or may return an empty thread.
        clean_url = f"https://www.threads.com{urlsplit(final_url).path}"
        page_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        page = curl_requests.get(clean_url, headers=page_headers, timeout=25, impersonate="chrome")
        html = page.text or ""
        lsd_match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
        if not lsd_match:
            logger.warning("Threads GraphQL page has no LSD token code=%s status=%s", code, page.status_code)
            return None
        lsd = lsd_match.group(1)

        # Verified public implementation (2026-08): anonymous API form works
        # for public posts even when an authenticated request is rejected.
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": page_headers["User-Agent"],
            "X-FB-LSD": lsd,
            "X-IG-App-ID": "238260118697367",
            "X-ASBD-ID": "129477",
            "X-FB-Friendly-Name": "BarcelonaPostPageContentQuery",
            "Origin": "https://www.threads.com",
            "Referer": clean_url,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        response = curl_requests.post(
            "https://www.threads.com/api/graphql",
            data={
                "av": "0",
                "__user": "0",
                "__a": "1",
                "__req": "1",
                "dpr": "1",
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "BarcelonaPostPageContentQuery",
                "variables": json.dumps({"postID": str(post_id)}, separators=(",", ":")),
                "server_timestamps": "true",
                "doc_id": "25460088156920903",
            },
            headers=headers,
            impersonate="chrome",
            timeout=25,
            **_threads_curl_proxy_kwargs(),
        )
        if response.status_code != 200:
            logger.warning("Threads GraphQL HTTP status=%s code=%s", response.status_code, code)
            return None
        try:
            raw = response.text or ""
            if raw.startswith("for (;;);"):
                raw = raw[len("for (;;);"):]
            payload = json.loads(raw)
        except Exception:
            logger.warning("Threads GraphQL returned non-JSON code=%s", code)
            return None
        if payload.get("errors") and payload.get("data") is None:
            err = payload.get("errors") or []
            summary = (err[0].get("summary") or err[0].get("message") or "unknown") if err and isinstance(err[0], dict) else "unknown"
            logger.warning("Threads GraphQL API error code=%s summary=%s", code, str(summary)[:180])
            return None

        root_data = payload.get("data") or {}
        data = root_data.get("data") or {}
        logger.info(
            "Threads GraphQL response shape code=%s root_keys=%s data_keys=%s edges=%d",
            code,
            sorted(str(k) for k in root_data.keys())[:12],
            sorted(str(k) for k in data.keys())[:12] if isinstance(data, dict) else [],
            len(data.get("edges") or []) if isinstance(data, dict) else 0,
        )
        target = None
        fallback = None
        for edge in data.get("edges") or []:
            node = (edge or {}).get("node") or {}
            for item in node.get("thread_items") or []:
                post = (item or {}).get("post")
                if not isinstance(post, dict):
                    continue
                if fallback is None:
                    fallback = post
                if post.get("code") == code:
                    target = post
                    break
            if target is not None:
                break
        post = target or fallback
        if not isinstance(post, dict):
            logger.warning("Threads GraphQL returned no post code=%s", code)
            return None

        def best(items):
            valid = [x for x in (items or []) if isinstance(x, dict) and isinstance(x.get("url"), str)]
            return max(valid, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0), default=None)

        caption = post.get("caption")
        title = ((caption.get("text") if isinstance(caption, dict) else "") or f"Threads post {code}").strip()[:72]
        items = post.get("carousel_media") or [post]
        entries = []
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            video = best(item.get("video_versions"))
            if video:
                media_url = video["url"]
                h = {"Referer": final_url, "User-Agent": headers["User-Agent"]}
                fmt = {"format_id": f"threads-gql-video-{idx}", "url": media_url, "ext": "mp4",
                       "vcodec": "unknown", "acodec": "unknown", "width": video.get("width"),
                       "height": video.get("height"), "protocol": urlsplit(media_url).scheme,
                       "http_headers": h}
                entries.append({"id": f"{code}_{idx}", "title": title, "webpage_url": final_url,
                                "formats": [fmt], "_mediafetch_direct_video": media_url,
                                "_mediafetch_direct_headers": h})
                continue
            iv = item.get("image_versions2")
            image = best(iv.get("candidates") if isinstance(iv, dict) else None)
            if image:
                media_url = image["url"]
                fmt = {"format_id": f"threads-gql-image-{idx}", "url": media_url, "ext": "jpg",
                       "vcodec": "none", "acodec": "none", "width": image.get("width"),
                       "height": image.get("height"), "protocol": urlsplit(media_url).scheme}
                entries.append({"id": f"{code}_{idx}", "title": title, "webpage_url": final_url,
                                "image_url": media_url, "thumbnail": media_url, "formats": [fmt]})
        if not entries:
            logger.warning("Threads GraphQL post had no media code=%s", code)
            return None
        logger.info("Threads GraphQL media recovered code=%s media=%d", code, len(entries))
        if len(entries) == 1:
            return entries[0], final_url, _base_opts()
        return {"id": code, "title": title, "webpage_url": final_url, "entries": entries, "formats": []}, final_url, _base_opts()
    except Exception as exc:
        logger.warning("Threads GraphQL fallback failed error_type=%s error=%s", type(exc).__name__, exc)
        return None



def _threads_browser_video_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Last-resort Threads video resolver using a real rendered Chromium page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Threads browser fallback unavailable: playwright is not installed")
        return None

    # Resolve /share/ cheaply first so Chromium lands on the exact canonical post.
    try:
        resolved = curl_requests.get(
            url,
            allow_redirects=True,
            timeout=20,
            **_threads_curl_proxy_kwargs(),
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
        ) if curl_requests is not None else None
        resolved_url = str(resolved.url) if resolved is not None else url
        match = re.search(r"/post/([A-Za-z0-9_-]+)", urlsplit(resolved_url).path)
        if not match:
            logger.warning("Threads browser resolver did not reach a post")
            return None
        post_code = match.group(1)
        canonical_url = resolved_url  # preserve signed xmt/slof query from share redirect
    except Exception as exc:
        logger.warning("Threads browser resolver failed error_type=%s error=%s", type(exc).__name__, exc)
        return None

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--mute-audio",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )

            # Threads video is frequently injected only for an authenticated
            # browser session. Reuse the existing Instagram/Meta Netscape jar,
            # one cookie at a time so one malformed entry cannot abort login.
            cookie_file = _materialize_instagram_cookie_file()
            added_cookies = 0
            if cookie_file and cookie_file.is_file():
                try:
                    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
                    jar.load(ignore_discard=True, ignore_expires=True)
                    for ck in jar:
                        domain = ck.domain or ".instagram.com"
                        # Instagram session cookies are accepted by the shared
                        # Meta auth surface; keep their original domain.
                        item = {
                            "name": ck.name,
                            "value": ck.value,
                            "domain": domain,
                            "path": ck.path or "/",
                            "secure": bool(ck.secure),
                        }
                        if ck.expires:
                            item["expires"] = float(ck.expires)
                        targets = [item]
                        # A Netscape export from instagram.com otherwise leaves
                        # Chromium completely unauthenticated on threads.com.
                        # Mirror the same Meta session cookie to Threads; invalid
                        # cookies are ignored individually.
                        if "instagram.com" in domain:
                            for threads_domain in (".threads.com", ".threads.net"):
                                mirrored = dict(item)
                                mirrored["domain"] = threads_domain
                                targets.append(mirrored)
                        for target in targets:
                            try:
                                context.add_cookies([target])
                                added_cookies += 1
                            except Exception:
                                pass
                except Exception as cookie_exc:
                    logger.warning("Threads browser cookie load failed error_type=%s", type(cookie_exc).__name__)
            logger.info("Threads browser session cookies loaded count=%d", added_cookies)
            page = context.new_page()
            candidates: list[str] = []

            def remember(candidate):
                if not isinstance(candidate, str) or not candidate.startswith(("http://", "https://")):
                    return
                low = candidate.lower()
                host = urlsplit(candidate).netloc.lower()
                if (
                    (".mp4" in low or "video" in low)
                    and ("cdninstagram.com" in host or "fbcdn.net" in host or "scontent" in host)
                    and candidate not in candidates
                ):
                    candidates.append(candidate)

            def on_response(response):
                try:
                    ctype = (response.headers.get("content-type") or "").lower()
                    if ctype.startswith("video/") or ".mp4" in response.url.lower():
                        remember(response.url)
                except Exception:
                    pass

            page.on("response", on_response)
            # First establish the Threads origin so mirrored Meta cookies are
            # active before navigating to the signed permalink.
            try:
                page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            try:
                page.goto(canonical_url, wait_until="networkidle", timeout=60000)
            except Exception as nav_exc:
                logger.info("Threads browser navigation incomplete code=%s error_type=%s", post_code, type(nav_exc).__name__)
            page.wait_for_timeout(3000)

            videos = page.locator("video")
            video_count = videos.count()
            for idx in range(video_count):
                try:
                    video = videos.nth(idx)
                    remember(video.get_attribute("src"))
                    remember(video.evaluate("(v) => v.currentSrc || ''"))
                    poster = video.get_attribute("poster")
                    try:
                        video.click(timeout=1500)
                    except Exception:
                        pass
                except Exception:
                    pass
            page.wait_for_timeout(2500)

            dom_urls = page.eval_on_selector_all(
                "video",
                """els => els.flatMap(v => [
                    v.currentSrc || "",
                    v.src || "",
                    ...Array.from(v.querySelectorAll("source")).map(s => s.src || "")
                ]).filter(Boolean)""",
            )
            for candidate in dom_urls or []:
                remember(candidate)

            if not candidates:
                logger.warning(
                    "Threads rendered browser found no video code=%s page_url=%s video_elements=%d",
                    post_code,
                    urlsplit(page.url).path,
                    page.locator("video").count(),
                )
                context.close()
                browser.close()
                browser = None
                return None

            media_url = candidates[0]
            headers = {
                "Referer": "https://www.threads.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            fmt = {
                "format_id": "threads-browser-progressive",
                "url": media_url,
                "ext": "mp4",
                "vcodec": "unknown",
                "acodec": "unknown",
                "protocol": urlsplit(media_url).scheme,
                "http_headers": headers,
            }
            info = {
                "id": post_code,
                "title": f"Threads video {post_code}",
                "webpage_url": canonical_url,
                "formats": [fmt],
                "_mediafetch_direct_video": media_url,
                "_mediafetch_direct_headers": headers,
            }
            logger.info(
                "Threads rendered browser video recovered code=%s candidates=%d",
                post_code,
                len(candidates),
            )
            context.close()
            browser.close()
            browser = None
            return info, canonical_url, _base_opts()
    except Exception as exc:
        logger.warning("Threads browser fallback failed error_type=%s error=%s", type(exc).__name__, exc)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _threads_authenticated_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Resolve Threads with a logged-in Meta session and browser-navigation headers.

    Threads may return 404 when the signed canonical URL's query parameters are
    stripped. Keep the complete resolved URL (including xmt/slof), then parse
    the inlined video_versions/image_versions2 payload directly.
    """
    if curl_requests is None:
        return None
    cookie_file = _materialize_instagram_cookie_file()
    if not cookie_file or not cookie_file.is_file():
        logger.info("Threads authenticated fallback unavailable: Meta cookie jar not configured")
        return None

    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {cookie.name: cookie.value for cookie in jar}
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        # Resolve /share/... first, but KEEP the signed query string. Meta can
        # reject the same canonical path with 404 after xmt/slof are removed.
        resolve = curl_requests.get(
            url,
            allow_redirects=True,
            timeout=20,
            **_threads_curl_proxy_kwargs(),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        resolved_url = str(resolve.url)
        match = re.search(r"/post/([A-Za-z0-9_-]+)", urlsplit(resolved_url).path)
        if not match:
            logger.warning("Threads auth resolver did not reach a post status=%s", resolve.status_code)
            return None
        post_code = match.group(1)
        logger.info(
            "Threads auth resolver status=%s canonical_host=%s canonical_path=%s signed_query=%s",
            resolve.status_code,
            urlsplit(resolved_url).netloc,
            urlsplit(resolved_url).path,
            bool(urlsplit(resolved_url).query),
        )

        response = curl_requests.get(
            resolved_url,
            impersonate="chrome",
            allow_redirects=True,
            timeout=25,
            **_threads_curl_proxy_kwargs(),
            cookies=cookies,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.threads.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        final_url = str(response.url)
        if response.status_code >= 400 or "/login" in urlsplit(final_url).path.lower():
            logger.warning(
                "Threads authenticated page unavailable status=%s final_host=%s",
                response.status_code,
                urlsplit(final_url).netloc,
            )
            return None

        # Nostos-style extraction: Threads repeats escaped JSON in the HTML.
        # Parse every video_versions array rather than requiring the outer post
        # node to be present (the exact node is omitted for the failing videos).
        text = (response.text or "").replace('\\\"', "\\x00").replace("\\/", "/").replace("\\x00", '\\\"')

        def json_literal_at(source: str, pos: int):
            opening = source[pos]
            closing = {"[": "]", "{": "}"}[opening]
            depth = 0
            in_string = False
            escaped = False
            for idx in range(pos, len(source)):
                ch = source[idx]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == opening:
                    depth += 1
                elif ch == closing:
                    depth -= 1
                    if depth == 0:
                        return json.loads(source[pos:idx + 1])
            raise ValueError("unterminated Threads JSON literal")

        videos: list[dict] = []
        seen: set[str] = set()
        for vm in re.finditer(r'"video_versions":\\s*(\\[)', text):
            try:
                versions = json_literal_at(text, vm.start(1))
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(versions, list):
                continue
            for version in versions:
                if not isinstance(version, dict):
                    continue
                media_url = version.get("url")
                if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
                    continue
                if media_url in seen:
                    continue
                seen.add(media_url)
                videos.append(version)

        if not videos:
            logger.warning(
                "Threads authenticated page loaded but no video_versions code=%s html_bytes=%d",
                post_code,
                len(response.content or b""),
            )
            return None

        # Prefer the largest progressive variant. Duplicate video_versions types
        # usually point at the same muxed MP4.
        best = max(
            videos,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("bitrate") or 0),
            ),
        )
        media_url = best["url"]
        fmt = {
            "format_id": "threads-auth-progressive",
            "url": media_url,
            "ext": "mp4",
            "vcodec": "unknown",
            "acodec": "unknown",
            "width": best.get("width"),
            "height": best.get("height"),
            "protocol": urlsplit(media_url).scheme,
            "http_headers": {"Referer": final_url, "User-Agent": ua},
        }
        info = {
            "id": post_code,
            "title": f"Threads video {post_code}",
            "webpage_url": final_url,
            "formats": [fmt],
            "_mediafetch_direct_video": media_url,
            "_mediafetch_direct_headers": fmt["http_headers"],
        }
        logger.info(
            "Threads authenticated progressive video recovered code=%s variants=%d",
            post_code,
            len(videos),
        )
        return info, final_url, _base_opts()
    except Exception as exc:
        logger.warning(
            "Threads authenticated fallback failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None

def _threads_api_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Fallback used by active open-source Threads downloaders when page JSON omits videos."""
    if curl_requests is None:
        return None
    try:
        response = curl_requests.post(
            "https://www.threadsdl.app/api/threads",
            json={"url": url},
            timeout=15,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            logger.warning("Threads API fallback status=%s", response.status_code)
            return None
        data = response.json()
        medias = data.get("medias") if isinstance(data, dict) else None
        if not isinstance(medias, list):
            return None
        entries = []
        for idx, media in enumerate(medias, 1):
            if not isinstance(media, dict):
                continue
            media_type = int(media.get("mediaType") or 0)
            if media_type == 2 and isinstance(media.get("cover"), str):
                media_url = media["cover"]
                fmt = {"format_id": f"threads-api-video-{idx}", "url": media_url, "ext": "mp4",
                       "vcodec": "unknown", "acodec": "unknown", "protocol": urlsplit(media_url).scheme,
                       "http_headers": {"Referer": "https://www.threads.com/"}}
                entries.append({"id": f"threads_{idx}", "title": (data.get("text") or "Threads video")[:72],
                                "webpage_url": url, "formats": [fmt]})
        if not entries:
            return None
        logger.info("Threads API video fallback recovered media=%d", len(entries))
        if len(entries) == 1:
            return entries[0], url, _base_opts()
        return {"id": "threads", "title": (data.get("text") or "Threads post")[:72],
                "webpage_url": url, "entries": entries, "formats": []}, url, _base_opts()
    except Exception as exc:
        logger.warning("Threads API fallback failed error_type=%s error=%s", type(exc).__name__, exc)
        return None


def _youtube_post_fallback(url: str) -> tuple[dict, str, dict] | None:
    """Recover a public YouTube Community post image from page metadata."""
    if "/post/" not in urlsplit(url).path.lower():
        return None
    try:
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/146.0 Safari/537.36"
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=20) as response:
            html = response.read(6 * 1024 * 1024).decode("utf-8", "replace")
        parser = _OpenGraphParser()
        parser.feed(html)
        og_image = parser.values.get("og:image")
        # Community multi-image posts expose the remaining attachments in
        # page JSON even though OpenGraph advertises only the first image.
        raw_urls = re.findall(
            r'https?://[^"\\\\ ]+(?:yt3\\.ggpht\\.com|ytimg\\.com)[^"\\\\ ]*',
            html,
            flags=re.I,
        )
        candidates = []
        for item in ([og_image] if og_image else []) + raw_urls:
            if not item:
                continue
            item = item.replace(r"\\u0026", "&").replace("&amp;", "&")
            if item.startswith(("http://", "https://")) and item not in candidates:
                candidates.append(item)
        # Prefer post attachment images and avoid avatars/icons.
        candidates = [u for u in candidates if "yt3.ggpht.com" not in urlsplit(u).netloc.lower()] or candidates
        if not candidates:
            return None
        post_id = urlsplit(url).path.rstrip("/").split("/")[-1]
        entries = []
        for index, image_url in enumerate(candidates[:10], 1):
            fmt = {"format_id": f"youtube-community-image-{index}", "url": image_url, "ext": "jpg",
                   "vcodec": "none", "acodec": "none", "protocol": urlsplit(image_url).scheme,
                   "http_headers": {"User-Agent": ua, "Referer": url}}
            entries.append({"id": f"{post_id}-{index}", "title": parser.values.get("og:title") or "YouTube Community post",
                            "webpage_url": url, "thumbnail": image_url, "image_url": image_url, "formats": [fmt]})
        logger.info("YouTube Community images recovered count=%d", len(entries))
        return {"id": post_id, "title": parser.values.get("og:title") or "YouTube Community post",
                "webpage_url": url, "entries": entries, "formats": []}, url, _base_opts()
    except Exception as exc:
        logger.warning("YouTube Community fallback failed error_type=%s error=%s", type(exc).__name__, exc)
        return None


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
    extraction_deadline = (
        time.monotonic() + max(10, settings.extraction_timeout_seconds)
        if _platform_from_url(url) == "youtube"
        else None
    )

    # Threads: use the crawler/data-sjs resolver first. The installed
    # yt-dlp-threads plugin and this resolver use the same current Meta
    # server-rendered payload strategy; this avoids wasting time on yt-dlp's
    # generic extractor/login-wall path.
    if _platform_from_url(url) == "threads":
        threads_media = _threads_crawler_fallback(url)
        if threads_media:
            return threads_media

    # Photo/carousel posts need child-specific media metadata. Do this before
    # yt-dlp/OpenGraph: OpenGraph exposes only the cover image and can make a
    # carousel look like the same low-resolution photo repeated.
    if _platform_from_url(url) == "instagram" and re.search(r"/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+", urlsplit(url).path):
        instagram_photo = _instagram_structured_fallback(url)
        if instagram_photo:
            return instagram_photo

    for candidate in _url_variants(url):
        for profile in _extract_profiles(candidate):
            if extraction_deadline is not None and time.monotonic() >= extraction_deadline:
                raise DownloadError(
                    "YouTube extraction timed out while checking the available player clients."
                )
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

                # Do not stop on a metadata-only result. This is what lets the
                # generic OpenGraph/direct-media fallback run when a site's
                # dedicated extractor returns a shell page with no formats.
                if (
                    _has_video_format(info)
                    or _best_thumbnail(info)
                    or _image_entries(info)
                ):
                    return info, candidate, opts
                raise DownloadError("Extractor returned no media formats or images.")
            except Exception as exc:
                last_error = exc
                profile_name = "generic" if profile.get("allowed_extractors") else "native"
                youtube_clients = (
                    profile.get("extractor_args", {})
                    .get("youtube", {})
                    .get("player_client")
                    if _platform_from_url(candidate) == "youtube"
                    else None
                )
                logger.warning(
                    "yt-dlp extraction attempt failed platform=%s profile=%s clients=%s cookies=%s url=%s error=%s",
                    _platform_from_url(candidate),
                    profile_name,
                    youtube_clients,
                    bool(profile.get("cookiefile")),
                    candidate,
                    exc,
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
    # Playlist/carousel results can keep media formats on child entries
    # while the parent has an empty formats list. Inspect recursively so a
    # video carousel/reel is not presented as photo-only.
    def has_video_recursive(node: dict) -> bool:
        if _has_video_format(node):
            return True
        children = node.get("entries") or []
        return any(isinstance(child, dict) and has_video_recursive(child) for child in children)

    is_photo = not has_video_recursive(info) and bool(_best_thumbnail(info) or entries)
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

    if max_file_mb > 0 and len(data) > max_file_mb * 1024 * 1024:
        raise DownloadError(f"Image exceeds the {max_file_mb} MB plan limit.")

    # Never trust a .jpg suffix alone. Meta/CDN URLs can return an HTML
    # interstitial/error page with HTTP 200; saving that as .jpg later makes
    # Telegram and Pillow fail with Image_process_failed/UnidentifiedImageError.
    image_signatures = (
        data.startswith(bytes.fromhex("ffd8ff")),
        data.startswith(bytes.fromhex("89504e470d0a1a0a")),
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
    if data.startswith(bytes.fromhex("ffd8ff")):
        extension = ".jpg"
    elif data.startswith(bytes.fromhex("89504e47")):
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
        # Always include the carousel position in the filename. Some Meta
        # sidecar payloads reuse the parent media id for every child; without
        # the index each download overwrote the same file and the returned
        # path list therefore pointed to one repeated final image.
        target = Path(output_dir) / f"{safe_title}-{index:02d}-{stem}"
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


def _download_direct_video(
    media_url: str,
    output_dir: str,
    media_id: str,
    max_file_mb: int,
    notify: Callable[[float, str], None],
    headers: dict[str, str] | None = None,
    platform: str = "Media",
) -> Path:
    """Stream an already-resolved progressive Meta CDN video URL directly."""
    safe_platform = "".join(ch if ch.isalnum() else "-" for ch in platform).strip("-") or "Media"
    target = Path(output_dir) / f"{safe_platform}-{media_id}.mp4"
    part = target.with_suffix(".mp4.part")
    request_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        **(headers or {}),
    }
    request = urllib.request.Request(media_url, headers=request_headers)
    limit = max_file_mb * 1024 * 1024 if max_file_mb > 0 else 0
    downloaded = 0
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            if limit and total and total > limit:
                raise DownloadError(f"Video exceeds the {max_file_mb} MB plan limit.")
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if limit and downloaded > limit:
                    raise DownloadError(f"Video exceeds the {max_file_mb} MB plan limit.")
                fh.write(chunk)
                elapsed = max(time.monotonic() - started, 0.001)
                percent = (downloaded / total * 100) if total else 0
                notify(percent, f"{percent:.0f}% • {downloaded / elapsed / (1024 * 1024):.1f} MB/s")
        if downloaded <= 0:
            raise DownloadError(f"{platform} returned an empty video.")
        part.replace(target)
        notify(100, "ready")
        return target
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _download_structured_media(
    info: dict,
    output_dir: str,
    max_file_mb: int,
    notify: Callable[[float, str], None],
    platform: str,
) -> list[Path]:
    """Download structured Meta media in original carousel order.

    Threads/Instagram fallbacks can return a parent object with child entries.
    Some children are videos and some are images. Never send those mixed entries
    through the image downloader, because a video CDN response is valid media
    but is intentionally rejected by the image signature validator.
    """
    entries = _image_entries(info)
    if not entries:
        raise DownloadError("No downloadable media was found in this post.")

    paths: list[Path] = []
    for index, entry in enumerate(entries, start=1):
        direct_video = entry.get("_mediafetch_direct_video")
        if isinstance(direct_video, str) and direct_video:
            headers = entry.get("_mediafetch_direct_headers")
            if not isinstance(headers, dict):
                headers = {}
            path = _download_direct_video(
                direct_video,
                output_dir,
                str(entry.get("id") or f"media-{index}"),
                max_file_mb,
                lambda percent, detail, base=index - 1: notify(
                    ((base + percent / 100) / len(entries)) * 100,
                    f"media {index}/{len(entries)} • {detail}",
                ),
                headers,
                platform=platform,
            )
            paths.append(path)
            continue

        image_url = _direct_image_url(entry)
        thumbnail = _best_thumbnail(entry)
        if not image_url:
            image_url = (thumbnail or {}).get("url")
        if not image_url:
            raise DownloadError(
                f"Structured media item {index}/{len(entries)} has no downloadable URL."
            )

        stem = entry.get("id") or info.get("id") or f"media-{index}"
        title = entry.get("title") or info.get("title") or "media"
        safe_title = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(title)
        )[:60]
        target = Path(output_dir) / f"{safe_title}-{index:02d}-{stem}"
        image_headers = (thumbnail or {}).get("http_headers") or entry.get("http_headers")
        for fmt in entry.get("formats") or []:
            if isinstance(fmt, dict) and fmt.get("url") == image_url:
                image_headers = fmt.get("http_headers") or image_headers
                break
        path = _download_image(image_url, target, max_file_mb, image_headers)
        paths.append(path)
        notify(index / len(entries) * 100, f"media {index}/{len(entries)}")

    return paths



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
            **({"max_filesize": max_file_mb * 1024 * 1024} if max_file_mb > 0 else {}),
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
            if platform == "facebook" and mode != "audio" and _has_video_format(info):
                # Extraction profiles may otherwise fall back to an audio-only
                # "best" candidate. Require a video stream for Facebook.
                opts["format"] = selector or "bv+ba/b[vcodec!=none][ext=mp4]/b[vcodec!=none]"
            opts["noplaylist"] = True
            opts["merge_output_format"] = "mp4"
            if max_file_mb > 0:
                opts["max_filesize"] = max_file_mb * 1024 * 1024
            else:
                opts.pop("max_filesize", None)
            opts["progress_hooks"] = [progress_hook]
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        # "photo" is a UI choice only for genuinely image-only posts.
        # Never turn a Reel/video into its poster merely because a stale
        # callback requested photo mode.
        if mode == "photo" and _has_video_format(info):
            mode = "best"
            selector = "bv+ba/b[vcodec!=none][ext=mp4]/b[vcodec!=none]" if platform == "facebook" else "bv*+ba/b"
            opts["format"] = selector

        direct_platform_video = (
            info.get("_mediafetch_direct_video")
            if platform in {"threads", "instagram"}
            else None
        )
        if mode != "audio" and isinstance(direct_platform_video, str):
            logger.info("%s direct CDN download starting id=%s", platform, info.get("id"))
            return _download_direct_video(
                direct_platform_video,
                output_dir,
                str(info.get("id") or "video"),
                max_file_mb,
                notify,
                info.get("_mediafetch_direct_headers")
                if isinstance(info.get("_mediafetch_direct_headers"), dict)
                else None,
                platform=platform,
            )

        if mode != "audio" and platform in {"threads", "instagram"} and info.get("entries"):
            structured_entries = _image_entries(info)
            if any(
                isinstance(entry, dict)
                and isinstance(entry.get("_mediafetch_direct_video"), str)
                for entry in structured_entries
            ):
                logger.info(
                    "%s structured mixed-media download starting items=%d",
                    platform,
                    len(structured_entries),
                )
                return _download_structured_media(
                    info,
                    output_dir,
                    max_file_mb,
                    notify,
                    platform,
                )

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
