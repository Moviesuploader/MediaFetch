from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from telegram.error import BadRequest
from telegram import InputFile, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

import qrcode
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.core.config import settings
from app.bot.admin import admin_has_pending_action, admin_message_router
from app.core.rate_limit import UserRateLimiter
from app.core.storage import storage
from app.core.payments import PLANS, PLAN_LABELS, create_payment, payment_config, valid_utr
from app.core.cashfree import configured as cashfree_configured, create_order as cashfree_create_order, public_base_url as cashfree_public_base_url
from app.bot.mtproto import LargeUploadError, mtproto_uploader
from app.downloader.detector import detect_platform
from app.downloader.service import DownloadError, MediaInfo, download_media, get_media_info

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ACTIVE_JOBS: dict[tuple[int, str], tuple[asyncio.Task, threading.Event]] = {}
_ACTIVE_LOCK = asyncio.Lock()
_PENDING_REQUESTS: dict[int, tuple[str, str, str, MediaInfo | None, str | None, object | None]] = {}
_PENDING_PAYMENT_PLAN: dict[int, str] = {}
_PENDING_CASHFREE_PLAN: dict[int, str] = {}
_PENDING_LOCK = asyncio.Lock()
class _DynamicDownloadLimiter:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0

    async def acquire(self) -> None:
        async with self._condition:
            while self._active >= storage.concurrent_download_limit():
                await self._condition.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


_DOWNLOAD_LIMITER = _DynamicDownloadLimiter()
_RATE_LIMITER = UserRateLimiter(min_interval=3.0)
logger = logging.getLogger(__name__)

SUPPORTED_TEXT = (
    "YouTube • Instagram • Facebook • Reddit • X/Twitter • "
    "TikTok • Pinterest • Threads"
)


