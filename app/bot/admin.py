from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from yt_dlp.version import __version__ as YTDLP_VERSION

from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from app.core.config import settings


logger = logging.getLogger("mediafetch.admin")
from app.core.storage import storage


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in settings.admin_id_set)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    stats = await asyncio.to_thread(storage.stats)
    mode = "MongoDB" if storage.persistent else "memory fallback"
    cookie_status = (
        "🟢 Loaded"
        if Path(settings.ytdlp_cookies_file).is_file()
        else "⚪ Not loaded"
    )
    await update.message.reply_text(
        "🛠 <b>MediaFetch Admin</b>\n\n"
        f"📥 Downloads: <b>{stats['downloads']}</b>\n"
        f"⚡ Cache hits: <b>{stats['cache_hits']}</b>\n"
        f"❌ Failures: <b>{stats['failures']}</b>\n"
        f"💾 Data processed: <b>{stats['bytes'] / (1024 * 1024):.1f} MB</b>\n"
        f"👥 Known users: <b>{len(await asyncio.to_thread(storage.user_ids))}</b>\n"
        f"🗄 Storage: <b>{mode}</b>\n"
        f"🍪 yt-dlp cookies: <b>{cookie_status}</b>\n\n"
        "Commands:\n"
        "/premium USER_ID DAYS\n"
        "/revoke USER_ID\n"
        "/maintenance on|off\n"
        "/broadcast (reply to a message)\n"
        "/cookies (reply to cookies.txt)\n"
        "/cookies_clear",
        "/diagnostics",
        parse_mode="HTML",
    )


async def diagnostics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    try:
        deno = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
    except Exception:
        deno = "unavailable"
    stats = await asyncio.to_thread(storage.stats)
    platform_stats = await asyncio.to_thread(storage.platform_stats)
    lines = [
        "🩺 <b>MediaFetch diagnostics</b>",
        f"yt-dlp: <code>{YTDLP_VERSION}</code>",
        f"Deno: <code>{deno}</code>",
        f"Storage: <code>{'MongoDB' if storage.persistent else 'memory fallback'}</code>",
        f"Cookies: <code>{'loaded' if Path(settings.ytdlp_cookies_file).is_file() else 'not loaded'}</code>",
        f"Downloads: <code>{stats['downloads']}</code> • failures: <code>{stats['failures']}</code>",
    ]
    if platform_stats:
        lines.append("\n<b>Platforms</b>")
        for row in platform_stats[:10]:
            lines.append(f"• {row['platform']}: {row['downloads']} requests / {row['successes']} success")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /premium USER_ID DAYS")
        return
    target = int(context.args[0])
    days = max(1, int(context.args[1]))
    expires = await asyncio.to_thread(storage.set_premium, target, days)
    date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await update.message.reply_text(f"💎 Premium enabled for {target} until {date}.")


async def revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke USER_ID")
        return
    await asyncio.to_thread(storage.set_premium, int(context.args[0]), -1)
    await update.message.reply_text("✅ Premium revoked.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /maintenance on|off")
        return
    enabled = context.args[0].lower() == "on"
    await asyncio.to_thread(storage.set_maintenance, enabled)
    await update.message.reply_text(f"🔧 Maintenance mode: {'ON' if enabled else 'OFF'}")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    source = update.message.reply_to_message
    if not source:
        await update.message.reply_text("Reply to the message you want to broadcast, then use /broadcast.")
        return
    users = await asyncio.to_thread(storage.user_ids)
    sent = failed = 0
    for target in users:
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=source.chat_id, message_id=source.message_id)
            sent += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await context.bot.copy_message(chat_id=target, from_chat_id=source.chat_id, message_id=source.message_id)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 Broadcast finished. Sent: {sent} • Failed: {failed}")


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Import a Netscape-format cookies.txt file from an admin reply."""
    user_id = update.effective_user.id if update.effective_user else None
    message = update.message
    if not _is_admin(user_id) or not message:
        return

    source = message.reply_to_message
    if not source or not source.document:
        await message.reply_text(
            "🍪 Reply to your exported <b>cookies.txt</b> file with /cookies.\n"
            "Only the bot admin can import it.",
            parse_mode="HTML",
        )
        return

    filename = (source.document.file_name or "").lower()
    if not (filename.endswith(".txt") or filename.endswith(".cookies")):
        await message.reply_text(
            "⚠️ Please send a Netscape-format cookies file, usually named <b>cookies.txt</b>.",
            parse_mode="HTML",
        )
        return

    try:
        target = Path(settings.ytdlp_cookies_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        tg_file = await context.bot.get_file(source.document.file_id)
        await tg_file.download_to_drive(custom_path=str(target))

        size = target.stat().st_size
        if size <= 0 or size > 5 * 1024 * 1024:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ Cookie file is empty or larger than 5 MB.")
            return

        # Validate the Netscape/Mozilla structure without logging sensitive values.
        raw = target.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        header_ok = any(line in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"} for line in lines[:5])
        if not header_ok:
            target.unlink(missing_ok=True)
            await message.reply_text(
                "⚠️ Invalid cookies.txt format. The first line must be "
                "<code># Netscape HTTP Cookie File</code> or "
                "<code># HTTP Cookie File</code>.",
                parse_mode="HTML",
            )
            return

        valid_rows = sum(
            1
            for line in raw.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and len(line.split("\t")) >= 7
        )
        if valid_rows == 0:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ No valid Netscape cookie rows were found in this file.")
            return

        await message.reply_text(
            f"🍪 <b>Cookies imported successfully.</b>\n"
            f"Entries detected: <b>{valid_rows}</b>\n"
            "yt-dlp will use them for supported extractors.",
            parse_mode="HTML",
        )
    except Exception as exc:
        Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
        logger.exception(
            "Cookie import failed user=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )
        await message.reply_text(
            "❌ Cookie import failed while downloading or reading the file. "
            "Please try sending the exported cookies.txt again."
        )


async def cookies_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
    await update.message.reply_text("🗑️ yt-dlp cookies cleared from this instance.")


