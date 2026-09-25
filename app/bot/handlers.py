from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from pathlib import Path

from telegram.error import BadRequest
from telegram import InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.core.config import settings
from app.core.rate_limit import UserRateLimiter
from app.core.storage import storage
from app.downloader.detector import detect_platform
from app.downloader.service import DownloadError, MediaInfo, download_media, get_media_info

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ACTIVE_USERS: set[int] = set()
_ACTIVE_LOCK = asyncio.Lock()
_PENDING_REQUESTS: dict[int, tuple[str, str, str, MediaInfo | None]] = {}
_PENDING_LOCK = asyncio.Lock()
_DOWNLOAD_SLOTS = asyncio.Semaphore(settings.max_concurrent_downloads)
_RATE_LIMITER = UserRateLimiter(min_interval=3.0)
logger = logging.getLogger(__name__)

SUPPORTED_TEXT = (
    "YouTube • Instagram • Facebook • Reddit • X/Twitter • "
    "TikTok • Pinterest • Threads"
)


def _cache_key(url: str, mode: str) -> str:
    # v2 invalidates older document-only cache entries so native video/photo
    # Telegram media types are regenerated after the media-send fix.
    return hashlib.sha256(f"v2|{url}|{mode}".encode("utf-8")).hexdigest()


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set


def _file_limit_mb(user_id: int) -> int:
    limits = storage.file_limits()
    if _is_admin(user_id):
        configured = limits["admin"]
    elif storage.is_premium(user_id):
        configured = limits["premium"]
    else:
        configured = limits["free"]

    # Official cloud Bot API uploads are limited to 50 MB. A Local Bot API
    # Server raises the upload ceiling to 2000 MB.
    if not settings.telegram_api_base_url:
        return min(configured, 50)
    return min(configured, 2000)


def _limit_label(user_id: int) -> str:
    limits = storage.file_limits()
    if _is_admin(user_id):
        return f"{limits['admin']} MB (Admin)"
    if storage.is_premium(user_id):
        return f"{limits['premium']} MB (Premium)"
    return f"{limits['free']} MB (Free)"


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
    if _is_admin(user_id):
        return (
            f"⚠️ This file is estimated at <b>{estimated_mb:.1f} MB</b>, "
            f"which is above the configured Admin limit of <b>{limit_mb} MB</b>."
        )
    if storage.is_premium(user_id):
        return (
            f"⚠️ This file is estimated at <b>{estimated_mb:.1f} MB</b>, "
            f"which is above your Premium limit of <b>{limit_mb} MB</b>."
        )
    return (
        f"📦 <b>File too large for Free users.</b>\n\n"
        f"Estimated size: <b>{estimated_mb:.1f} MB</b>\n"
        f"Free limit: <b>{storage.file_limits()['free']} MB</b>\n\n"
        "💎 <b>Premium required</b> for larger downloads.\n"
        "Use /premium to check your Premium status."
    )


def _limit_for(user_id: int) -> int:
    return settings.premium_daily_limit if storage.is_premium(user_id) else settings.free_daily_limit


def _media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp4":
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    return "document"


async def _send_media_message(message, path: Path | None = None, file_id: str | None = None,
                              kind: str = "document", caption: str = ""):
    if file_id:
        if kind == "video":
            return await message.reply_video(video=file_id, caption=caption, supports_streaming=True)
        if kind == "photo":
            return await message.reply_photo(photo=file_id, caption=caption)
        return await message.reply_document(document=file_id, caption=caption)

    if path is None:
        raise ValueError("path or file_id is required")

    with path.open("rb") as media:
        if kind == "video":
            return await message.reply_video(
                video=media,
                caption=caption,
                supports_streaming=True,
            )
        if kind == "photo" and path.stat().st_size <= 10 * 1024 * 1024:
            return await message.reply_photo(photo=media, caption=caption)
        return await message.reply_document(document=media, caption=caption)


def _normalize_telegram_photo(path: Path) -> Path:
    """Decode image bytes and rewrite them as a standard RGB JPEG Telegram accepts."""
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
        image.save(normalized, format="JPEG", quality=95, optimize=True)
    logger.info("Normalized Telegram photo source=%s output=%s size=%d", path.name, normalized.name, normalized.stat().st_size)
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
                )
            )
        return await message.reply_media_group(media=media)

    if not paths:
        raise ValueError("paths or file_ids are required")

    if len(paths) == 1:
        path = paths[0]
        try:
            with path.open("rb") as photo:
                return [await message.reply_photo(photo=photo, caption=caption)]
        except BadRequest as exc:
            if "image_process_failed" not in str(exc).lower():
                raise
            logger.warning("Telegram rejected original photo; normalizing path=%s size=%d", path.name, path.stat().st_size)
            normalized = await asyncio.to_thread(_normalize_telegram_photo, path)
            with normalized.open("rb") as photo:
                return [await message.reply_photo(photo=photo, caption=caption)]

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
                )
            )
        return await message.reply_media_group(media=media)


