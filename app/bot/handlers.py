import asyncio
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        "2️⃣ Choose the quality or audio mode.\n"
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


def _quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 Best", callback_data="mf:best"),
                InlineKeyboardButton("📺 720p", callback_data="mf:720p"),
            ],
            [
                InlineKeyboardButton("📱 480p", callback_data="mf:480p"),
                InlineKeyboardButton("🎵 MP3", callback_data="mf:audio"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="mf:cancel")],
        ]
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

    url = match.group(0).rstrip(".,!?)]}")
    platform = detect_platform(url)

    context.user_data["mediafetch_pending_url"] = url
    context.user_data["mediafetch_pending_platform"] = platform

    await update.message.reply_text(
        f"🔎 <b>Platform:</b> {platform}\n\n"
        "Choose how you want the media:",
        reply_markup=_quality_keyboard(),
        parse_mode="HTML",
    )


async def download_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    mode = query.data.removeprefix("mf:")
    if mode == "cancel":
        context.user_data.pop("mediafetch_pending_url", None)
        context.user_data.pop("mediafetch_pending_platform", None)
        await query.edit_message_text("❌ Download cancelled.")
        return

    url = context.user_data.pop("mediafetch_pending_url", None)
    platform = context.user_data.pop("mediafetch_pending_platform", "Unknown")

    if not url:
        await query.edit_message_text(
            "⌛ This download request expired. Please send the URL again."
        )
        return

    user_id = update.effective_user.id if update.effective_user else query.message.chat_id

    async with _ACTIVE_LOCK:
        if user_id in _ACTIVE_USERS:
            await query.edit_message_text(
                "⏳ You already have a download running. "
                "Please wait for it to finish."
            )
            return
        _ACTIVE_USERS.add(user_id)

    labels = {
        "best": "Best quality",
        "720p": "720p",
        "480p": "480p",
        "audio": "MP3 audio",
    }
    label = labels.get(mode, mode)

    status = None
    path: Path | None = None

    try:
        status = await query.edit_message_text(
            f"🔎 <b>Platform:</b> {platform}\n"
            f"🎯 <b>Mode:</b> {label}\n"
            "⏬ <b>Progress:</b> starting…",
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

        path = await download_media(
            url,
            settings.download_dir,
            mode=mode,
            progress_callback=progress,
        )

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > settings.max_file_mb:
            await status.edit_text(
                f"⚠️ File size: {size_mb:.1f} MB. "
                f"Limit: {settings.max_file_mb} MB."
            )
            return

        await status.edit_text(
            "📤 <b>Progress:</b> uploading to Telegram…",
            parse_mode="HTML",
        )

        with path.open("rb") as media:
            await query.message.reply_document(
                document=media,
                caption=f"✅ {platform} • {label} • {size_mb:.1f} MB",
            )

        await status.delete()
    except DownloadError:
        if status:
            await status.edit_text(
                "❌ <b>Download failed.</b>\n"
                "The URL may be private, restricted, unsupported, or temporarily unavailable.",
                parse_mode="HTML",
            )
    except Exception:
        if status:
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
