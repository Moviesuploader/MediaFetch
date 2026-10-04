from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from yt_dlp.version import __version__ as YTDLP_VERSION
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from app.core.config import settings
from app.core.storage import storage

logger = logging.getLogger("mediafetch.admin")
_PENDING_ADMIN_ACTIONS: dict[int, str] = {}
_PENDING_BROADCASTS: dict[int, tuple[int, int]] = {}


def _owner_id() -> int | None:
    if getattr(settings, "owner_id", ""):
        try:
            return int(str(settings.owner_id).strip())
        except ValueError:
            return None
    return next(iter(sorted(settings.admin_id_set)), None)


def _is_owner(user_id: int | None) -> bool:
    return bool(user_id and _owner_id() == user_id)


def admin_has_pending_action(user_id: int | None) -> bool:
    return bool(user_id and _PENDING_ADMIN_ACTIONS.get(user_id))


def _private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Overview", callback_data="mfa:overview"),
         InlineKeyboardButton("⚙️ Runtime", callback_data="mfa:runtime")],
        [InlineKeyboardButton("📡 Log Channels", callback_data="mfa:channels"),
         InlineKeyboardButton("📢 Broadcast", callback_data="mfa:broadcast")],
        [InlineKeyboardButton("🍪 Cookies", callback_data="mfa:cookies"),
         InlineKeyboardButton("🩺 Diagnostics", callback_data="mfa:diagnostics")],
        [InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
         InlineKeyboardButton("❌ Cancel", callback_data="mfa:cancel")]
    ])


