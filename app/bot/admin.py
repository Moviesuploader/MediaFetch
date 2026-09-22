from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from app.core.config import settings
from app.core.storage import storage


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in settings.admin_id_set)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    stats = await asyncio.to_thread(storage.stats)
    mode = "MongoDB" if storage.persistent else "memory fallback"
    await update.message.reply_text(
        "🛠 <b>MediaFetch Admin</b>\n\n"
        f"📥 Downloads: <b>{stats['downloads']}</b>\n"
        f"⚡ Cache hits: <b>{stats['cache_hits']}</b>\n"
        f"❌ Failures: <b>{stats['failures']}</b>\n"
        f"💾 Data processed: <b>{stats['bytes'] / (1024 * 1024):.1f} MB</b>\n"
        f"👥 Known users: <b>{len(await __import__('asyncio').to_thread(storage.user_ids))}</b>\n"
        f"🗄 Storage: <b>{mode}</b>\n\n"
        "Commands:\n"
        "/premium USER_ID DAYS\n"
        "/revoke USER_ID\n"
        "/maintenance on|off\n"
        "/broadcast (reply to a message)",
        parse_mode="HTML",
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /premium USER_ID DAYS")
        return
    target = int(context.args[0])
    days = max(1, int(context.args[1]))
    expires = await __import__("asyncio").to_thread(storage.set_premium, target, days)
    date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await update.message.reply_text(f"💎 Premium enabled for {target} until {date}.")


async def revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke USER_ID")
        return
    await __import__("asyncio").to_thread(storage.set_premium, int(context.args[0]), -1)
    await update.message.reply_text("✅ Premium revoked.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /maintenance on|off")
        return
    enabled = context.args[0].lower() == "on"
    await __import__("asyncio").to_thread(storage.set_maintenance, enabled)
    await update.message.reply_text(f"🔧 Maintenance mode: {'ON' if enabled else 'OFF'}")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id) or not update.message:
        return
    source = update.message.reply_to_message
    if not source:
        await update.message.reply_text("Reply to the message you want to broadcast, then use /broadcast.")
        return
    users = await __import__("asyncio").to_thread(storage.user_ids)
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
        # Stay below Telegram's normal bulk-send rate limit.
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 Broadcast finished. Sent: {sent} • Failed: {failed}")
