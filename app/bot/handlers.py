from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

SUPPORTED_TEXT = (
    "YouTube • Instagram • Facebook • Reddit • X/Twitter • "
    "TikTok • Pinterest • Threads"
)


def _cache_key(url: str, mode: str) -> str:
    return hashlib.sha256(f"{url}|{mode}".encode("utf-8")).hexdigest()


def _limit_for(user_id: int) -> int:
    return settings.premium_daily_limit if storage.is_premium(user_id) else settings.free_daily_limit


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
        "🎬 Video • 🎵 MP3 • 📸 HD photos • 🖼️ carousels\n\n"
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
        "Commands: /start /help /supported /about /premium",
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
    except DownloadError:
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

    max_file_mb = min(
        settings.premium_max_file_mb if storage.is_premium(user_id) else settings.max_file_mb,
        50,
    )

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
                for index, file_id in enumerate(cache["file_ids"], start=1):
                    caption = (
                        f"⚡ Cached • {platform} • {label}"
                        if len(cache["file_ids"]) == 1
                        else f"⚡ Cached • {platform} • Photo {index}/{len(cache['file_ids'])}"
                    )
                    await query.message.reply_document(document=file_id, caption=caption)
                await asyncio.to_thread(storage.increment_usage, user_id)
                await asyncio.to_thread(storage.record_event, user_id, platform, True, 0, True)
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

        async def progress(percent: float, detail: str) -> None:
            nonlocal last_text
            text = (
                f"🔎 <b>Platform:</b> {platform}\n"
                f"🎯 <b>Mode:</b> {label}\n"
                f"⏬ <b>Progress:</b> {detail}"
            )
            if text == last_text or status is None:
                return
            last_text = text
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
            await status.edit_text(
                f"⚠️ One or more files exceed the {max_file_mb} MB upload limit."
            )
            await asyncio.to_thread(storage.record_event, user_id, platform, False, size_bytes)
            return

        await status.edit_text("📤 <b>Uploading to Telegram…</b>", parse_mode="HTML")
        file_ids: list[str] = []

        for index, item in enumerate(paths, start=1):
            size_mb = item.stat().st_size / (1024 * 1024)
            caption = (
                f"✅ {platform} • {label} • {size_mb:.1f} MB"
                if len(paths) == 1
                else f"✅ {platform} • Photo {index}/{len(paths)} • {size_mb:.1f} MB"
            )
            with item.open("rb") as media:
                sent = await query.message.reply_document(document=media, caption=caption)
            if sent.document:
                file_ids.append(sent.document.file_id)

        if file_ids:
            metadata = {
                "title": info.title if isinstance(info, MediaInfo) else "Media",
                "platform": platform,
                "size_bytes": size_bytes,
            }
            await asyncio.to_thread(storage.set_cache, _cache_key(url, mode), file_ids, metadata)

        await asyncio.to_thread(storage.increment_usage, user_id)
        await asyncio.to_thread(storage.record_event, user_id, platform, True, size_bytes, cache_hit)
        await status.delete()
    except DownloadError:
        await asyncio.to_thread(storage.record_event, user_id, platform, False, 0, cache_hit)
        if status:
            await status.edit_text(
                "❌ <b>Download failed.</b>\n"
                "The source may be private, restricted, unsupported, or temporarily unavailable.",
                parse_mode="HTML",
            )
    except Exception:
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