def _channels_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Set Dump", callback_data="mfa:setdump"),
         InlineKeyboardButton("🔗 Set Links Log", callback_data="mfa:setlinks")],
        [InlineKeyboardButton("🗑 Clear Dump", callback_data="mfa:cleardump"),
         InlineKeyboardButton("🗑 Clear Links", callback_data="mfa:clearlinks")],
        [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
         InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


def _runtime_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Task Limit", callback_data="mfa:tasklimit"),
         InlineKeyboardButton("📦 Plan Limits", callback_data="mfa:filelimits")],
        [InlineKeyboardButton("🔧 Maintenance", callback_data="mfa:maintenance")],
        [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
         InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


async def _render_home(message, edit: bool = True) -> None:
    stats = await asyncio.to_thread(storage.stats)
    limits = await asyncio.to_thread(storage.file_limits)
    channels = await asyncio.to_thread(storage.channel_config)
    task_limit = await asyncio.to_thread(storage.concurrent_download_limit)
    users = len(await asyncio.to_thread(storage.user_ids))
    text = (
        "🛠 <b>MediaFetch Owner Panel</b>\n\n"
        f"📥 Downloads: <b>{stats['downloads']}</b> • ❌ Failures: <b>{stats['failures']}</b>\n"
        f"⚡ Cache hits: <b>{stats['cache_hits']}</b> • 👥 Users: <b>{users}</b>\n\n"
        f"⬇️ Task limit: <b>{task_limit}</b> concurrent\n"
        f"📥 Dump: <code>{channels.get('dump') or 'Not configured'}</code>\n"
        f"🔗 Links log: <code>{channels.get('links') or 'Not configured'}</code>\n\n"
        f"🆓 Free: <b>{limits['free']} MB</b> • 💎 Premium: <b>{limits['premium']} MB</b> • 👑 Admin: <b>{limits['admin']} MB</b>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_main_keyboard())
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=_main_keyboard())


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    _PENDING_ADMIN_ACTIONS.pop(uid, None)
    _PENDING_BROADCASTS.pop(uid, None)
    await _render_home(update.message, edit=False)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id if update.effective_user else None
    if not query or not query.message:
        return
    if not _is_owner(uid) or not _private(update):
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1] if query.data else ""

    if action in {"cancel", "home"}:
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        _PENDING_BROADCASTS.pop(uid, None)
        await _render_home(query.message)
        return
    if action == "close":
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        _PENDING_BROADCASTS.pop(uid, None)
        await query.message.delete()
        return

    if action == "overview":
        s = await asyncio.to_thread(storage.stats)
        await query.message.edit_text(
            "📊 <b>Overview</b>\n\n"
            f"📥 Downloads: <b>{s['downloads']}</b>\n⚡ Cache hits: <b>{s['cache_hits']}</b>\n"
            f"❌ Failures: <b>{s['failures']}</b>\n💾 Data: <b>{s['bytes']/(1024*1024):.1f} MB</b>\n"
            f"👥 Users: <b>{len(await asyncio.to_thread(storage.user_ids))}</b>",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "runtime":
        limits = await asyncio.to_thread(storage.file_limits)
        await query.message.edit_text(
            "⚙️ <b>Runtime Settings</b>\n\n"
            f"⬇️ Concurrent downloads: <b>{storage.concurrent_download_limit()}</b>\n"
            f"🆓 Free: <b>{limits['free']} MB</b>\n🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
            f"💎 Platinum: <b>{limits['platinum']} MB</b>\n💎 Diamond: <b>{limits['diamond']} MB</b>\n"
            "👑 Admin/Owner: <b>Unlimited</b>\n"
            f"🔧 Maintenance: <b>{'ON' if storage.maintenance() else 'OFF'}</b>",
            parse_mode="HTML", reply_markup=_runtime_keyboard())
        return

    if action == "tasklimit":
        _PENDING_ADMIN_ACTIONS[uid] = "tasklimit"
        await query.message.edit_text(
            "⬇️ <b>Task Downloading Limit</b>\n\nSend a number from <b>1–20</b>.\n"
            "This controls simultaneous downloads.\n\nExample: <code>2</code>",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "filelimits":
        limits = await asyncio.to_thread(storage.file_limits)
        await query.message.edit_text(
            "📦 <b>Plan Limits</b>\n\n"
            f"🆓 Free: <b>{limits['free']} MB</b>\n🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
            f"💎 Platinum: <b>{limits['platinum']} MB</b>\n💎 Diamond: <b>{limits['diamond']} MB</b>\n"
            "👑 Admin/Owner: <b>Unlimited</b>\n\n"
            "Change limits: <code>/set_limit free|bronze|platinum|diamond MB</code>.",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "maintenance":
        enabled = not storage.maintenance()
        await asyncio.to_thread(storage.set_maintenance, enabled)
        await query.message.edit_text(
            f"🔧 <b>Maintenance: {'ON' if enabled else 'OFF'}</b>",
            parse_mode="HTML", reply_markup=_runtime_keyboard())
        return

    if action == "channels":
        ch = await asyncio.to_thread(storage.channel_config)
        await query.message.edit_text(
            "📡 <b>Operational Log Channels</b>\n\n"
            f"📥 Dump: <code>{ch.get('dump') or 'Not configured'}</code>\n"
            f"🔗 Links Log: <code>{ch.get('links') or 'Not configured'}</code>\n\n"
            "Set button dabao, phir target channel ka <b>koi bhi message forward</b> karo. "
            "Channel ID automatically save ho jayega.",
            parse_mode="HTML", reply_markup=_channels_keyboard())
        return

    if action in {"setdump", "setlinks"}:
        _PENDING_ADMIN_ACTIONS[uid] = "set_dump" if action == "setdump" else "set_links"
        label = "Dump" if action == "setdump" else "Links Log"
        await query.message.edit_text(
            f"📡 <b>Configure {label} Channel</b>\n\n"
            "Target channel se <b>koi bhi message forward</b> karke bhejo.\n"
            "Only owner can configure it.",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action in {"cleardump", "clearlinks"}:
        kind = "dump" if action == "cleardump" else "links"
        await asyncio.to_thread(storage.set_channel_config, kind, "")
        await query.message.edit_text(
            f"🗑 <b>{'Dump' if kind == 'dump' else 'Links Log'} cleared.</b>",
            parse_mode="HTML", reply_markup=_channels_keyboard())
        return

    if action == "broadcast":
        _PENDING_ADMIN_ACTIONS[uid] = "broadcast"
        _PENDING_BROADCASTS.pop(uid, None)
        await query.message.edit_text(
            "📢 <b>Broadcast</b>\n\n"
            "Ab jo message broadcast karna hai woh send/forward karo.\n"
            "Text, photo, video, document etc. supported.\n\n"
            "Review ke baad <b>Confirm & Send</b> hoga.",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "broadcast_confirm":
        source = _PENDING_BROADCASTS.pop(uid, None)
        if not source:
            await query.message.edit_text("⌛ Broadcast draft expired.", reply_markup=_main_keyboard())
            return
        chat_id, message_id = source
        users = await asyncio.to_thread(storage.user_ids)
        await query.message.edit_text("📢 <b>Broadcasting…</b>\n\n⏳ Please wait.", parse_mode="HTML")
        sent = failed = 0
        for target in users:
            try:
                await context.bot.copy_message(chat_id=target, from_chat_id=chat_id, message_id=message_id)
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                try:
                    await context.bot.copy_message(chat_id=target, from_chat_id=chat_id, message_id=message_id)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await query.message.edit_text(
            f"📢 <b>Broadcast finished</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode="HTML", reply_markup=_main_keyboard())
        return

    if action == "broadcast_cancel":
        _PENDING_BROADCASTS.pop(uid, None)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await _render_home(query.message)
        return

    if action == "cookies":
        await query.message.edit_text(
            "🍪 <b>YouTube Cookies</b>\n\ncookies.txt send karke <code>/cookies</code> se import karo.",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "diagnostics":
        s = await asyncio.to_thread(storage.stats)
        await query.message.edit_text(
            "🩺 <b>Diagnostics</b>\n\n"
            f"yt-dlp: <code>{YTDLP_VERSION}</code>\n"
            f"Storage: <code>{'MongoDB' if storage.persistent else 'memory fallback'}</code>\n"
            f"Cookies: <code>{'loaded' if Path(settings.ytdlp_cookies_file).is_file() else 'not loaded'}</code>\n"
            f"Downloads: <code>{s['downloads']}</code> • failures: <code>{s['failures']}</code>",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return


async def admin_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    message = update.message
    if not _is_owner(uid) or not message or not _private(update):
        return
    action = _PENDING_ADMIN_ACTIONS.get(uid)
    if not action:
        return

    if action in {"set_dump", "set_links"}:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None)
        if not chat:
            await message.reply_text(
                "⚠️ Channel detect nahi hua. Please target channel ka actual message forward karo.",
                reply_markup=_back_keyboard())
            return
        kind = "dump" if action == "set_dump" else "links"
        await asyncio.to_thread(storage.set_channel_config, kind, chat.id)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        label = "Dump" if kind == "dump" else "Links Log"
        await message.reply_text(
            f"✅ <b>{label} channel configured.</b>\n\n"
            f"Channel: <b>{chat.title or chat.username or chat.id}</b>\nID: <code>{chat.id}</code>\n\n"
            "Bot ko target channel me admin/post permission do.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Log Channels", callback_data="mfa:channels"),
                 InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")]
            ]))
        return

    if action == "tasklimit":
        value = (message.text or "").strip()
        if not value.isdigit() or not 1 <= int(value) <= 20:
            await message.reply_text("⚠️ Task limit <b>1–20</b> hona chahiye.", parse_mode="HTML")
            return
        limit = await asyncio.to_thread(storage.set_concurrent_download_limit, int(value))
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await message.reply_text(
            f"✅ <b>Concurrent download limit set to {limit}.</b>",
            parse_mode="HTML", reply_markup=_runtime_keyboard())
        return

    if action == "broadcast":
        _PENDING_BROADCASTS[uid] = (message.chat_id, message.message_id)
        _PENDING_ADMIN_ACTIONS[uid] = "broadcast_review"
        await message.reply_text(
            "📢 <b>Broadcast Review</b>\n\nMessage receive ho gaya. "
            "Sab users ko bhejne se pehle confirm karo.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Send", callback_data="mfa:broadcast_confirm")],
                [InlineKeyboardButton("🔄 Replace", callback_data="mfa:broadcast")],
                [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
                 InlineKeyboardButton("❌ Cancel", callback_data="mfa:broadcast_cancel")]
            ]))
        return


