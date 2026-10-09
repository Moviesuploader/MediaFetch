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
from app.bot.mtproto import mtproto_uploader
from app.core.payments import PLAN_LABELS, payment_config, payment_summary

logger = logging.getLogger("mediafetch.admin")
_PENDING_ADMIN_ACTIONS: dict[int, str] = {}
_PENDING_BROADCASTS: dict[int, tuple[int, int]] = {}
_PANEL_MESSAGES: dict[int, tuple[int, int]] = {}


def _owner_id() -> int | None:
    if getattr(settings, "owner_id", ""):
        try:
            return int(str(settings.owner_id).strip())
        except ValueError:
            return None
    return None


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
        [InlineKeyboardButton("👥 Plan Management", callback_data="mfa:plans"), InlineKeyboardButton("💳 Payments", callback_data="mfa:payments")],
        [InlineKeyboardButton("🍪 Cookies", callback_data="mfa:cookies"),
         InlineKeyboardButton("🩺 Diagnostics", callback_data="mfa:diagnostics")],
        [InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
         InlineKeyboardButton("❌ Cancel", callback_data="mfa:cancel")]
    ])

async def _edit_payment_message(message, text: str, reply_markup=None) -> None:
    """Approval messages are photos with captions; owner-panel details are text messages."""
    if getattr(message, "photo", None):
        await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)

async def _panel_edit(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, reply_markup=None) -> bool:
    target = _PANEL_MESSAGES.get(uid)
    if not target:
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=target[0], message_id=target[1], text=text,
            parse_mode="HTML", reply_markup=reply_markup,
        )
        return True
    except Exception as exc:
        logger.warning("Owner panel edit failed user=%s error_type=%s", uid, type(exc).__name__)
        return False


