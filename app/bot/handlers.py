import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.core.config import settings
from app.downloader.detector import detect_platform
from app.downloader.service import DownloadError, download_media

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "👋 Welcome to MediaFetch!\n\n"
        "Send me a public media URL and I’ll try to download it for you.\n"
        "Supported platforms are expanding."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text("🔗 Please send a valid http/https media URL.")
        return

    url = match.group(0).rstrip(".,!?)]}")
    platform = detect_platform(url)

    status = await update.message.reply_text(
        f"🔎 Detected: {platform}\n"
        "⏬ Downloading…"
    )

    path: Path | None = None
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        path = await download_media(url, settings.download_dir)

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > settings.max_file_mb:
            await status.edit_text(
                f"⚠️ The downloaded file is {size_mb:.1f} MB, "
                f"above the current {settings.max_file_mb} MB bot limit."
            )
            return

        await status.edit_text("📤 Uploading to Telegram…")

        with path.open("rb") as media:
            await update.message.reply_document(
                document=media,
                caption=f"✅ {platform} • {size_mb:.1f} MB",
            )

        await status.delete()
    except DownloadError:
        await status.edit_text(
            "❌ I couldn't download that URL. "
            "The content may be private, unsupported, restricted, or temporarily unavailable."
        )
    except Exception:
        await status.edit_text("❌ Something went wrong while processing that link.")
    finally:
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