async def diagnostics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    try:
        deno = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
    except Exception:
        deno = "unavailable"
    s = await asyncio.to_thread(storage.stats)
    await update.message.reply_text(
        "🩺 <b>MediaFetch diagnostics</b>\n"
        f"yt-dlp: <code>{YTDLP_VERSION}</code>\nDeno: <code>{deno}</code>\n"
        f"Storage: <code>{'MongoDB' if storage.persistent else 'memory fallback'}</code>\n"
        f"Downloads: <code>{s['downloads']}</code> • failures: <code>{s['failures']}</code>",
        parse_mode="HTML")


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    if len(context.args) == 2 and context.args[0].isdigit() and context.args[1].isdigit():
        user_id, plan, days = int(context.args[0]), "bronze", max(1, int(context.args[1]))
    elif len(context.args) == 3 and context.args[0].isdigit() and context.args[1].lower() in {"bronze", "platinum", "diamond"} and context.args[2].isdigit():
        user_id, plan, days = int(context.args[0]), context.args[1].lower(), max(1, int(context.args[2]))
    else:
        await update.message.reply_text("Usage:\n<code>/premium USER_ID DAYS</code> → Bronze\n<code>/premium USER_ID bronze|platinum|diamond DAYS</code>", parse_mode="HTML")
        return
    expires = await asyncio.to_thread(storage.set_plan, user_id, plan, days)
    date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    labels = {"bronze": "🥉 Bronze", "platinum": "💎 Platinum", "diamond": "💎 Diamond"}
    await update.message.reply_text(f"✅ {labels[plan]} enabled for <code>{user_id}</code> until <b>{date}</b>.", parse_mode="HTML")