async def _swallow(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


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
        [InlineKeyboardButton("🚀 Upload Engine", callback_data="mfa:upload"),
         InlineKeyboardButton("🔧 Maintenance", callback_data="mfa:maintenance")],
        [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
         InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


async def _render_home(message, edit: bool = True):
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
        f"🆓 Free: <b>{limits['free']} MB</b> • 🥉 Bronze: <b>{limits['bronze']} MB</b> • 💎 Platinum: <b>{limits['platinum']} MB</b> • 💎 Diamond: <b>{limits['diamond']} MB</b> • 👑 Admin/Owner: <b>Unlimited</b>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_main_keyboard())
    else:
        sent = await message.reply_text(text, parse_mode="HTML", reply_markup=_main_keyboard())
        return sent


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not _is_owner(uid) or not update.message or not _private(update):
        return
    _PENDING_ADMIN_ACTIONS.pop(uid, None)
    _PENDING_BROADCASTS.pop(uid, None)
    sent = await _render_home(update.message, edit=False)
    _PANEL_MESSAGES[uid] = (sent.chat_id, sent.message_id) if sent else (update.message.chat_id, update.message.message_id)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id if update.effective_user else None
    if not query or not query.message:
        return
    action = query.data.split(":", 1)[1] if query.data else ""
    is_private = _private(update)
    group_id = str(getattr(settings, "payment_approval_chat_id", "") or "").strip()
    chat_id = str(query.message.chat_id)
    allowed_group_payment_action = (
        bool(group_id)
        and chat_id == group_id
        and action.startswith(("payapprove:", "payreject:"))
    )
    if not _is_owner(uid) or (not is_private and not allowed_group_payment_action):
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    if is_private and uid is not None:
        _PANEL_MESSAGES[uid] = (query.message.chat_id, query.message.message_id)

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

    if action == "payments":
        stats = await asyncio.to_thread(storage.payment_stats)
        cfg = payment_config()
        await query.message.edit_text(
            "💳 <b>Payment Management</b>\n\n"
            f"🟡 Pending: <b>{stats['pending']}</b>\n✅ Approved: <b>{stats['approved']}</b>\n❌ Rejected: <b>{stats['rejected']}</b>\n\n"
            f"📱 UPI: <code>{cfg['upi_id'] or 'Not configured'}</code>\n"
            f"🥉 Bronze: <b>{cfg['prices']['bronze']} {cfg['currency']}</b>\n"
            f"💎 Platinum: <b>{cfg['prices']['platinum']} {cfg['currency']}</b>\n"
            f"💎 Diamond: <b>{cfg['prices']['diamond']} {cfg['currency']}</b>\n"
            f"⏳ Durations: <b>Bronze {cfg['durations']['bronze']}d • Platinum {cfg['durations']['platinum']}d • Diamond {cfg['durations']['diamond']}d</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟡 Pending Payments", callback_data="mfa:paypending")],
                [InlineKeyboardButton("⚙️ Configure UPI/Prices", callback_data="mfa:payconfig")],
                [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"), InlineKeyboardButton("❌ Close", callback_data="mfa:close")]
            ]))
        return

    if action == "paypending":
        items = await asyncio.to_thread(storage.pending_payments, 20)
        rows = [[InlineKeyboardButton(
            f"{str(x.get('user_name') or ('@' + x.get('username') if x.get('username') else x.get('user_id', 'User')))} • {PLAN_LABELS.get(x.get('plan'), x.get('plan'))} • {x.get('amount')} {x.get('currency')}",
            callback_data=f"mfa:payview:{x.get('payment_id')}"
        )] for x in items]
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="mfa:payments")])
        await query.message.edit_text(
            "🟡 <b>Pending Payments</b>\n\n" + ("Select a payment to verify." if items else "No pending payments."),
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action.startswith("payview:"):
        payment_id = action.split(":", 1)[1]
        doc = await asyncio.to_thread(storage.payment_by_id, payment_id)
        if not doc:
            await query.message.edit_text("❌ Payment not found.", reply_markup=_back_keyboard())
            return
        details_text = payment_summary(doc) + "\n\n⚠️ Verify payment in your UPI/bank app before approving."
        if getattr(query.message, "photo", None):
            await _edit_payment_message(
                query.message, details_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"mfa:payapprove:{payment_id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"mfa:payreject:{payment_id}")]
                ]),
            )
        else:
            await query.message.edit_text(
                details_text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"mfa:payapprove:{payment_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"mfa:payreject:{payment_id}")],
                    [InlineKeyboardButton("🔙 Pending", callback_data="mfa:paypending")]
                ]))
        return

    if action.startswith("payapprove:"):
        payment_id = action.split(":", 1)[1]
        try:
            doc = await asyncio.to_thread(storage.approve_payment, payment_id, uid, payment_config()["duration_days"])
        except ValueError as exc:
            await _edit_payment_message(query.message, f"⚠️ {exc}", reply_markup=_back_keyboard())
            return
        expires = float(doc.get("subscription_until") or 0)
        date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            await context.bot.send_message(chat_id=int(doc["user_id"]), text=f"✅ <b>Payment approved!</b>\n\n📦 Plan: <b>{PLAN_LABELS.get(doc.get('plan'), doc.get('plan'))}</b>\n⏳ Active until: <b>{date}</b>", parse_mode="HTML")
        except Exception:
            logger.warning("Could not notify approved payment id=%s", payment_id)
        await _edit_payment_message(query.message, payment_summary(doc) + f"\n\n✅ <b>Approved</b>\n⏳ Active until: <b>{date}</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mfa:payments")]]))
        return

    if action.startswith("payreject:"):
        payment_id = action.split(":", 1)[1]
        try:
            doc = await asyncio.to_thread(storage.reject_payment, payment_id, uid)
        except ValueError as exc:
            await _edit_payment_message(query.message, f"⚠️ {exc}", reply_markup=_back_keyboard())
            return
        try:
            await context.bot.send_message(chat_id=int(doc["user_id"]), text="❌ <b>Payment rejected.</b>\nPlease contact the owner if this is unexpected.", parse_mode="HTML")
        except Exception:
            logger.warning("Could not notify rejected payment id=%s", payment_id)
        await _edit_payment_message(query.message, payment_summary(doc) + "\n\n❌ <b>Rejected</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mfa:payments")]]))
        return

    if action == "payconfig":
        _PENDING_ADMIN_ACTIONS[uid] = "payment_config"
        await query.message.edit_text(
            "⚙️ <b>Payment Configuration</b>\n\nSend:\n<code>UPI_ID BRONZE_PRICE BRONZE_DAYS PLATINUM_PRICE PLATINUM_DAYS DIAMOND_PRICE DIAMOND_DAYS</code>\n\nExample: <code>name@upi 29 7 79 30 149 30</code>\nUTR manually verify hoga; koi gateway nahi.",
            parse_mode="HTML", reply_markup=_back_keyboard())
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

    if action == "plans":
        limits = await asyncio.to_thread(storage.file_limits)
        await query.message.edit_text(
            "👥 <b>Plan Management</b>\n\n"
            f"🆓 Free: <b>{limits['free']} MB</b>\n"
            f"🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
            f"💎 Platinum: <b>{limits['platinum']} MB</b>\n"
            f"💎 Diamond: <b>{limits['diamond']} MB</b>\n"
            "👑 Admin/Owner: <b>Unlimited</b>\n\n"
            "<b>Grant:</b> <code>USER_ID PLAN DAYS</code>\n"
            "<b>Revoke:</b> <code>USER_ID</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Grant Plan", callback_data="mfa:grantplan"),
                 InlineKeyboardButton("🔄 Revoke Plan", callback_data="mfa:revokeplan")],
                [InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
                 InlineKeyboardButton("❌ Close", callback_data="mfa:close")]
            ]),
        )
        return

    if action == "grantplan":
        _PENDING_ADMIN_ACTIONS[uid] = "grant_plan"
        await query.message.edit_text(
            "🎁 <b>Grant Plan</b>\n\n"
            "Send: <code>USER_ID bronze|platinum|diamond DAYS</code>",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "revokeplan":
        _PENDING_ADMIN_ACTIONS[uid] = "revoke_plan"
        await query.message.edit_text(
            "🔄 <b>Revoke Plan</b>\n\nSend the numeric Telegram user ID.",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
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

    if action == "grant_plan":
        parts = (message.text or "").strip().split()
        if len(parts) != 3 or not parts[0].isdigit() or parts[1].lower() not in {"bronze", "platinum", "diamond"} or not parts[2].isdigit():
            await _panel_edit(context, uid, "⚠️ Format: <code>USER_ID bronze|platinum|diamond DAYS</code>. Dobara bhejo.", _back_keyboard())
            return
        user_id = int(parts[0])
        plan = parts[1].lower()
        days = max(1, int(parts[2]))
        expires = await asyncio.to_thread(storage.set_plan, user_id, plan, days)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        labels = {"bronze": "🥉 Bronze", "platinum": "💎 Platinum", "diamond": "💎 Diamond"}
        await _swallow(message)
        await _panel_edit(context, uid, f"✅ {labels[plan]} granted to <code>{user_id}</code> until <b>{date}</b>.", InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Plan Management", callback_data="mfa:plans"),
             InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")]
        ]))
        return

    if action == "revoke_plan":
        value = (message.text or "").strip()
        if not value.isdigit():
            await _panel_edit(context, uid, "⚠️ Numeric Telegram user ID bhejo.", _back_keyboard())
            return
        await asyncio.to_thread(storage.set_plan, int(value), "free", 0)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await _swallow(message)
        await _panel_edit(context, uid, f"✅ Plan revoked for <code>{value}</code>. User is back on Free.", InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Plan Management", callback_data="mfa:plans"),
             InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")]
        ]))
        return

    if action == "file_limit":
        parts = (message.text or "").strip().lower().split()
        if len(parts) != 2 or parts[0] not in {"free", "bronze", "platinum", "diamond", "admin"} or not parts[1].isdigit():
            await _panel_edit(context, uid, "⚠️ Format: <code>free|bronze|platinum|diamond|admin MB</code>. Dobara bhejo.", _back_keyboard())
            return
        role, mb = parts[0], int(parts[1])
        if (role == "admin" and not 0 <= mb <= 100000) or (role != "admin" and not 1 <= mb <= 100000):
            await _panel_edit(context, uid, "⚠️ Admin limit 0–100000 MB; baaki plans 1–100000 MB hone chahiye.", _back_keyboard())
            return
        limits = await asyncio.to_thread(storage.set_file_limit, role, mb)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await _swallow(message)
        await _panel_edit(
            context, uid,
            f"✅ <b>{role.title()} limit updated.</b>\n\n"
            f"Free: <b>{limits['free']} MB</b> • Bronze: <b>{limits['bronze']} MB</b>\n"
            f"Platinum: <b>{limits['platinum']} MB</b> • Diamond: <b>{limits['diamond']} MB</b>\n"
            f"Admin/Owner: <b>{'Unlimited' if limits['admin'] == 0 else str(limits['admin']) + ' MB'}</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Plan Limits", callback_data="mfa:filelimits"),
                 InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")]
            ]),
        )
        return

    if action == "payment_config":
        parts = (message.text or "").strip().split()
        if len(parts) not in {5, 7} or not all(x.isdigit() for x in parts[1:]):
            await _panel_edit(context, uid, "⚠️ Format: <code>UPI_ID BRONZE_PRICE BRONZE_DAYS PLATINUM_PRICE PLATINUM_DAYS DIAMOND_PRICE DIAMOND_DAYS</code>. Dobara bhejo.", _back_keyboard())
            return
        if len(parts) == 7:
            values = {"upi_id": parts[0], "bronze_price": int(parts[1]), "bronze_duration_days": int(parts[2]), "platinum_price": int(parts[3]), "platinum_duration_days": int(parts[4]), "diamond_price": int(parts[5]), "diamond_duration_days": int(parts[6]), "duration_days": 30, "currency": "INR"}
        else:
            values = {"upi_id": parts[0], "bronze_price": int(parts[1]), "platinum_price": int(parts[2]), "diamond_price": int(parts[3]), "duration_days": int(parts[4]), "bronze_duration_days": int(parts[4]), "platinum_duration_days": int(parts[4]), "diamond_duration_days": int(parts[4]), "currency": "INR"}
        durations = [values["bronze_duration_days"], values["platinum_duration_days"], values["diamond_duration_days"]]
        if min(values["bronze_price"], values["platinum_price"], values["diamond_price"]) <= 0 or any(days < 1 or days > 3650 for days in durations):
            await _panel_edit(context, uid, "⚠️ Prices > 0 aur har plan ki duration 1–3650 days honi chahiye. Dobara bhejo.", _back_keyboard())
            return
        await asyncio.to_thread(storage.set_payment_settings, values)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await _swallow(message)
        await _panel_edit(context, uid, "✅ <b>Payment settings saved.</b>\n\n📱 UPI: <code>%s</code>\n🥉 %s INR • 💎 %s INR • 💎 %s INR\n⏳ %s days" % (values["upi_id"], values["bronze_price"], values["platinum_price"], values["diamond_price"], values["duration_days"]), InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mfa:payments")]]))
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
            "Use the button below to update a limit.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Change a limit", callback_data="mfa:filelimit")],
                [InlineKeyboardButton("🔙 Back", callback_data="mfa:runtime"),
                 InlineKeyboardButton("❌ Close", callback_data="mfa:close")]
            ]))
        return

    if action == "filelimit":
        _PENDING_ADMIN_ACTIONS[uid] = "file_limit"
        await query.message.edit_text(
            "📦 <b>Change Plan Limit</b>\n\n"
            "Send one line: <code>free|bronze|platinum|diamond|admin MB</code>\n"
            "Example: <code>platinum 1500</code>\n"
            "Admin limit: <code>0</code> means unlimited.",
            parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "upload":
        configured = mtproto_uploader.configured
        connected = mtproto_uploader.ready
        if configured and not connected:
            await mtproto_uploader.start()
            connected = mtproto_uploader.ready
        if connected:
            account = "Premium" if mtproto_uploader.account_is_premium else "Standard"
            ceiling = mtproto_uploader.telegram_single_file_limit_mb
            account_text = f"{account} • {ceiling} MB MTProto ceiling"
        else:
            account_text = "Not connected"
        bot_limit = int(settings.local_bot_api_max_upload_mb) if settings.telegram_api_base_url else 50
        bot_text = f"Local Bot API ≤{bot_limit} MB" if settings.telegram_api_base_url else "Cloud Bot API ≤50 MB"
        await query.message.edit_text(
            "🚀 <b>Telegram Upload Engine</b>\n\n"
            f"🤖 Bot API: <b>{bot_text}</b>\n"
            f"🔐 MTProto session: <b>{'Configured' if configured else 'Not configured'}</b>\n"
            f"🟢 Connection: <b>{'Ready' if connected else 'Offline'}</b>\n"
            f"📦 Account transport: <b>{account_text}</b>\n"
            f"✂️ MediaFetch split size: <b>{settings.large_upload_split_mb} MB</b>\n\n"
            "Files above the configured 2 GB split size are sent sequentially as "
            "parts. Plan limits are separate from Telegram transport limits.",
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
            "Review ke baad <b>Confirm & Send</b> hoga. Source message ko broadcast review complete hone tak chat mein rehne do.",
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
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        await query.message.edit_text(
            f"📢 <b>Broadcast finished</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode="HTML", reply_markup=_main_keyboard())
        return

    if action == "broadcast_cancel":
        source = _PENDING_BROADCASTS.pop(uid, None)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        if source:
            try:
                await context.bot.delete_message(chat_id=source[0], message_id=source[1])
            except Exception:
                pass
        await _render_home(query.message)
        return

    if action == "cookies":
        _PENDING_ADMIN_ACTIONS[uid] = "cookies_import"
        await query.message.edit_text(
            "🍪 <b>YouTube Cookies</b>\n\nNetscape-format <code>cookies.txt</code> ko document ke roop mein bhejo. Import hone ke baad input message delete ho jayega.",
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
            await _panel_edit(context, uid, "⚠️ Channel detect nahi hua. Target channel ka actual message forward karo.", _back_keyboard())
            return
        kind = "dump" if action == "set_dump" else "links"
        await asyncio.to_thread(storage.set_channel_config, kind, chat.id)
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        label = "Dump" if kind == "dump" else "Links Log"
        await _swallow(message)
        await _panel_edit(
            context, uid,
            f"✅ <b>{label} channel configured.</b>\n\n"
            f"Channel: <b>{chat.title or chat.username or chat.id}</b>\nID: <code>{chat.id}</code>\n\n"
            "Bot ko target channel me admin/post permission do.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Log Channels", callback_data="mfa:channels"),
                 InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")]
            ]))
        return

    if action == "cookies_import":
        source = message.document
        if not source:
            await _panel_edit(context, uid, "⚠️ <code>cookies.txt</code> ko document ke roop mein bhejo.", _back_keyboard())
            return
        filename = (source.file_name or "").lower()
        if not (filename.endswith(".txt") or filename.endswith(".cookies")):
            await _panel_edit(context, uid, "⚠️ File ka extension .txt ya .cookies hona chahiye.", _back_keyboard())
            return
        target = Path(settings.ytdlp_cookies_file)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tg_file = await context.bot.get_file(source.file_id)
            await tg_file.download_to_drive(custom_path=str(target))
            raw = target.read_text(encoding="utf-8", errors="replace")
            rows = [line.strip() for line in raw.splitlines() if line.strip()]
            if not any(line in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"} for line in rows[:5]):
                target.unlink(missing_ok=True)
                await _panel_edit(context, uid, "⚠️ Invalid Netscape cookies.txt format.", _back_keyboard())
                return
            valid = sum(1 for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#") and len(line.split("\t")) >= 7)
            if valid == 0 or target.stat().st_size > 5 * 1024 * 1024:
                target.unlink(missing_ok=True)
                await _panel_edit(context, uid, "⚠️ Cookie file empty/invalid hai ya 5 MB se badi hai.", _back_keyboard())
                return
            _PENDING_ADMIN_ACTIONS.pop(uid, None)
            await _swallow(message)
            await _panel_edit(context, uid, f"🍪 <b>Cookies imported.</b> Entries: <b>{valid}</b>", _back_keyboard())
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.warning("Panel cookie import failed user=%s error_type=%s", uid, type(exc).__name__)
            await _panel_edit(context, uid, "❌ Cookie import failed. File format check karke dobara bhejo.", _back_keyboard())
        return

    if action == "tasklimit":
        value = (message.text or "").strip()
        if not value.isdigit() or not 1 <= int(value) <= 20:
            await _panel_edit(context, uid, "⚠️ Task limit 1–20 hona chahiye. Correct number dobara bhejo.", _back_keyboard())
            return
        limit = await asyncio.to_thread(storage.set_concurrent_download_limit, int(value))
        _PENDING_ADMIN_ACTIONS.pop(uid, None)
        await _swallow(message)
        await _panel_edit(context, uid, f"✅ <b>Concurrent download limit set to {limit}.</b>", _runtime_keyboard())
        return

    if action == "broadcast":
        _PENDING_BROADCASTS[uid] = (message.chat_id, message.message_id)
        _PENDING_ADMIN_ACTIONS[uid] = "broadcast_review"
        await _panel_edit(
            context, uid,
            "📢 <b>Broadcast Review</b>\n\nMessage receive ho gaya. "
            "Sab users ko bhejne se pehle confirm karo. Source message review ke baad automatically delete hoga.",
            InlineKeyboardMarkup([
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
    await asyncio.to_thread(storage.set_plan, int(context.args[0]), "free", 0)
    await update.message.reply_text("✅ Paid plan revoked; user is back on Free.")


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
    role = context.args[0].lower()
    mb = int(context.args[1])
    valid = 0 <= mb <= 100000 if role == "admin" else 1 <= mb <= 100000
    if not valid:
        await update.message.reply_text("Limit must be 0 for admin or 1–100000 MB for user tiers.")
        return
    limits = await asyncio.to_thread(storage.set_file_limit, role, mb)
    admin_label = "Unlimited" if limits["admin"] == 0 else f"{limits['admin']} MB"
    await update.message.reply_text(
        "⚙️ <b>File limits updated</b>\n\n"
        f"🆓 Free: <b>{limits['free']} MB</b>\n"
        f"🥉 Bronze: <b>{limits['bronze']} MB</b>\n"
        f"💎 Platinum: <b>{limits['platinum']} MB</b>\n"
        f"💎 Diamond: <b>{limits['diamond']} MB</b>\n"
        f"👑 Admin/Owner: <b>{admin_label}</b>",
        parse_mode="HTML")
