import asyncio
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.core.config import settings
from app.downloader.detector import detect_platform
from app.downloader.service import DownloadError, download_media

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ACTIVE_USERS: set[int] = set()
_ACTIVE_LOCK = asyncio.Lock()

SUPPORTED_TEXT = (
    "YouTube • Instagram • Facebook • Reddit • X/Twitter • "
    "TikTok • Pinterest • Threads"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "👋 <b>Welcome to MediaFetch!</b>\n\n"
        "Send me a public media URL and I’ll try to download it.\n\n"
        "Use /help for commands.",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🛠 <b>MediaFetch Help</b>\n\n"
        "1️⃣ Send a public media URL.\n"
        "2️⃣ I detect the platform.\n"
        "3️⃣ I download the media.\n"
        "4️⃣ I send the file back here.\n\n"
        "Commands: /start /help /supported /about",
        parse_mode="HTML",
    )


async def supported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        f"🌐 <b>Detected platforms</b>\n\n{SUPPORTED_TEXT}\n\n"
        "Actual download support depends on yt-dlp and the platform.",
        parse_mode="HTML",
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "⚡ <b>MediaFetch</b>\n\n"
        "A modular media downloader built with Python, Telegram Bot API, "
        "FastAPI, yt-dlp and FFmpeg.\n\n"
        "Only download content you are authorized to download.",
        parse_mode="HTML",
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "🔗 Send a valid public http/https media URL.\n"
            "Try /supported to see detected platforms."
        )
        return

    user_id = update.effective_user.id if update.effective_user else update.message.chat_id
    async with _ACTIVE_LOCK:
        if user_id in _ACTIVE_USERS:
            await update.message.reply_text(
                "⏳ You already have a download running. "
                "Please wait for it to finish."
            )
            return
        _ACTIVE_USERS.add(user_id)

    url = match.group(0).rstrip(".,!?)]}")
    platform = detect_platform(url)
    status = await update.message.reply_text(
        f"🔎 <b>Platform:</b> {platform}\n"
        "⏬ <b>Status:</b> starting download…",
        parse_mode="HTML",
    )

    path: Path | None = None
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        await status.edit_text(
            f"🔎 <b>Platform:</b> {platform}\n"
            "⏬ <b>Status:</b> downloading…",
            parse_mode="HTML",
        )
        path = await download_media(url, settings.download_dir)

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > settings.max_file_mb:
            await status.edit_text(
                f"⚠️ File size: {size_mb:.1f} MB. "
                f"Limit: {settings.max_file_mb} MB."
            )
            return

        await status.edit_text("📤 <b>Status:</b> uploading to Telegram…", parse_mode="HTML")
        with path.open("rb") as media:
            await update.message.reply_document(
                document=media,
                caption=f"✅ {platform} • {size_mb:.1f} MB",
            )

        await status.delete()
    except DownloadError:
        await status.edit_text(
            "❌ <b>Download failed.</b>\n"
            "The URL may be private, restricted, unsupported, or temporarily unavailable.",
            parse_mode="HTML",
        )
    except Exception:
        await status.edit_text(
            "❌ Something went wrong while processing that link. Please try again."
        )
    finally:
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        async with _ACTIVE_LOCK:
            _ACTIVE_USERS.discard(user_id)