def _prepare_source_thumbnail(
    thumbnail_url: str | None,
    path: Path,
    source_url: str | None = None,
) -> Path | None:
    """Prefer a real YouTube poster and skip empty/black placeholder thumbnails."""
    candidates: list[str] = []
    if source_url:
        video_id = None
        parts = urlsplit(source_url)
        host = parts.netloc.lower().removeprefix("www.")
        if host == "youtu.be":
            video_id = parts.path.strip("/").split("/", 1)[0]
        elif host.endswith("youtube.com"):
            video_id = (
                re.search(r"(?:^|[?&])v=([A-Za-z0-9_-]{6,})", parts.query)
                or re.search(r"/(?:shorts|live)/([A-Za-z0-9_-]{6,})", parts.path)
            )
            video_id = video_id.group(1) if video_id else None
        if video_id:
            candidates.extend(
                f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
                for name in ("maxresdefault", "sddefault", "hqdefault", "mqdefault")
            )
    if isinstance(thumbnail_url, str) and thumbnail_url.startswith(("http://", "https://")):
        if thumbnail_url not in candidates:
            candidates.append(thumbnail_url)

    if not candidates:
        return None
    target = path.with_name(f"{path.stem}-source-thumb.jpg")
    for candidate_url in candidates:
        try:
            request = urllib.request.Request(
                candidate_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,*/*;q=0.8",
                    "Referer": source_url or "https://www.youtube.com/",
                },
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                data = response.read(5 * 1024 * 1024 + 1)
                content_type = (response.headers.get("Content-Type") or "").lower()
            if not data or len(data) > 5 * 1024 * 1024 or not content_type.startswith("image/"):
                continue
            from PIL import Image, ImageOps, ImageStat
            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                # YouTube returns a black placeholder for unavailable maxres posters.
                stats = ImageStat.Stat(image.resize((32, 32)))
                if max(stats.stddev) < 3.0 and max(stats.mean) < 35:
                    continue
                image.thumbnail((320, 320), Image.Resampling.LANCZOS)
                image.save(target, format="JPEG", quality=86, optimize=True)
            if target.stat().st_size > 190 * 1024:
                with Image.open(target) as image:
                    for quality in (76, 66, 56):
                        image.save(target, format="JPEG", quality=quality, optimize=True)
                        if target.stat().st_size <= 190 * 1024:
                            break
            if target.stat().st_size <= 200 * 1024:
                logger.info("Source thumbnail prepared candidate_host=%s", urllib.parse.urlsplit(candidate_url).netloc)
                return target
            target.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Thumbnail candidate unavailable host=%s error_type=%s", urllib.parse.urlsplit(candidate_url).netloc, type(exc).__name__)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
    logger.info("No usable source thumbnail found; falling back to generated preview")
    return None

def _cache_key(url: str, mode: str) -> str:
    # v4 invalidates Instagram carousel cache created from cover/thumbnail fallbacks.
    return hashlib.sha256(f"v5|{url}|{mode}".encode("utf-8")).hexdigest()


def _is_owner(user_id: int) -> bool:
    try:
        return bool(settings.owner_id and int(str(settings.owner_id).strip()) == user_id)
    except (TypeError, ValueError):
        return False


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set or _is_owner(user_id)


def _plan_name(user_id: int) -> str:
    return str(storage.plan_info(user_id).get("plan") or "free").lower()


def _file_limit_mb(user_id: int) -> int:
    if _is_admin(user_id):
        return 0
    limits = storage.file_limits()
    return int(limits.get(_plan_name(user_id), limits["free"]))


def _limit_label(user_id: int) -> str:
    limits = storage.file_limits()
    if _is_admin(user_id):
        return "Unlimited (Admin/Owner)"
    plan = _plan_name(user_id)
    labels = {
        "free": f"{limits['free']} MB • Free",
        "bronze": f"{limits['bronze']} MB • Bronze 🥉",
        "platinum": f"{limits['platinum']} MB • Platinum 💎",
        "diamond": f"{limits['diamond']} MB • Diamond 💎",
    }
    return labels.get(plan, f"{limits['free']} MB • Free")


def _plan_upgrade_hint(user_id: int) -> str:
    limits = storage.file_limits()
    return (
        f"🆓 Free: <b>{limits['free']} MB</b>\n"
        f"🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
        f"💎 Platinum: <b>{limits['platinum']} MB</b>\n"
        f"💎 Diamond: <b>{limits['diamond']} MB</b>"
    )


def _estimated_size_for_mode(info: MediaInfo, mode: str) -> int:
    if not info.estimated_sizes:
        return 0
    values = dict(info.estimated_sizes)
    if mode.endswith("p") and mode[:-1].isdigit():
        return values.get(int(mode[:-1]), 0)
    if mode == "best":
        known = [size for height, size in info.estimated_sizes if height > 0]
        return max(known, default=0)
    return 0


def _limit_message(user_id: int, estimated_bytes: int, limit_mb: int) -> str:
    estimated_mb = estimated_bytes / (1024 * 1024)
    return (
        f"📦 <b>File is above your {_limit_label(user_id)} limit.</b>\n\n"
        f"Estimated size: <b>{estimated_mb:.1f} MB</b>\n\n"
        f"{_plan_upgrade_hint(user_id)}\n\n"
        "Use /premium to see plan status."
    )


def _limit_for(user_id: int) -> int:
    # 0 means unlimited; admins/owner bypass the normal daily quota.
    return storage.daily_limit(user_id, is_admin=_is_admin(user_id))


def _media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp4":
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    return "document"


async def _log_link(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str | None,
                   platform: str, url: str, status: str = "⏳ Processing") :
    channel = str((storage.channel_config().get("links") or settings.links_log_channel_id or "")).strip()
    if not channel:
        return None
    user_label = f"@{username}" if username else str(user_id)
    text = (
        f"🔗 <b>MediaFetch Link Log</b>\n"
        f"👤 <b>User:</b> {html.escape(user_label)} (<code>{user_id}</code>)\n"
        f"🌐 <b>Platform:</b> {html.escape(platform)}\n"
        f"📌 <b>Status:</b> {html.escape(status)}\n"
        f"🔗 <b>URL:</b> <code>{html.escape(url)}</code>"
    )
    try:
        return await context.bot.send_message(chat_id=channel, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Links log channel send failed error_type=%s error=%s", type(exc).__name__, exc)
        return None


async def _update_link_log(context: ContextTypes.DEFAULT_TYPE, log_message, user_id: int,
                           username: str | None, platform: str, url: str, status: str) -> None:
    if log_message is None:
        return
    user_label = f"@{username}" if username else str(user_id)
    text = (
        f"🔗 <b>MediaFetch Link Log</b>\n"
        f"👤 <b>User:</b> {html.escape(user_label)} (<code>{user_id}</code>)\n"
        f"🌐 <b>Platform:</b> {html.escape(platform)}\n"
        f"📌 <b>Status:</b> {html.escape(status)}\n"
        f"🔗 <b>URL:</b> <code>{html.escape(url)}</code>"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=log_message.chat_id,
            message_id=log_message.message_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Links log channel update failed error_type=%s", type(exc).__name__)


async def _dump_messages(context: ContextTypes.DEFAULT_TYPE, messages: list, user_id: int,
                         username: str | None, platform: str, url: str) -> None:
    channel = str((storage.channel_config().get("dump") or settings.dump_channel_id or "")).strip()
    if not channel or not messages:
        return
    user_label = f"@{username}" if username else str(user_id)
    header = (
        f"📥 <b>MediaFetch Dump</b>\n"
        f"👤 <b>User:</b> {html.escape(user_label)} (<code>{user_id}</code>)\n"
        f"🌐 <b>Platform:</b> {html.escape(platform)}\n"
        f"🔗 <code>{html.escape(url)}</code>"
    )
    try:
        await context.bot.send_message(chat_id=channel, text=header, parse_mode="HTML")
        for sent in messages:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=sent.chat_id,
                message_id=sent.message_id,
            )
    except Exception as exc:
        logger.warning("Dump channel send failed error_type=%s error=%s", type(exc).__name__, exc)


async def _send_media_message(
    message,
    path: Path | None = None,
    file_id: str | None = None,
    kind: str = "document",
    caption: str = "",
    upload_progress=None,
    cancel_event=None,
    thumbnail_url: str | None = None,
    source_url: str | None = None,
):
    if file_id:
        if kind == "video":
            return await message.reply_video(video=file_id, caption=caption, parse_mode="HTML", supports_streaming=True)
        if kind == "photo":
            return await message.reply_photo(photo=file_id, caption=caption, parse_mode="HTML")
        return await message.reply_document(document=file_id, caption=caption, parse_mode="HTML")

    if path is None:
        raise ValueError("path or file_id is required")

    with path.open("rb") as raw_media:
        media = raw_media
        if upload_progress is not None:
            media = _ProgressFile(
                raw_media,
                path.stat().st_size,
                upload_progress,
                cancel_event,
            )
            # Keep the file handle lazy so python-telegram-bot/HTTPX reads it
            # during multipart transmission and _ProgressFile can report real
            # byte-level upload progress.
            media = InputFile(
                media,
                filename=path.name,
                read_file_handle=False,
            )
        if kind == "video":
            thumbnail_file = None
            thumbnail_handle = None
            source_thumb_path = None
            generated_thumb_path = None
            try:
                source_thumb_path = await asyncio.to_thread(_prepare_source_thumbnail, thumbnail_url, path, source_url)
                thumb_path = source_thumb_path
                if thumb_path is None:
                    generated_thumb_path = path.with_name(f"{path.stem}-thumb.jpg")
                    process = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-ss", "00:00:01", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(generated_thumb_path),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(process.wait(), timeout=15)
                    if process.returncode == 0 and generated_thumb_path.is_file() and generated_thumb_path.stat().st_size:
                        thumb_path = generated_thumb_path
                if thumb_path is not None and thumb_path.is_file():
                    thumbnail_handle = thumb_path.open("rb")
                    thumbnail_file = InputFile(thumbnail_handle, filename="thumbnail.jpg")
                return await message.reply_video(video=media, caption=caption, parse_mode="HTML", supports_streaming=True, thumbnail=thumbnail_file)
            finally:
                if thumbnail_handle: thumbnail_handle.close()
                for cleanup in (source_thumb_path, generated_thumb_path):
                    if cleanup:
                        try: cleanup.unlink(missing_ok=True)
                        except OSError: pass
        if kind == "photo" and path.stat().st_size <= 10 * 1024 * 1024:
            return await message.reply_photo(photo=media, caption=caption)
        return await message.reply_document(document=media, caption=caption)


def _normalize_telegram_photo(path: Path) -> Path:
    """Rewrite an image to Telegram-safe RGB JPEG dimensions."""
    from PIL import Image, ImageOps

    normalized = path.with_name(f"{path.stem}-telegram.jpg")
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, "white")
            alpha = image.getchannel("A")
            background.paste(image.convert("RGB"), mask=alpha)
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        # Telegram Bot API requires width + height <= 10000 and the aspect
        # ratio must not exceed 20:1. Leave margin below both limits.
        max_sum = 9500
        max_ratio = 19.5
        ratio = max(width, height) / max(1, min(width, height))
        if ratio > max_ratio:
            if width >= height:
                target_height = max(1, int(width / max_ratio))
                canvas = Image.new("RGB", (width, target_height), "white")
                canvas.paste(image, (0, (target_height - height) // 2))
            else:
                target_width = max(1, int(height / max_ratio))
                canvas = Image.new("RGB", (target_width, height), "white")
                canvas.paste(image, ((target_width - width) // 2, 0))
            image = canvas
            width, height = image.size

        if width + height > max_sum:
            scale = max_sum / (width + height)
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )

        image.save(normalized, format="JPEG", quality=92, optimize=True)
    logger.info(
        "Normalized Telegram photo source=%s output=%s dimensions=%sx%s size=%d",
        path.name, normalized.name, image.size[0], image.size[1], normalized.stat().st_size,
    )
    return normalized

async def _send_photo_album(
    message,
    paths: list[Path] | None = None,
    file_ids: list[str] | None = None,
    caption: str = "",
):
    """Send carousel photos as one Telegram album instead of separate messages."""
    media: list[InputMediaPhoto] = []

    if file_ids is not None:
        if len(file_ids) == 1:
            return [await message.reply_photo(photo=file_ids[0], caption=caption)]
        for index, file_id in enumerate(file_ids):
            media.append(
                InputMediaPhoto(
                    media=file_id,
                    caption=caption if index == 0 else None,
                    parse_mode="HTML",
                )
            )
        return await message.reply_media_group(media=media)

    if not paths:
        raise ValueError("paths or file_ids are required")

    if len(paths) == 1:
        path = paths[0]
        try:
            with path.open("rb") as photo:
                return [await message.reply_photo(photo=photo, caption=caption, parse_mode="HTML")]
        except BadRequest as exc:
            telegram_error = str(exc).lower()
            if not any(code in telegram_error for code in ("image_process_failed", "photo_invalid_dimensions")):
                raise
            logger.warning("Telegram rejected original photo; normalizing path=%s size=%d", path.name, path.stat().st_size)
            normalized = await asyncio.to_thread(_normalize_telegram_photo, path)
            with normalized.open("rb") as photo:
                return [await message.reply_photo(photo=photo, caption=caption, parse_mode="HTML")]

    # Telegram albums accept 2–10 media items. max_carousel_items is capped
    # at 10 in settings, so all photo carousel items can be sent together.
    from contextlib import ExitStack

    with ExitStack() as stack:
        for index, path in enumerate(paths):
            photo = stack.enter_context(path.open("rb"))
            media.append(
                InputMediaPhoto(
                    media=photo,
                    caption=caption if index == 0 else None,
                    parse_mode="HTML",
                )
            )
        return await message.reply_media_group(media=media)


def _post_caption(info: MediaInfo | None, platform: str, label: str, item_count: int = 1) -> str:
    """Build one compact album caption from source metadata."""
    if not isinstance(info, MediaInfo):
        return f"📥 <b>{html.escape(platform)}</b> • <b>{html.escape(label)}</b>"
    title = html.escape((info.title or "Media").strip()[:180])
    uploader = html.escape((info.uploader or "").strip()[:120])
    description = html.escape(re.sub(r"\s+", " ", info.description or "").strip()[:420])
    lines = [
        f"📥 <b>{html.escape(platform)}</b> • <b>{html.escape(label)}</b>",
        f"🎬 <b>{title}</b>",
    ]
    if uploader:
        lines.append(f"👤 <b>By:</b> {uploader}")
    if item_count > 1:
        lines.append(f"🖼️ <b>Media:</b> {item_count} items")
    if description and description.lower() != (info.title or "").strip().lower():
        lines.append(f"📝 {description}")
    if info.webpage_url:
        lines.append(f'🔗 <a href="{html.escape(info.webpage_url, quote=True)}">Source post</a>')
    return "\n".join(lines)[:1024]


async def _send_media_album(
    message,
    paths: list[Path] | None = None,
    file_ids: list[str] | None = None,
    kinds: list[str] | None = None,
    caption: str = "",
) -> list:
    """Send photo/video media as native Telegram albums, max 10 per request."""
    if file_ids is not None:
        ids = list(file_ids)
        resolved_kinds = list(kinds or [])
        if len(ids) == 1:
            kind = resolved_kinds[0] if resolved_kinds else "document"
            return [await _send_media_message(message, file_id=ids[0], kind=kind, caption=caption)]
        sent: list = []
        for start in range(0, len(ids), 10):
            chunk = ids[start:start + 10]
            media = []
            for index, file_id in enumerate(chunk):
                kind = resolved_kinds[start + index] if start + index < len(resolved_kinds) else "document"
                if kind == "photo":
                    media.append(InputMediaPhoto(media=file_id, caption=caption if start == 0 and index == 0 else None, parse_mode="HTML"))
                elif kind == "video":
                    media.append(InputMediaVideo(
                        media=file_id,
                        caption=caption if start == 0 and index == 0 else None,
                        parse_mode="HTML",
                        supports_streaming=True,
                    ))
            if len(media) >= 2:
                sent.extend(await message.reply_media_group(media=media))
            elif media:
                item = media[0]
                if isinstance(item, InputMediaPhoto):
                    sent.append(await message.reply_photo(photo=item.media, caption=item.caption, parse_mode="HTML"))
                else:
                    sent.append(await message.reply_video(video=item.media, caption=item.caption, parse_mode="HTML", supports_streaming=True))
        return sent

    if not paths:
        raise ValueError("paths or file_ids are required")
    if len(paths) == 1:
        return [await _send_media_message(
            message, path=paths[0],
            kind=(kinds[0] if kinds else _media_kind(paths[0])),
            caption=caption,
        )]

    resolved_kinds = list(kinds or [_media_kind(path) for path in paths])
    if not all(kind in {"photo", "video"} for kind in resolved_kinds):
        return [
            await _send_media_message(
                message, path=path, kind=kind,
                caption=caption if index == 0 else "",
            )
            for index, (path, kind) in enumerate(zip(paths, resolved_kinds))
        ]

    from contextlib import ExitStack
    sent: list = []
    for start in range(0, len(paths), 10):
        chunk_paths = paths[start:start + 10]
        chunk_kinds = resolved_kinds[start:start + 10]
        media = []
        with ExitStack() as stack:
            for index, (path, kind) in enumerate(zip(chunk_paths, chunk_kinds)):
                handle = stack.enter_context(path.open("rb"))
                item_caption = caption if start == 0 and index == 0 else None
                if kind == "photo":
                    media.append(InputMediaPhoto(media=handle, caption=item_caption, parse_mode="HTML"))
                else:
                    # Omit per-video FFmpeg thumbnails in albums to save CPU/RAM
                    # on Koyeb free; Telegram can generate a preview itself.
                    media.append(InputMediaVideo(
                        media=handle,
                        caption=item_caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                    ))
            if len(media) >= 2:
                sent.extend(await message.reply_media_group(media=media))
            else:
                sent.append(await _send_media_message(
                    message,
                    path=chunk_paths[0],
                    kind=chunk_kinds[0],
                    caption=caption if start == 0 else "",
                ))
    return sent


class _ProgressFile:
    """File-like wrapper that reports actual multipart upload progress."""

    def __init__(self, file_handle, total_bytes: int, progress_callback, cancel_event=None):
        self._file = file_handle
        self._total = max(0, int(total_bytes))
        self._callback = progress_callback
        self._cancel_event = cancel_event
        self._uploaded = 0
        self._started = time.monotonic()
        self._last_report = 0.0

    def read(self, size=-1):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise DownloadError("Upload cancelled by user.")
        data = self._file.read(size)
        if data:
            self._uploaded += len(data)
            now = time.monotonic()
            if self._uploaded >= self._total or now - self._last_report >= 0.8:
                self._last_report = now
                elapsed = max(now - self._started, 0.001)
                percent = self._uploaded / self._total * 100 if self._total else 0.0
                speed = self._uploaded / elapsed / (1024 * 1024)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._callback(
                            percent,
                            f"{self._uploaded / (1024 * 1024):.1f}/"
                            f"{self._total / (1024 * 1024):.1f} MB • {speed:.1f} MB/s",
                        )
                    )
                except RuntimeError:
                    pass
        return data

    def seek(self, *args):
        return self._file.seek(*args)

    def tell(self):
        return self._file.tell()

    def fileno(self):
        return self._file.fileno()

    def __getattr__(self, name):
        return getattr(self._file, name)


def _cancel_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"mf:{request_id}:cancel")]
    ])


def _quality_keyboard(info: MediaInfo, request_id: str) -> InlineKeyboardMarkup:
    if info.is_photo:
        rows = [[InlineKeyboardButton("📸 HD / Original", callback_data=f"mf:{request_id}:photo")]]
    else:
        rows = [[
            InlineKeyboardButton("🎬 Best", callback_data=f"mf:{request_id}:best"),
            InlineKeyboardButton("🎵 MP3", callback_data=f"mf:{request_id}:audio"),
        ]]
        # Use the exact heights returned by the extractor/API. Do not synthesize
        # missing qualities (e.g. showing 720p when the API returned 1080/480
        # only). This keeps the buttons truthful and prevents avoidable
        # "format not available" downloads.
        available = sorted(
            {int(height) for height in info.heights if int(height) > 0},
            reverse=True,
        )
        for index in range(0, len(available), 2):
            rows.append([
                InlineKeyboardButton(
                    f"📺 {height}p",
                    callback_data=f"mf:{request_id}:{height}p",
                )
                for height in available[index:index + 2]
            ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"mf:{request_id}:cancel")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user = update.effective_user
    if user:
        await asyncio.to_thread(storage.touch_user, user.id, user.username)
    limits = storage.file_limits()
    await update.message.reply_text(
        "⚡ <b>Welcome to MediaFetch</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 Send a public media link and choose your quality.\n"
        "🎬 Video • 🎵 MP3 • 📸 HD Photos • 🖼️ Carousels\n\n"
        "📦 <b>Plan file limits</b>\n"
        f"🆓 Free: <b>{limits['free']} MB</b>\n"
        f"🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
        f"💎 Platinum: <b>{limits['platinum']} MB</b>\n"
        f"👑 Diamond: <b>{limits['diamond']} MB</b>\n\n"
        "👇 Manage your plan or explore supported sites below.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Premium Plans", callback_data="mfp:plans"), InlineKeyboardButton("👤 My Plan", callback_data="mfp:status")],
            [InlineKeyboardButton("🌐 Supported Sites", callback_data="mfp:supported"), InlineKeyboardButton("❓ Help", callback_data="mfp:help")],
        ]),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🛠 <b>MediaFetch Help</b>\n\n"
        "1️⃣ Send a public media URL.\n"
        "2️⃣ I inspect the available media and qualities.\n"
        "3️⃣ Choose a quality.\n"
        "4️⃣ I download and send it back.\n\n"
        "Commands: /start /help /supported /about /premium /history",
        parse_mode="HTML",
    )


async def supported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        f"🌐 <b>Supported platforms</b>\n\n{SUPPORTED_TEXT}\n\n"
        "Actual availability depends on the source and yt-dlp.",
        parse_mode="HTML",
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "⚡ <b>MediaFetch</b>\n\n"
        "Universal public-media downloader powered by yt-dlp + FFmpeg.\n"
        "Includes quality selection, HD photo support, caching, limits and admin tools.\n\n"
        "Only download content you are authorized to download.",
        parse_mode="HTML",
    )


async def premium_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    from datetime import datetime, timezone
    info = await asyncio.to_thread(storage.plan_info, user_id)
    plan = str(info.get("plan") or "free")
    limits = storage.file_limits()
    labels = {
        "free": ("🆓 Free", limits["free"]),
        "bronze": ("🥉 Bronze", limits["bronze"]),
        "platinum": ("💎 Platinum", limits["platinum"]),
        "diamond": ("💎 Diamond", limits["diamond"]),
    }
    label, file_limit = labels.get(plan, labels["free"])
    used = await asyncio.to_thread(storage.usage_today, user_id)
    if _is_admin(user_id):
        await update.message.reply_text(
            "👑 <b>Admin / Owner</b>\n"
            "📏 Max file: <b>Unlimited*</b>\n"
            "📥 Daily downloads: <b>Unlimited</b>\n\n"
            "*Subject to Telegram/API/account limits.",
            parse_mode="HTML",
        )
        return
    if info.get("active"):
        date = datetime.fromtimestamp(float(info["until"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(
            f"📦 <b>{label}</b> active\n"
            f"📏 Max file: <b>{file_limit} MB</b>\n"
            f"📅 Until: <b>{date}</b>\n"
            f"📥 Today: <b>{used}/{storage.daily_limit(user_id)}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"🆓 <b>Free plan</b>\n"
            f"📏 Max file: <b>{file_limit} MB</b>\n"
            f"📥 Today: <b>{used}/{settings.free_daily_limit}</b>\n\n"
            f"🥉 Bronze — {limits['bronze']} MB\n"
            f"💎 Platinum — {limits['platinum']} MB\n"
            f"💎 Diamond — {limits['diamond']} MB",
            parse_mode="HTML",
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    items = await asyncio.to_thread(storage.history, user_id, 10)
    if not items:
        await update.message.reply_text("📜 No download history yet.")
        return
    lines = ["📜 <b>Your recent downloads</b>"]
    for index, item in enumerate(items, start=1):
        title = str(item.get("title") or "Media").replace("<", "&lt;").replace(">", "&gt;")[:70]
        mode = str(item.get("mode") or "best")
        platform = str(item.get("platform") or "Unknown")
        state = "✅" if item.get("success") else "❌"
        lines.append(f"{index}. {state} <b>{platform}</b> • {mode} • {title}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    cfg = payment_config()
    limits = storage.file_limits()
    lines = ["💎 <b>MediaFetch Premium Plans</b>", "", "Choose a plan to pay via UPI:"]
    buttons = []
    for plan in PLANS:
        price = cfg["prices"][plan]
        days = cfg["durations"][plan]
        lines.append(f"{PLAN_LABELS[plan]} — <b>{price} {cfg['currency']}</b> • <b>{days} days</b> • <b>{limits[plan]} MB/file</b>" if price > 0 else f"{PLAN_LABELS[plan]} — <b>Not configured</b>")
        if price > 0 and (cfg["upi_id"] or cashfree_configured()):
            buttons.append([InlineKeyboardButton(f"{PLAN_LABELS[plan]} • ₹{price} / {days}d", callback_data=f"mfp:buy:{plan}")])
    if not cfg["upi_id"]:
        lines.append("\n⚠️ UPI payment is currently not configured.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def _send_dynamic_upi_qr(message, plan: str, amount: int, currency: str, upi_id: str) -> None:
    """Generate a plan-specific UPI QR with the exact payable amount."""
    plan_label = PLAN_LABELS.get(plan, plan.title())
    upi_uri = (
        "upi://pay?"
        f"pa={quote_plus(upi_id)}&"
        f"pn={quote_plus('MediaFetch')}&"
        f"am={quote_plus(f'{amount:.2f}')}&"
        f"cu={quote_plus(currency)}&"
        f"tn={quote_plus(f'MediaFetch {plan_label}')}"
    )
    try:
        qr = qrcode.QRCode(version=None, box_size=10, border=4)
        qr.add_data(upi_uri)
        qr.make(fit=True)
        image = qr.make_image()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        buffer.name = f"mediafetch-{plan}-upi-qr.png"
        await message.reply_photo(
            photo=InputFile(buffer, filename=buffer.name),
            caption=(
                f"📲 <b>Scan to pay</b>\n"
                f"📦 {plan_label}\n"
                f"💰 <b>{amount} {currency}</b>\n"
                f"📱 UPI: <code>{html.escape(upi_id)}</code>\n\n"
                "QR me exact amount already set hai."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Dynamic UPI QR generation failed plan=%s amount=%s", plan, amount)

async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    await query.answer()
    if query.data == "mfp:plans":
        cfg = payment_config()
        limits = storage.file_limits()
        lines = ["💎 <b>MediaFetch Premium</b>", ""]
        rows = []
        for tier in PLANS:
            price = cfg["prices"][tier]
            days = cfg["durations"][tier]
            lines.append(f"{PLAN_LABELS[tier]} — ₹{price} / {days} days • {limits[tier]} MB/file")
            if price > 0 and cfg["upi_id"]:
                rows.append([InlineKeyboardButton(f"Buy {PLAN_LABELS[tier]} • ₹{price}", callback_data=f"mfp:buy:{tier}")])
        await query.edit_message_text("\\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        return
    if query.data == "mfp:status":
        info = await asyncio.to_thread(storage.plan_info, update.effective_user.id)
        limits = storage.file_limits()
        tier = info.get("plan", "free")
        used = await asyncio.to_thread(storage.usage_today, update.effective_user.id)
        if info.get("active"):
            from datetime import datetime, timezone
            until = datetime.fromtimestamp(float(info["until"]), tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            status_text = f"👤 <b>Your Plan</b>\\n\\n📦 {tier.title()} • {limits.get(tier, limits['free'])} MB/file\\n⏳ Expires: {until}\\n📥 Today: {used}/{storage.daily_limit(update.effective_user.id)}"
        else:
            status_text = f"👤 <b>Your Plan</b>\\n\\n🆓 Free • {limits['free']} MB/file\\n📥 Today: {used}/{settings.free_daily_limit}"
        await query.edit_message_text(status_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Premium Plans", callback_data="mfp:plans")]]))
        return
    if query.data == "mfp:supported":
        await query.edit_message_text("🌐 <b>Supported platforms</b>\\n\\n" + SUPPORTED_TEXT, parse_mode="HTML")
        return
    if query.data == "mfp:help":
        await query.edit_message_text("🛠 <b>How to use MediaFetch</b>\\n\\n1. Send a public media URL.\\n2. Choose quality.\\n3. Wait for download and upload.\\n\\nCommands: /start /help /supported /about /premium /plans /history", parse_mode="HTML")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "mfp" or parts[1] != "buy":
        return
    plan = parts[2].lower()
    cfg = payment_config()
    if plan not in PLANS or not cfg["upi_id"] or int(cfg["prices"].get(plan, 0)) <= 0:
        await query.edit_message_text("⚠️ This payment plan is not configured yet.")
        return
    user_id = update.effective_user.id
    if cashfree_configured():
        if query.message.chat.type != "private":
            await query.message.reply_text("Please open my private chat and choose the plan there to pay securely.")
            return
        _PENDING_CASHFREE_PLAN[user_id] = plan
        await query.message.reply_text(
            f"💳 <b>{PLAN_LABELS[plan]} • ₹{cfg['prices'][plan]}</b>\n\n"
            "Cashfree secure checkout ke liye apna Indian mobile number share karo.\n"
            "Number sirf payment gateway order create karne ke liye use hoga.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Share my number", request_contact=True)], [KeyboardButton("Cancel")]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    if not cfg["upi_id"]:
        await query.edit_message_text("⚠️ Payment is not configured. Owner ko Cashfree credentials ya UPI ID configure karni hogi.")
        return
    _PENDING_PAYMENT_PLAN[user_id] = plan
    text = (
        f"💳 <b>{PLAN_LABELS[plan]} Payment</b>\n\n"
        f"💰 Amount: <b>{cfg['prices'][plan]} {cfg['currency']}</b>\n"
        f"⏳ Duration: <b>{cfg['durations'][plan]} days</b>\n"
        f"📱 UPI ID: <code>{html.escape(cfg['upi_id'])}</code>\n\n"
        "1️⃣ UPI app se exact amount pay karo.\n"
        "2️⃣ Payment ke baad UTR / transaction reference copy karo.\n"
        "3️⃣ Neeche sirf UTR bhejo.\n\n"
        "⚠️ UTR submit karna payment proof nahi hai. Plan owner verification ke baad hi activate hoga."
    )
    await query.edit_message_text(text, parse_mode="HTML")
    await _send_dynamic_upi_qr(
        query.message,
        plan,
        int(cfg["prices"][plan]),
        cfg["currency"],
        cfg["upi_id"],
    )


async def cashfree_contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return
    if (message.text or "").strip().lower() == "cancel":
        _PENDING_CASHFREE_PLAN.pop(user.id, None)
        await message.reply_text("Payment cancelled.", reply_markup=ReplyKeyboardRemove())
        return
    plan = _PENDING_CASHFREE_PLAN.get(user.id)
    if not plan:
        return
    contact = message.contact
    if not contact or contact.user_id != user.id:
        await message.reply_text(
            "Please use the button to share your own phone number.",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Share my number", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True),
        )
        return
    digits = re.sub(r"\D", "", contact.phone_number or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        await message.reply_text(
            "Please share a valid Indian mobile number.",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Share my number", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True),
        )
        return
    cfg = payment_config()
    amount = int(cfg["prices"].get(plan, 0))
    if amount <= 0:
        _PENDING_CASHFREE_PLAN.pop(user.id, None)
        await message.reply_text("This plan is currently unavailable.", reply_markup=ReplyKeyboardRemove())
        return
    order_id = f"mf{user.id}{int(time.time())}{secrets.token_hex(4)}"
    status_message = await message.reply_text("🔐 Creating secure Cashfree checkout…", reply_markup=ReplyKeyboardRemove())
    try:
        order = await cashfree_create_order(order_id=order_id, amount=amount, user_id=user.id, phone=digits, plan=plan)
        await asyncio.to_thread(
            storage.create_payment,
            payment_id=secrets.token_hex(6).upper(),
            user_id=user.id,
            plan=plan,
            amount=amount,
            currency="INR",
            utr=f"CF-{order_id}",
            duration_days=int(cfg["durations"][plan]),
            provider="cashfree",
            gateway_order_id=order_id,
            payment_session_id=str(order["payment_session_id"]),
        )
        checkout_url = f"{cashfree_public_base_url()}/cashfree/checkout/{order_id}"
        await status_message.edit_text(
            f"💳 <b>{PLAN_LABELS[plan]} checkout ready</b>\n\n"
            f"💰 Amount: <b>₹{amount}</b>\n"
            f"⏳ Validity: <b>{cfg['durations'][plan]} days</b>\n\n"
            "Tap below to pay. Plan activates only after Cashfree confirms payment with our server.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Pay securely with Cashfree", url=checkout_url)]]),
        )
    except Exception as exc:
        logger.warning("Cashfree checkout creation failed user=%s error_type=%s", user.id, type(exc).__name__)
        await status_message.edit_text("⚠️ Secure checkout create nahi ho paya. Thodi der baad dobara try karo.")
    finally:
        _PENDING_CASHFREE_PLAN.pop(user.id, None)


async def _handle_payment_utr(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    plan = _PENDING_PAYMENT_PLAN.get(user_id)
    if not plan or not update.message or not update.message.text:
        return False
    utr = update.message.text.strip()
    if not valid_utr(utr):
        await update.message.reply_text("⚠️ Invalid UTR/reference. Please send the transaction reference only.")
        return True
    try:
        doc = await asyncio.to_thread(create_payment, user_id, plan, utr)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return True
    _PENDING_PAYMENT_PLAN.pop(user_id, None)
    cfg = payment_config()
    notify_admins = set(settings.admin_id_set)
    try:
        if settings.owner_id:
            notify_admins.add(int(str(settings.owner_id).strip()))
    except (TypeError, ValueError):
        pass
    for admin_id in notify_admins:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🟡 <b>New payment pending</b>\n\n"
                    f"🧾 ID: <code>{doc['payment_id']}</code>\n"
                    f"👤 User: <code>{user_id}</code>\n"
                    f"📦 Plan: <b>{PLAN_LABELS[plan]}</b>\n"
                    f"💰 Amount: <b>{doc['amount']} {doc['currency']}</b>\n"
                    f"🔢 UTR: <code>{html.escape(utr)}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Payment admin notification failed user=%s", user_id)

    # Optional configured group gets actionable approval buttons. Only OWNER_ID
    # can use them; the same payment remains available in the private owner panel.
    approval_chat_id = str(getattr(settings, "payment_approval_chat_id", "") or "").strip()
    if approval_chat_id:
        try:
            await context.bot.send_message(
                chat_id=int(approval_chat_id),
                text=(
                    f"🟡 <b>Premium payment approval required</b>\n\n"
                    f"🧾 ID: <code>{doc['payment_id']}</code>\n"
                    f"👤 User: <code>{user_id}</code>\n"
                    f"📦 Plan: <b>{PLAN_LABELS[plan]}</b>\n"
                    f"💰 Amount: <b>{doc['amount']} {doc['currency']}</b>\n"
                    f"🔢 UTR: <code>{html.escape(utr)}</code>\n\n"
                    "⚠️ Owner: bank/UPI app mein amount aur UTR verify karke hi action karein."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"mfa:payapprove:{doc['payment_id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"mfa:payreject:{doc['payment_id']}"),
                ]]),
            )
        except Exception as exc:
            logger.warning("Payment approval group notification failed user=%s error_type=%s", user_id, type(exc).__name__)

    await update.message.reply_text(
        f"✅ <b>Payment submitted</b>\n\n"
        f"🧾 ID: <code>{doc['payment_id']}</code>\n"
        f"📦 Plan: <b>{PLAN_LABELS[plan]}</b>\n"
        f"💰 Amount: <b>{doc['amount']} {doc['currency']}</b>\n"
        f"🔢 UTR: <code>{html.escape(utr)}</code>\n\n"
        "🕒 Status: <b>Pending verification</b>\n"
        "Aapka plan owner payment verify karne ke baad activate karega."
    , parse_mode="HTML")
    return True


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    if admin_has_pending_action(user_id):
        await admin_message_router(update, context)
        return
    if await _handle_payment_utr(update, context, user_id):
        return

    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    username = update.effective_user.username if update.effective_user else None
    await asyncio.to_thread(storage.touch_user, user_id, username)

    if await asyncio.to_thread(storage.maintenance) and not _is_admin(user_id):
        await update.message.reply_text("🔧 MediaFetch is temporarily under maintenance. Please try again later.")
        return

    if not await _RATE_LIMITER.allow(user_id):
        await update.message.reply_text("⏱️ Please wait a few seconds before sending another link.")
        return

    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "🔗 Send a valid public http/https media URL.\nTry /supported to see supported platforms."
        )
        return

    url = match.group(0).rstrip(".,!?)]}")
    platform = detect_platform(url)
    if platform == "Unknown":
        await update.message.reply_text(
            "⚠️ This platform is not in the supported list yet. Use /supported."
        )
        return

    link_log_message = await _log_link(
        context, user_id, username, platform, url, "⏳ Inspecting"
    )

    used = await asyncio.to_thread(storage.usage_today, user_id)
    limit = _limit_for(user_id)
    if limit > 0 and used >= limit:
        await update.message.reply_text(
            f"🚦 Daily limit reached ({limit}).\n"
            "Premium users have a higher daily limit."
        )
        return

    async with _ACTIVE_LOCK:
        # _ACTIVE_JOBS is the single source of truth for running downloads.
        # Do not use a separate _ACTIVE_USERS set here; that caused a runtime
        # NameError and prevented every new URL from reaching inspection.
        if any(job_user_id == user_id for job_user_id, _request_id in _ACTIVE_JOBS):
            await update.message.reply_text(
                "⏳ You already have a download running. Please cancel it with ❌ "
                "or wait for it to finish before starting another task."
            )
            return

    request_id = secrets.token_hex(4)
    async with _PENDING_LOCK:
        if user_id in _PENDING_REQUESTS:
            await update.message.reply_text(
                "⌛ You already have a link being inspected. Please wait for the quality buttons."
            )
            return
        _PENDING_REQUESTS[user_id] = (request_id, url, platform, None, username, link_log_message)

    status = await update.message.reply_text("🔎 Inspecting media…")
    try:
        info = await asyncio.wait_for(get_media_info(url), timeout=45)
    except asyncio.TimeoutError:
        async with _PENDING_LOCK:
            current = _PENDING_REQUESTS.get(user_id)
            if current and current[0] == request_id:
                _PENDING_REQUESTS.pop(user_id, None)

        # Never let link-log I/O block the user's Telegram response.
        timeout_text = (
            "⏱️ YouTube inspection timed out while the source was being checked.\n"
            "Please try again in a little while."
            if platform == "YouTube"
            else
            "⏱️ Media inspection timed out after 45 seconds. "
            "The source may be slow, restricted, or temporarily unavailable. Please try again."
        )
        try:
            await status.edit_text(timeout_text)
        except Exception:
            try:
                await update.message.reply_text(timeout_text)
            except Exception:
                logger.exception("Failed to send inspection-timeout response")
        try:
            await _update_link_log(
                context, link_log_message, user_id, username, platform, url, "❌ Inspection timeout"
            )
        except Exception:
            logger.exception("Inspection-timeout link log update failed")
        return
    except DownloadError as exc:
        logger.warning("Media inspection failed user=%s platform=%s url=%s error=%s", user_id, platform, url, exc)
        async with _PENDING_LOCK:
            current = _PENDING_REQUESTS.get(user_id)
            if current and current[0] == request_id:
                _PENDING_REQUESTS.pop(user_id, None)

        error_lower = str(exc).lower()
        if platform == "YouTube" and (
            "sign in to confirm" in error_lower
            or "not a bot" in error_lower
            or "page needs to be reloaded" in error_lower
        ):
            failure_text = (
                "⚠️ <b>YouTube is temporarily blocking this server.</b>\n\n"
                "The video itself may be public, but YouTube is rejecting requests "
                "from the current cloud IP. Please try again later."
            )
        elif platform == "YouTube" and "requested format is not available" in error_lower:
            failure_text = (
                "⚠️ <b>YouTube did not return a downloadable format.</b>\n\n"
                "Please try another quality or try the link again shortly."
            )
        else:
            failure_text = (
                "❌ I couldn't inspect this URL. It may be private, restricted, "
                "rate-limited, or temporarily unavailable."
            )

        # Response first; logging must never be able to swallow the Telegram reply.
        try:
            await status.edit_text(failure_text, parse_mode="HTML")
        except Exception:
            try:
                await update.message.reply_text(failure_text, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send media-inspection failure response")
        try:
            await _update_link_log(
                context, link_log_message, user_id, username, platform, url, "❌ Inspection failed"
            )
        except Exception:
            logger.exception("Inspection-failure link log update failed")
        return

    await _update_link_log(
        context, link_log_message, user_id, username, platform, url, "🔎 Inspected"
    )

    async with _PENDING_LOCK:
        current = _PENDING_REQUESTS.get(user_id)
        if not current or current[0] != request_id:
            await status.edit_text("⌛ This request expired. Please send the URL again.")
            return
        _PENDING_REQUESTS[user_id] = (request_id, url, platform, info, username, link_log_message)

    title = info.title[:80]
    details = [f"🔎 <b>{platform}</b>", f"🎬 <b>{title}</b>"]
    if info.duration_text:
        details.append(f"⏱️ {info.duration_text}")
    if info.item_count > 1:
        details.append(f"🖼️ {info.item_count} items")
    if info.heights:
        details.append("📐 " + ", ".join(f"{height}p" for height in info.heights[:6]))

    await status.edit_text(
        "\n".join(details) + "\n\n<b>Choose download mode:</b>",
        reply_markup=_quality_keyboard(info, request_id),
        parse_mode="HTML",
    )


async def download_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    parts = query.data.split(":") if query.data else []
    if len(parts) != 3 or parts[0] != "mf":
        await query.answer()
        await query.edit_message_text("⌛ This request is invalid. Please send the URL again.")
        return

    request_id, mode = parts[1], parts[2]
    user_id = update.effective_user.id if update.effective_user else query.message.chat_id

    if mode == "cancel":
        await query.answer("Cancelling…")
        async with _ACTIVE_LOCK:
            job = _ACTIVE_JOBS.get((user_id, request_id))
        if job:
            task, cancel_event = job
            # Signal the blocking downloader/FFmpeg work first. The worker also
            # owns the Bot API upload, so cancel the asyncio task after the
            # cooperative signal to interrupt an in-flight upload request.
            cancel_event.set()
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # The download layer now checks cancel_event inside direct HTTP
                # reads and FFmpeg normalization. If a third-party extractor is
                # still unwinding, its temporary file is cleaned by the worker.
                pass
            except Exception:
                logger.exception("Cancelled media task cleanup failed")
            try:
                await query.edit_message_text(
                    "❌ <b>Task cancelled.</b>\n"
                    "You can start another task now.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        async with _PENDING_LOCK:
            pending = _PENDING_REQUESTS.get(user_id)
            if pending and pending[0] == request_id:
                _PENDING_REQUESTS.pop(user_id, None)
                pending = True
            else:
                pending = False
        await query.edit_message_text(
            "❌ <b>Download cancelled.</b>" if pending else "⌛ This task is already finished.",
            parse_mode="HTML",
        )
        return

    await query.answer()
    context.application.create_task(
        _download_choice_worker(update, context),
        update=update,
        name=f"mediafetch-download-{user_id}-{request_id}",
    )


async def _download_choice_worker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    parts = query.data.split(":") if query.data else []
    if len(parts) != 3 or parts[0] != "mf":
        await query.edit_message_text("⌛ This request is invalid. Please send the URL again.")
        return
    request_id, mode = parts[1], parts[2]

    user_id = update.effective_user.id if update.effective_user else query.message.chat_id
    # Safe defaults ensure a downloader exception cannot be masked by logging.
    username = update.effective_user.username if update.effective_user else None
    link_log_message = None
    async with _PENDING_LOCK:
        pending = _PENDING_REQUESTS.get(user_id)
        if not pending or pending[0] != request_id:
            pending = None
        else:
            _PENDING_REQUESTS.pop(user_id, None)

    if not pending:
        await query.edit_message_text("⌛ This request expired. Please send the URL again.")
        return

    _, url, platform, info, username, link_log_message = pending
    if mode == "cancel":
        await query.edit_message_text("❌ Download cancelled.")
        return

    limit = _limit_for(user_id)
    used = await asyncio.to_thread(storage.usage_today, user_id)
    if limit > 0 and used >= limit:
        await query.edit_message_text(f"🚦 Daily limit reached ({limit}).")
        return

    max_file_mb = _file_limit_mb(user_id)

    estimated_bytes = _estimated_size_for_mode(info, mode) if isinstance(info, MediaInfo) else 0
    if max_file_mb > 0 and estimated_bytes > max_file_mb * 1024 * 1024:
        await query.edit_message_text(
            _limit_message(user_id, estimated_bytes, max_file_mb),
            parse_mode="HTML",
        )
        return

    cancel_event = threading.Event()
    current_task = asyncio.current_task()
    if current_task is None:
        await query.edit_message_text("❌ Unable to start the download task.")
        return
    async with _ACTIVE_LOCK:
        _ACTIVE_JOBS[(user_id, request_id)] = (current_task, cancel_event)

    labels = {
        "best": "Best quality",
        "2160p": "2160p",
        "1440p": "1440p",
        "4320p": "4320p",
        "2160p": "2160p",
        "1440p": "1440p",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p",
        "audio": "MP3 audio",
        "photo": "HD / Original photo",
    }
    label = labels.get(mode, mode)
    status = None
    path: Path | list[Path] | None = None
    cache_hit = False

    try:
        cache = await asyncio.to_thread(
            storage.get_cache,
            _cache_key(url, mode),
            settings.cache_ttl_days * 86400,
        )
        cached_size = int((cache or {}).get("metadata", {}).get("size_bytes", 0) or 0)
        if cache and cache.get("file_ids") and (not cached_size or max_file_mb == 0 or cached_size <= max_file_mb * 1024 * 1024):
            try:
                cache_hit = True
                await query.edit_message_text("⚡ <b>Cache hit</b> — sending instantly…", parse_mode="HTML")
                metadata = cache.get("metadata", {}) or {}
                cached_kinds = metadata.get("media_kinds") or []
                cached_ids = cache["file_ids"]

                if cached_ids and cached_kinds and all(
                    kind in {"photo", "video"} for kind in cached_kinds
                ) and len(cached_ids) > 1:
                    await _send_media_album(
                        query.message,
                        file_ids=cached_ids,
                        kinds=cached_kinds,
                        caption=_post_caption(info, platform, label, len(cached_ids)),
                    )
                else:
                    for index, file_id in enumerate(cached_ids, start=1):
                        kind = (
                            cached_kinds[index - 1]
                            if index - 1 < len(cached_kinds)
                            else metadata.get("media_kind", "document")
                        )
                        await _send_media_message(
                            query.message,
                            file_id=file_id,
                            kind=kind,
                            caption=_post_caption(info, platform, label, len(cached_ids))
                            if index == 1 else "",
                        )
                await asyncio.to_thread(storage.increment_usage, user_id)
                await asyncio.to_thread(storage.record_event, user_id, platform, True, 0, True)
                await asyncio.to_thread(
                    storage.record_history,
                    user_id, platform, url,
                    (info.title if isinstance(info, MediaInfo) else "Media"),
                    mode, True, cached_size,
                )
                return
            except Exception:
                await asyncio.to_thread(storage.delete_cache, _cache_key(url, mode))
                cache_hit = False

        status = await query.edit_message_text(
            f"🔎 <b>Platform:</b> {platform}\n"
            f"🎯 <b>Mode:</b> {label}\n"
            "⏬ <b>Download:</b> <code>[░░░░░░░░░░░░] 0.0%</code>\n"
            "⚡ preparing…",
            reply_markup=_cancel_keyboard(request_id),
            parse_mode="HTML",
        )
        await query.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

        last_text = ""
        last_progress_edit = 0.0

        def progress_bar(percent: float, width: int = 12) -> str:
            percent = max(0.0, min(100.0, percent))
            filled = int(round(percent / 100 * width))
            return "█" * filled + "░" * (width - filled)

        async def progress(percent: float, detail: str) -> None:
            nonlocal last_text, last_progress_edit
            now = asyncio.get_running_loop().time()
            # Telegram message edits are throttled so fast downloads do not
            # hit Bot API edit limits.
            if percent < 100 and now - last_progress_edit < 1.5:
                return

            bar = progress_bar(percent)
            text = (
                f"🔎 <b>Platform:</b> {platform}\n"
                f"🎯 <b>Mode:</b> {label}\n"
                f"⏬ <b>Download:</b> <code>[{bar}] {percent:5.1f}%</code>\n"
                f"⚡ {detail}"
            )
            if text == last_text or status is None:
                return
            last_text = text
            last_progress_edit = now
            try:
                await status.edit_text(
                    text,
                    reply_markup=_cancel_keyboard(request_id),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        await _DOWNLOAD_LIMITER.acquire()
        try:
            await status.edit_text(
                f"🔎 <b>Platform:</b> {platform}\n"
                f"🎯 <b>Mode:</b> {label}\n"
                "⏬ <b>Download:</b> <code>[░░░░░░░░░░░░] 0.0%</code>\n"
                "⚡ downloading…",
                reply_markup=_cancel_keyboard(request_id),
                parse_mode="HTML",
            )
            path = await download_media(
                url,
                settings.download_dir,
                mode=mode,
                max_file_mb=max_file_mb,
                progress_callback=progress,
                cancel_event=cancel_event,
            )
        finally:
            await _DOWNLOAD_LIMITER.release()

        paths = path if isinstance(path, list) else [path]
        size_bytes = sum(item.stat().st_size for item in paths)
        max_bytes = max_file_mb * 1024 * 1024 if max_file_mb > 0 else 0
        if max_file_mb > 0 and any(item.stat().st_size > max_bytes for item in paths):
            await status.edit_text(
                f"⚠️ <b>File exceeds your {_limit_label(user_id)} limit.</b>\n\n"
                "Choose a lower quality or upgrade the plan.",
                parse_mode="HTML",
            )
            await asyncio.to_thread(storage.record_event, user_id, platform, False, size_bytes)
            return

        # Keep plan limits separate from Telegram transport limits.
        # Cloud Bot API <=50 MB; Local Bot API <=2000 MB when configured;
        # otherwise the MTProto user session handles larger files.
        bot_api_limit_mb = (
            int(settings.local_bot_api_max_upload_mb)
            if settings.telegram_api_base_url
            else 50
        )
        use_mtproto = any(
            item.stat().st_size > bot_api_limit_mb * 1024 * 1024
            for item in paths
        )
        if use_mtproto and not mtproto_uploader.ready:
            await mtproto_uploader.start()
        if use_mtproto and not mtproto_uploader.ready:
            raise DownloadError(
                "This file is above the configured Bot API transport limit, "
                "and the MTProto user-session uploader is unavailable."
            )

        async def upload_progress(percent: float, detail: str) -> None:
            if cancel_event.is_set():
                return
            nonlocal last_text, last_progress_edit
            now = asyncio.get_running_loop().time()
            if percent < 100 and now - last_progress_edit < 0.8:
                return
            bar = progress_bar(percent)
            text = (
                f"📤 <b>Telegram Upload</b> • {platform}\n"
                f"🎯 <b>Mode:</b> {label}\n"
                f"⏫ <code>[{bar}] {percent:5.1f}%</code>\n"
                f"⚡ {detail}"
            )
            if text == last_text or status is None:
                return
            last_text = text
            last_progress_edit = now
            try:
                await status.edit_text(
                    text,
                    reply_markup=_cancel_keyboard(request_id),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        file_ids: list[str] = []
        sent_messages_for_dump: list = []
        try:
            photo_paths = [
                item for item in paths
                if _media_kind(item) == "photo" and item.stat().st_size <= 10 * 1024 * 1024
            ]

            media_kinds = [_media_kind(item) for item in paths]
            can_album = (
                len(paths) > 1
                and all(kind in {"photo", "video"} for kind in media_kinds)
                and all(item.stat().st_size <= bot_api_limit_mb * 1024 * 1024 for item in paths)
            )
            if can_album:
                sent_messages = await _send_media_album(
                    query.message,
                    paths=paths,
                    kinds=media_kinds,
                    caption=_post_caption(info, platform, label, len(paths)),
                )
                sent_messages_for_dump.extend(sent_messages)
                for sent in sent_messages:
                    if sent.photo:
                        file_ids.append(sent.photo[-1].file_id)
                    elif sent.video:
                        file_ids.append(sent.video.file_id)
            else:
                for index, item in enumerate(paths, start=1):
                    caption = _post_caption(
                        info, platform, label, len(paths)
                    ) if index == 1 else ""
                    kind = _media_kind(item)

                    if use_mtproto and item.stat().st_size > bot_api_limit_mb * 1024 * 1024:
                        async def mt_progress(percent: float, detail: str) -> None:
                            nonlocal last_text, last_progress_edit
                            now = asyncio.get_running_loop().time()
                            if percent < 100 and now - last_progress_edit < 1.0:
                                return
                            bar = progress_bar(percent)
                            text = (
                                f"📤 <b>Telegram Upload</b> • {platform}\n"
                                f"🎯 <b>Mode:</b> {label}\n"
                                f"⏫ <code>[{bar}] {percent:5.1f}%</code>\n"
                                f"⚡ {detail}"
                            )
                            if text == last_text or status is None:
                                return
                            last_text = text
                            last_progress_edit = now
                            try:
                                await status.edit_text(
                                    text,
                                    reply_markup=_cancel_keyboard(request_id),
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass

                        large_dump_channel = (
                            storage.channel_config().get("dump")
                            or settings.dump_channel_id
                            or None
                        )
                        try:
                            sent_large = await mtproto_uploader.send_file(
                                item,
                                target_chat_id=query.message.chat_id,
                                caption=caption,
                                progress_callback=mt_progress,
                                reply_to_message_id=query.message.message_id,
                                dump_channel_id=large_dump_channel,
                            )
                        except LargeUploadError as exc:
                            raise DownloadError(f"Large Telegram upload failed: {exc}") from exc

                        # MTProto uploads the large file to the bridge/dump
                        # channel. The Bot API then copies it from there into
                        # the user's PM. This avoids PEER_ID_INVALID when the
                        # MTProto user session has never met the requesting
                        # user's peer. Telegram's copyMessage is server-side,
                        # so the large file does not need to be re-uploaded.
                        if large_dump_channel:
                            # MTProto has already uploaded the large file into
                            # the bridge channel. The Bot API can copy that
                            # server-side message to the user's PM without
                            # re-uploading the file.
                            for bridge_message in sent_large:
                                copied = await context.bot.copy_message(
                                    chat_id=query.message.chat_id,
                                    from_chat_id=large_dump_channel,
                                    message_id=bridge_message.id,
                                    reply_to_message_id=(
                                        query.message.message_id
                                        if not file_ids
                                        else None
                                    ),
                                )
                                # Bot API copyMessage returns MessageId rather
                                # than the copied Message object. Large bridge
                                # uploads are therefore intentionally not added
                                # to the normal file_id cache here.
                            # Do not run _send_media_message() below: that
                            # would upload the same >50 MB file a second time.
                            continue

                        # No bridge channel configured: MTProto delivered the
                        # file directly to the target chat.
                        logger.info(
                            "Large file delivered directly by MTProto user session user=%s",
                            user_id,
                        )
                        continue

                    await upload_progress(
                        0,
                        f"0.0/{item.stat().st_size / (1024 * 1024):.1f} MB • preparing…",
                    )
                    sent = await _send_media_message(
                        query.message,
                        path=item,
                        kind=kind,
                        caption=caption,
                        upload_progress=upload_progress,
                        cancel_event=cancel_event,
                        thumbnail_url=(info.thumbnail if isinstance(info, MediaInfo) and platform == "YouTube" else None),
                        source_url=(url if platform == "YouTube" else None),
                    )
                    await upload_progress(
                        100,
                        f"{item.stat().st_size / (1024 * 1024):.1f}/"
                        f"{item.stat().st_size / (1024 * 1024):.1f} MB • complete",
                    )
                    sent_messages_for_dump.append(sent)
                    if kind == "video" and sent.video:
                        file_ids.append(sent.video.file_id)
                    elif kind == "photo" and sent.photo:
                        file_ids.append(sent.photo[-1].file_id)
                    elif sent.document:
                        file_ids.append(sent.document.file_id)
        finally:
            pass

        media_kinds = [_media_kind(item) for item in paths]
        if file_ids:
            metadata = {
                "title": info.title if isinstance(info, MediaInfo) else "Media",
                "platform": platform,
                "size_bytes": size_bytes,
                "media_kind": media_kinds[0] if len(set(media_kinds)) == 1 else "document",
                "media_kinds": media_kinds,
                "transport": "bot_api",
            }
            await asyncio.to_thread(storage.set_cache, _cache_key(url, mode), file_ids, metadata)

        await asyncio.to_thread(storage.increment_usage, user_id)
        await asyncio.to_thread(storage.record_event, user_id, platform, True, size_bytes, cache_hit)
        await asyncio.to_thread(
            storage.record_history, user_id, platform, url,
            (info.title if isinstance(info, MediaInfo) else "Media"),
            mode, True, size_bytes,
        )
        await _dump_messages(context, sent_messages_for_dump, user_id, username, platform, url)
        await _update_link_log(
            context, link_log_message, user_id, username, platform, url,
            f"✅ Downloaded • {mode} • {size_bytes / (1024 * 1024):.1f} MB"
        )
        await status.delete()
    except asyncio.CancelledError:
        logger.info("Media task cancelled user=%s platform=%s mode=%s request_id=%s", user_id, platform, mode, request_id)
        await _update_link_log(
            context, link_log_message, user_id, username, platform, url, "❌ Cancelled"
        )
        if status:
            try:
                await status.edit_text("❌ <b>Download/upload cancelled.</b>", parse_mode="HTML")
            except Exception:
                pass
        raise
    except DownloadError as exc:
        logger.warning("Download failed user=%s platform=%s mode=%s error=%s", user_id, platform, mode, exc)
        await asyncio.to_thread(storage.record_event, user_id, platform, False, 0, cache_hit)
        await asyncio.to_thread(
            storage.record_history, user_id, platform, url,
            (info.title if isinstance(info, MediaInfo) else "Media"),
            mode, False, 0,
        )
        await _update_link_log(context, link_log_message, user_id, username, platform, url, "❌ Download failed")
        if status:
            await status.edit_text(
                "❌ <b>Download failed.</b>\n"
                "The source may be private, restricted, unsupported, or temporarily unavailable.",
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("Unexpected download handler failure user=%s platform=%s mode=%s", user_id, platform, mode)
        await asyncio.to_thread(storage.record_event, user_id, platform, False, 0, cache_hit)
        if status:
            await status.edit_text("❌ Something went wrong while processing that link. Please try again.")
    finally:
        if path:
            paths_to_remove = path if isinstance(path, list) else [path]
            for item in paths_to_remove:
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass
        async with _ACTIVE_LOCK:
            _ACTIVE_JOBS.pop((user_id, request_id), None)
