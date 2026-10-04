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

# Owner-only temporary admin-panel actions.
_PENDING_ADMIN_ACTIONS: dict[int, str] = {}


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
        [
            InlineKeyboardButton("📊 Overview", callback_data="mfa:overview"),
            InlineKeyboardButton("⚙️ Runtime", callback_data="mfa:runtime"),
        ],
        [
            InlineKeyboardButton("📡 Log Channels", callback_data="mfa:channels"),
            InlineKeyboardButton("📢 Broadcast", callback_data="mfa:broadcast"),
        ],
        [
            InlineKeyboardButton("🍪 Cookies", callback_data="mfa:cookies"),
            InlineKeyboardButton("🩺 Diagnostics", callback_data="mfa:diagnostics"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="mfa:close")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
            InlineKeyboardButton("❌ Close", callback_data="mfa:close"),
        ]
    ])


def _channels_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Set Dump", callback_data="mfa:setdump"),
            InlineKeyboardButton("🔗 Set Links Log", callback_data="mfa:setlinks"),
        ],
        [
            InlineKeyboardButton("🗑 Clear Dump", callback_data="mfa:cleardump"),
            InlineKeyboardButton("🗑 Clear Links", callback_data="mfa:clearlinks"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
            InlineKeyboardButton("❌ Close", callback_data="mfa:close"),
        ],
    ])


def _runtime_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ Task Limit", callback_data="mfa:tasklimit"),
            InlineKeyboardButton("📦 File Limits", callback_data="mfa:filelimits"),
        ],
        [
            InlineKeyboardButton("🔧 Maintenance", callback_data="mfa:maintenance"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data="mfa:home"),
            InlineKeyboardButton("❌ Close", callback_data="mfa:close"),
        ],
    ])