async def revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke USER_ID")
        return
    await asyncio.to_thread(storage.set_premium, int(context.args[0]), -1)
    await update.message.reply_text("✅ Premium revoked.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /maintenance on|off")
        return
    enabled = context.args[0].lower() == "on"
    await asyncio.to_thread(storage.set_maintenance, enabled)
    await update.message.reply_text(f"🔧 Maintenance mode: {'ON' if enabled else 'OFF'}")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    source = update.message.reply_to_message
    if not source:
        await update.message.reply_text("Use /admin → 📢 Broadcast for the clean broadcast flow.")
        return
    _PENDING_BROADCASTS[uid] = (source.chat_id, source.message_id)
    _PENDING_ADMIN_ACTIONS[uid] = "broadcast_review"
    await update.message.reply_text(
        "📢 <b>Broadcast Review</b>\n\nConfirm karne ke baad broadcast start hoga.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Send", callback_data="mfa:broadcast_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="mfa:broadcast_cancel")]
        ]))


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    message = update.message
    if not _is_owner(uid) or not message or not _private(update):
        return
    source = message.reply_to_message
    if not source or not source.document:
        await message.reply_text("🍪 Reply to your exported cookies.txt file with /cookies.")
        return
    filename = (source.document.file_name or "").lower()
    if not (filename.endswith(".txt") or filename.endswith(".cookies")):
        await message.reply_text("⚠️ Please send a Netscape-format cookies file.")
        return
    try:
        target = Path(settings.ytdlp_cookies_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        tg_file = await context.bot.get_file(source.document.file_id)
        await tg_file.download_to_drive(custom_path=str(target))
        raw = target.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not any(line in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"} for line in lines[:5]):
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ Invalid Netscape cookies.txt format.")
            return
        valid = sum(1 for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#") and len(line.split("\t")) >= 7)
        if valid == 0:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ No valid cookie rows found.")
            return
        if target.stat().st_size > 5 * 1024 * 1024:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ Cookie file is larger than 5 MB.")
            return
        await message.reply_text(f"🍪 <b>Cookies imported.</b> Entries: <b>{valid}</b>", parse_mode="HTML")
    except Exception as exc:
        Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
        logger.exception("Cookie import failed user=%s error_type=%s", uid, type(exc).__name__)
        await message.reply_text("❌ Cookie import failed.")


async def cookies_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
    await update.message.reply_text("🗑️ yt-dlp cookies cleared.")


async def set_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    if len(context.args) != 2 or context.args[0].lower() not in {"free", "bronze", "platinum", "diamond", "premium", "admin"} or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /set_limit free|bronze|platinum|diamond MB")
        return
    mb = int(context.args[1])
    if not 1 <= mb <= 100000:
        await update.message.reply_text("Limit must be between 1 and 100000 MB.")
        return
    limits = await asyncio.to_thread(storage.set_file_limit, context.args[0], mb)
    await update.message.reply_text(
        "⚙️ <b>File limit updated</b>\n\n"
        f"🆓 Free: <b>{limits['free']} MB</b>\n💎 Premium: <b>{limits['premium']} MB</b>\n👑 Admin: <b>{limits['admin']} MB</b>",
        parse_mode="HTML")