def _quality_keyboard(info: MediaInfo, request_id: str) -> InlineKeyboardMarkup:
    if info.is_photo:
        rows = [[InlineKeyboardButton("📸 HD / Original", callback_data=f"mf:{request_id}:photo")]]
    else:
        rows = [[
            InlineKeyboardButton("🎬 Best", callback_data=f"mf:{request_id}:best"),
            InlineKeyboardButton("🎵 MP3", callback_data=f"mf:{request_id}:audio"),
        ]]
        max_height = max(info.heights, default=0)
        standards = [2160, 1440, 1080, 720, 480, 360]
        available = [height for height in standards if height <= max_height]
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
    await update.message.reply_text(
        "👋 <b>Welcome to MediaFetch!</b>\n\n"
        "Send a public media URL and choose the quality.\n"
        "🎬 Video • 🎵 MP3 • 📸 HD photos • 🖼️ carousels\n"
        f"📦 Free limit: <b>{storage.file_limits()['free']} MB</b> • Premium: <b>{storage.file_limits()['premium']} MB</b>\n\n"
        "Use /help for commands.",
        parse_mode="HTML",
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
    until = await asyncio.to_thread(storage.premium_until, user_id)
    if until > __import__("time").time():
        from datetime import datetime, timezone
        date = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(
            f"💎 <b>Premium active</b>\nUntil: <b>{date}</b>\n"
            f"Daily limit: <b>{settings.premium_daily_limit}</b>",
            parse_mode="HTML",
        )
    else:
        used = await asyncio.to_thread(storage.usage_today, user_id)
        await update.message.reply_text(
            f"🆓 <b>Free plan</b>\nToday: {used}/{settings.free_daily_limit} downloads.\n"
            "Premium access is currently managed by the bot admin.",
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
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    username = update.effective_user.username if update.effective_user else None
    await asyncio.to_thread(storage.touch_user, user_id, username)

    if await asyncio.to_thread(storage.maintenance) and user_id not in settings.admin_id_set:
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

    used = await asyncio.to_thread(storage.usage_today, user_id)
    limit = _limit_for(user_id)
    if used >= limit:
        await update.message.reply_text(
            f"🚦 Daily limit reached ({limit}).\n"
            "Premium users have a higher daily limit."
        )
        return

    async with _ACTIVE_LOCK:
        if user_id in _ACTIVE_USERS:
            await update.message.reply_text(
                "⏳ You already have a download running. Please wait for it to finish."
            )
            return

    request_id = secrets.token_hex(4)
    async with _PENDING_LOCK:
        if user_id in _PENDING_REQUESTS:
            await update.message.reply_text(
                "⌛ You already have a link being inspected. Please wait for the quality buttons."
            )
            return
        _PENDING_REQUESTS[user_id] = (request_id, url, platform, None)

    status = await update.message.reply_text("🔎 Inspecting media…")
    try:
        info = await asyncio.wait_for(get_media_info(url), timeout=60)
    except asyncio.TimeoutError:
        async with _PENDING_LOCK:
            current = _PENDING_REQUESTS.get(user_id)
            if current and current[0] == request_id:
                _PENDING_REQUESTS.pop(user_id, None)
        await status.edit_text(
            "⏱️ Media inspection timed out after 60 seconds. "
            "The source may be slow, restricted, or temporarily unavailable. Please try again."
        )
        return
    except DownloadError as exc:
        logger.warning("Media inspection failed user=%s platform=%s url=%s error=%s", user_id, platform, url, exc)
        async with _PENDING_LOCK:
            current = _PENDING_REQUESTS.get(user_id)
            if current and current[0] == request_id:
                _PENDING_REQUESTS.pop(user_id, None)
        await status.edit_text(
            "❌ I couldn't inspect this URL. It may be private, restricted, "
            "rate-limited, or temporarily unavailable."
        )
        return

    async with _PENDING_LOCK:
        current = _PENDING_REQUESTS.get(user_id)
        if not current or current[0] != request_id:
            await status.edit_text("⌛ This request expired. Please send the URL again.")
            return
        _PENDING_REQUESTS[user_id] = (request_id, url, platform, info)

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

    await query.answer()
    parts = query.data.split(":") if query.data else []
    if len(parts) != 3 or parts[0] != "mf":
        await query.edit_message_text("⌛ This request is invalid. Please send the URL again.")
        return
    request_id, mode = parts[1], parts[2]

    user_id = update.effective_user.id if update.effective_user else query.message.chat_id
    async with _PENDING_LOCK:
        pending = _PENDING_REQUESTS.get(user_id)
        if not pending or pending[0] != request_id:
            pending = None
        else:
            _PENDING_REQUESTS.pop(user_id, None)

    if not pending:
        await query.edit_message_text("⌛ This request expired. Please send the URL again.")
        return

    _, url, platform, info = pending
    if mode == "cancel":
        await query.edit_message_text("❌ Download cancelled.")
        return

    limit = _limit_for(user_id)
    used = await asyncio.to_thread(storage.usage_today, user_id)
    if used >= limit:
        await query.edit_message_text(f"🚦 Daily limit reached ({limit}).")
        return

    max_file_mb = _file_limit_mb(user_id)

    estimated_bytes = _estimated_size_for_mode(info, mode) if isinstance(info, MediaInfo) else 0
    if estimated_bytes > max_file_mb * 1024 * 1024:
        await query.edit_message_text(
            _limit_message(user_id, estimated_bytes, max_file_mb),
            parse_mode="HTML",
        )
        return

    async with _ACTIVE_LOCK:
        if user_id in _ACTIVE_USERS:
            await query.edit_message_text(
                "⏳ You already have a download running. Please wait for it to finish."
            )
            return
        _ACTIVE_USERS.add(user_id)

    labels = {
        "best": "Best quality",
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
        if cache and cache.get("file_ids") and (not cached_size or cached_size <= max_file_mb * 1024 * 1024):
            try:
                cache_hit = True
                await query.edit_message_text("⚡ <b>Cache hit</b> — sending instantly…", parse_mode="HTML")
                metadata = cache.get("metadata", {}) or {}
                cached_kinds = metadata.get("media_kinds") or []
                cached_ids = cache["file_ids"]

                if cached_ids and cached_kinds and all(kind == "photo" for kind in cached_kinds):
                    title = str(metadata.get("title") or "Media")[:80]
                    caption = f"⚡ {platform} • {label}\n🎬 {title}\n📸 {len(cached_ids)} photos"
                    await _send_photo_album(
                        query.message,
                        file_ids=cached_ids,
                        caption=caption,
                    )
                else:
                    for index, file_id in enumerate(cached_ids, start=1):
                        caption = (
                            f"⚡ Cached • {platform} • {label}"
                            if len(cached_ids) == 1
                            else f"⚡ Cached • {platform} • Photo {index}/{len(cached_ids)}"
                        )
                        kind = (
                            cached_kinds[index - 1]
                            if index - 1 < len(cached_kinds)
                            else metadata.get("media_kind", "document")
                        )
                        await _send_media_message(
                            query.message,
                            file_id=file_id,
                            kind=kind,
                            caption=caption,
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
            f"⏬ <b>Progress:</b> starting…",
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
                await status.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        await _DOWNLOAD_SLOTS.acquire()
        try:
            await status.edit_text(
                f"🔎 <b>Platform:</b> {platform}\n"
                f"🎯 <b>Mode:</b> {label}\n"
                "⏬ <b>Progress:</b> downloading…",
                parse_mode="HTML",
            )
            path = await download_media(
                url,
                settings.download_dir,
                mode=mode,
                max_file_mb=max_file_mb,
                progress_callback=progress,
            )
        finally:
            _DOWNLOAD_SLOTS.release()

        paths = path if isinstance(path, list) else [path]
        size_bytes = sum(item.stat().st_size for item in paths)
        max_bytes = max_file_mb * 1024 * 1024
        if any(item.stat().st_size > max_bytes for item in paths):
            if not _is_admin(user_id) and not storage.is_premium(user_id):
                await status.edit_text(
                    f"📦 <b>File is larger than the Free limit ({storage.file_limits()['free']} MB).</b>\n\n"
                    "💎 Please get Premium to download larger files.",
                    parse_mode="HTML",
                )
            else:
                await status.edit_text(
                    f"⚠️ One or more files exceed your {_limit_label(user_id)} limit.",
                    parse_mode="HTML",
                )
            await asyncio.to_thread(storage.record_event, user_id, platform, False, size_bytes)
            return

        # Telegram's Bot API does not expose byte-level upload progress through
        # python-telegram-bot's normal send_* helpers. Show a live animated
        # upload bar rather than pretending a percentage is exact.
        upload_running = True

        async def upload_indicator() -> None:
            frames = [
                "▰▱▱▱▱▱▱▱▱▱",
                "▰▰▱▱▱▱▱▱▱▱",
                "▰▰▰▱▱▱▱▱▱▱",
                "▰▰▰▰▱▱▱▱▱▱",
                "▰▰▰▰▰▱▱▱▱▱",
                "▰▰▰▰▰▰▱▱▱▱",
                "▰▰▰▰▰▰▰▱▱▱",
                "▰▰▰▰▰▰▰▰▱▱",
                "▰▰▰▰▰▰▰▰▰▱",
                "▰▰▰▰▰▰▰▰▰▰",
            ]
            index = 0
            while upload_running:
                try:
                    await status.edit_text(
                        f"📤 <b>Uploading to Telegram…</b>\n"
                        f"<code>[{frames[index % len(frames)]}]</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                index += 1
                await asyncio.sleep(1.2)

        upload_task = asyncio.create_task(upload_indicator())
        file_ids: list[str] = []
        try:
            photo_paths = [
                item for item in paths
                if _media_kind(item) == "photo" and item.stat().st_size <= 10 * 1024 * 1024
            ]

            if len(photo_paths) == len(paths) and photo_paths:
                title = info.title if isinstance(info, MediaInfo) else "Media"
                total_mb = sum(item.stat().st_size for item in paths) / (1024 * 1024)
                caption = (
                    f"✅ {platform} • {label}\n"
                    f"🎬 {title[:80]}\n"
                    f"📸 {len(paths)} photos • {total_mb:.1f} MB"
                )
                sent_messages = await _send_photo_album(
                    query.message,
                    paths=photo_paths,
                    caption=caption,
                )
                for sent in sent_messages:
                    if sent.photo:
                        file_ids.append(sent.photo[-1].file_id)
            else:
                for index, item in enumerate(paths, start=1):
                    size_mb = item.stat().st_size / (1024 * 1024)
                    caption = (
                        f"✅ {platform} • {label} • {size_mb:.1f} MB"
                        if len(paths) == 1
                        else f"✅ {platform} • Photo {index}/{len(paths)} • {size_mb:.1f} MB"
                    )
                    kind = _media_kind(item)
                    sent = await _send_media_message(
                        query.message,
                        path=item,
                        kind=kind,
                        caption=caption,
                    )
                    if kind == "video" and sent.video:
                        file_ids.append(sent.video.file_id)
                    elif kind == "photo" and sent.photo:
                        file_ids.append(sent.photo[-1].file_id)
                    elif sent.document:
                        file_ids.append(sent.document.file_id)
        finally:
            upload_running = False
            await upload_task

        if file_ids:
            media_kinds = [_media_kind(item) for item in paths]
            metadata = {
                "title": info.title if isinstance(info, MediaInfo) else "Media",
                "platform": platform,
                "size_bytes": size_bytes,
                "media_kind": media_kinds[0] if len(set(media_kinds)) == 1 else "document",
                "media_kinds": media_kinds,
            }
            await asyncio.to_thread(storage.set_cache, _cache_key(url, mode), file_ids, metadata)

        await asyncio.to_thread(storage.increment_usage, user_id)
        await asyncio.to_thread(storage.record_event, user_id, platform, True, size_bytes, cache_hit)
        await asyncio.to_thread(
            storage.record_history, user_id, platform, url,
            (info.title if isinstance(info, MediaInfo) else "Media"),
            mode, True, size_bytes,
        )
        await status.delete()
    except DownloadError as exc:
        logger.warning("Download failed user=%s platform=%s mode=%s error=%s", user_id, platform, mode, exc)
        await asyncio.to_thread(storage.record_event, user_id, platform, False, 0, cache_hit)
        await asyncio.to_thread(
            storage.record_history, user_id, platform, url,
            (info.title if isinstance(info, MediaInfo) else "Media"),
            mode, False, 0,
        )
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
            _ACTIVE_USERS.discard(user_id)
