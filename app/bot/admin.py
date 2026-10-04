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
            InlineKeyboardButton("❌ Cancel", callback_data="mfa:cancel"),
        ]
    ])