async def _render_home(message, edit: bool = True) -> None:
    stats = await asyncio.to_thread(storage.stats)
    limits = await asyncio.to_thread(storage.file_limits)
    channels = await asyncio.to_thread(storage.channel_config)
    task_limit = await asyncio.to_thread(storage.concurrent_download_limit)
    text = (
        "🛠 <b>MediaFetch Owner Panel</b>\n\n"
        f"📥 Downloads: <b>{stats['downloads']}</b>  •  "
        f"❌ Failures: <b>{stats['failures']}</b>\n"
        f"⚡ Cache hits: <b>{stats['cache_hits']}</b>  •  "
        f"👥 Users: <b>{len(await asyncio.to_thread(storage.user_ids))}</b>\n\n"
        f"⬇️ Download tasks: <b>{task_limit}</b> concurrent\n"
        f"📥 Dump: <code>{channels.get('dump') or 'Not configured'}</code>\n"
        f"🔗 Links log: <code>{channels.get('links') or 'Not configured'}</code>\n\n"
        f"🆓 Free: <b>{limits['free']} MB</b>  •  "
        f"💎 Premium: <b>{limits['premium']} MB</b>  •  "
        f"👑 Admin: <b>{limits['admin']} MB</b>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_main_keyboard())
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=_main_keyboard())


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    _PENDING_ADMIN_ACTIONS.pop(user_id, None)
    await _render_home(update.message, edit=False)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id if update.effective_user else None
    if not query or not query.message:
        return
    if not _is_owner(user_id) or not _private(update):
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1] if query.data else ""

    if action == "home":
        _PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await _render_home(query.message)
        return

    if action == "close":
        _PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await query.message.delete()
        return

    if action == "overview":
        stats = await asyncio.to_thread(storage.stats)
        await query.message.edit_text(
            "📊 <b>Overview</b>\n\n"
            f"📥 Downloads: <b>{stats['downloads']}</b>\n"
            f"⚡ Cache hits: <b>{stats['cache_hits']}</b>\n"
            f"❌ Failures: <b>{stats['failures']}</b>\n"
            f"💾 Data: <b>{stats['bytes'] / (1024 * 1024):.1f} MB</b>\n"
            f"👥 Users: <b>{len(await asyncio.to_thread(storage.user_ids))}</b>",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "runtime":
        task_limit = await asyncio.to_thread(storage.concurrent_download_limit)
        limits = await asyncio.to_thread(storage.file_limits)
        await query.message.edit_text(
            "⚙️ <b>Runtime Settings</b>\n\n"
            f"⬇️ Concurrent download tasks: <b>{task_limit}</b>\n"
            f"🆓 Free file: <b>{limits['free']} MB</b>\n"
            f"💎 Premium file: <b>{limits['premium']} MB</b>\n"
            f"👑 Admin file: <b>{limits['admin']} MB</b>\n"
            f"🔧 Maintenance: <b>{'ON' if storage.maintenance() else 'OFF'}</b>",
            parse_mode="HTML",
            reply_markup=_runtime_keyboard(),
        )
        return

    if action == "tasklimit":
        _PENDING_ADMIN_ACTIONS[user_id] = "tasklimit"
        await query.message.edit_text(
            "⬇️ <b>Task Downloading Limit</b>\n\n"
            "Send a number from <b>1–20</b>.\n"
            "This controls how many downloads can run at the same time.\n\n"
            "Example: <code>2</code>",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "filelimits":
        limits = await asyncio.to_thread(storage.file_limits)
        await query.message.edit_text(
            "📦 <b>File Limits</b>\n\n"
            f"🆓 Free: <b>{limits['free']} MB</b>\n"
            f"💎 Premium: <b>{limits['premium']} MB</b>\n"
            f"👑 Admin: <b>{limits['admin']} MB</b>\n\n"
            "Use <code>/set_limit free|premium|admin MB</code> to change them.",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "maintenance":
        enabled = not storage.maintenance()
        await asyncio.to_thread(storage.set_maintenance, enabled)
        await query.message.edit_text(
            f"🔧 <b>Maintenance mode: {'ON' if enabled else 'OFF'}</b>",
            parse_mode="HTML",
            reply_markup=_runtime_keyboard(),
        )
        return

    if action == "channels":
        channels = await asyncio.to_thread(storage.channel_config)
        await query.message.edit_text(
            "📡 <b>Operational Log Channels</b>\n\n"
            f"📥 <b>Dump:</b> <code>{channels.get('dump') or 'Not configured'}</code>\n"
            f"🔗 <b>Links Log:</b> <code>{channels.get('links') or 'Not configured'}</code>\n\n"
            "Set buttons ke baad target channel ka <b>koi bhi message forward</b> karo. "
            "Bot automatically channel configure kar dega.",
            parse_mode="HTML",
            reply_markup=_channels_keyboard(),
        )
        return

    if action in {"setdump", "setlinks"}:
        _PENDING_ADMIN_ACTIONS[user_id] = "set_dump" if action == "setdump" else "set_links"
        label = "Dump" if action == "setdump" else "Links Log"
        await query.message.edit_text(
            f"📡 <b>Configure {label} Channel</b>\n\n"
            "Target channel se <b>koi bhi message forward</b> karke yahan bhejo.\n"
            "Channel ID automatically detect hoga.\n\n"
            "Only the owner can configure this.",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action in {"cleardump", "clearlinks"}:
        kind = "dump" if action == "cleardump" else "links"
        await asyncio.to_thread(storage.set_channel_config, kind, "")
        await query.message.edit_text(
            f"🗑 <b>{'Dump' if kind == 'dump' else 'Links Log'} channel cleared.</b>",
            parse_mode="HTML",
            reply_markup=_channels_keyboard(),
        )
        return

    if action == "broadcast":
        _PENDING_ADMIN_ACTIONS[user_id] = "broadcast"
        await query.message.edit_text(
            "📢 <b>Broadcast</b>\n\n"
            "Ab jo message tum broadcast karna chahte ho, woh yahan send/forward karo.\n"
            "Text, photo, video, document, album-message — supported Telegram message ko copy karke sab known users ko bheja jayega.\n\n"
            "⚠️ Broadcast start hote hi send ho jayega.",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "cookies":
        await query.message.edit_text(
            "🍪 <b>YouTube Cookies</b>\n\n"
            "cookies.txt ko send karo aur phir <code>/cookies</code> se import karo.\n"
            "Existing secure cookie flow preserved hai.",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    if action == "diagnostics":
        stats = await asyncio.to_thread(storage.stats)
        await query.message.edit_text(
            "🩺 <b>Diagnostics</b>\n\n"
            f"yt-dlp: <code>{YTDLP_VERSION}</code>\n"
            f"Storage: <code>{'MongoDB' if storage.persistent else 'memory fallback'}</code>\n"
            f"Cookies: <code>{'loaded' if Path(settings.ytdlp_cookies_file).is_file() else 'not loaded'}</code>\n"
            f"Downloads: <code>{stats['downloads']}</code> • failures: <code>{stats['failures']}</code>",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return


async def admin_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle owner-only panel input such as forwarded channel messages."""
    user_id = update.effective_user.id if update.effective_user else None
    message = update.message
    if not _is_owner(user_id) or not message or not _private(update):
        return
    action = _PENDING_ADMIN_ACTIONS.get(user_id)
    if not action:
        return

    if action in {"set_dump", "set_links"}:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None)
        if not chat:
            await message.reply_text(
                "⚠️ Channel detect nahi hua. Please target channel ka actual message <b>forward</b> karo.",
                parse_mode="HTML",
                reply_markup=_back_keyboard(),
            )
            return
        kind = "dump" if action == "set_dump" else "links"
        config = await asyncio.to_thread(storage.set_channel_config, kind, chat.id)
        _PENDING_ADMIN_ACTIONS.pop(user_id, None)
        label = "Dump" if kind == "dump" else "Links Log"
        await message.reply_text(
            f"✅ <b>{label} channel configured.</b>\n\n"
            f"Channel: <b>{chat.title or chat.username or chat.id}</b>\n"
            f"ID: <code>{chat.id}</code>\n\n"
            "Bot ko is channel me post/copy permission ke saath admin rakho.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Log Channels", callback_data="mfa:channels")],
                [InlineKeyboardButton("🏠 Main Panel", callback_data="mfa:home")],
            ]),
        )
        return

    if action == "tasklimit":
        value = (message.text or "").strip()
        if not value.isdigit() or not 1 <= int(value) <= 20:
            await message.reply_text("⚠️ Task limit <b>1–20</b> hona chahiye.", parse_mode="HTML")
            return
        limit = await asyncio.to_thread(storage.set_concurrent_download_limit, int(value))
        _PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await message.reply_text(
            f"✅ <b>Concurrent download limit set to {limit}.</b>",
            parse_mode="HTML",
            reply_markup=_runtime_keyboard(),
        )
        return

    if action == "broadcast":
        _PENDING_ADMIN_ACTIONS.pop(user_id, None)
        users = await asyncio.to_thread(storage.user_ids)
        sent = failed = 0
        for target in users:
            try:
                await context.bot.copy_message(
                    chat_id=target,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                try:
                    await context.bot.copy_message(
                        chat_id=target,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                    )
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.reply_text(
            f"📢 <b>Broadcast finished</b>\n\n"
            f"✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )


async def diagnostics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    try:
        deno = subprocess.run(
            ["deno", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip().splitlines()[0]
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
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /premium USER_ID DAYS")
        return
    target = int(context.args[0])
    days = max(1, int(context.args[1]))
    expires = await asyncio.to_thread(storage.set_premium, target, days)
    date = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await update.message.reply_text(f"💎 Premium enabled for {target} until {date}")


async def revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke USER_ID")
        return
    await asyncio.to_thread(storage.set_premium, int(context.args[0]), -1)
    await update.message.reply_text("✅ Premium revoked.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /maintenance on|off")
        return
    enabled = context.args[0].lower() == "on"
    await asyncio.to_thread(storage.set_maintenance, enabled)
    await update.message.reply_text(f"🔧 Maintenance mode: {'ON' if enabled else 'OFF'}")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    source = update.message.reply_to_message
    if not source:
        await update.message.reply_text("Use /admin → 📢 Broadcast for the clean broadcast flow.")
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
    user_id = update.effective_user.id if update.effective_user else None
    message = update.message
    if not _is_owner(user_id) or not message or not _private(update):
        return
    source = message.reply_to_message
    if not source or not source.document:
        await message.reply_text(
            "🍪 Reply to your exported <b>cookies.txt</b> file with /cookies.\n"
            "Only the owner can import it.",
            parse_mode="HTML",
        )
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
        size = target.stat().st_size
        if size <= 0 or size > 5 * 1024 * 1024:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ Cookie file is empty or larger than 5 MB.")
            return
        raw = target.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        header_ok = any(line in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"} for line in lines[:5])
        if not header_ok:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ Invalid Netscape cookies.txt format.", parse_mode="HTML")
            return
        valid_rows = sum(
            1 for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and len(line.split("\t")) >= 7
        )
        if valid_rows == 0:
            target.unlink(missing_ok=True)
            await message.reply_text("⚠️ No valid Netscape cookie rows were found.")
            return
        await message.reply_text(
            f"🍪 <b>Cookies imported.</b> Entries: <b>{valid_rows}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
        logger.exception("Cookie import failed user=%s error_type=%s", user_id, type(exc).__name__)
        await message.reply_text("❌ Cookie import failed.")


async def cookies_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    Path(settings.ytdlp_cookies_file).unlink(missing_ok=True)
    await update.message.reply_text("🗑️ yt-dlp cookies cleared from this instance.")


async def set_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_owner(user_id) or not update.message or not _private(update):
        return
    if len(context.args) != 2 or context.args[0].lower() not in {"free", "premium", "admin"} or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /set_limit free|premium|admin MB")
        return
    mb = int(context.args[1])
    if mb < 1 or mb > 2000:
        await update.message.reply_text("Limit must be between 1 and 2000 MB.")
        return
    limits = await asyncio.to_thread(storage.set_file_limit, context.args[0], mb)
    await update.message.reply_text(
        "⚙️ <b>File limit updated</b>\n\n"
        f"🆓 Free: <b>{limits['free']} MB</b>\n"
        f"💎 Premium: <b>{limits['premium']} MB</b>\n"
        f"👑 Admin: <b>{limits['admin']} MB</b>",
        parse_mode="HTML",
    )
