from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.storage import storage

PLANS = ("bronze", "platinum", "diamond")
PLAN_LABELS = {"bronze": "🥉 Bronze", "platinum": "💎 Platinum", "diamond": "💎 Diamond"}
_UTR_RE = re.compile(r"^[A-Za-z0-9]{6,35}$")


def payment_config() -> dict[str, Any]:
    saved = storage.payment_settings()
    return {
        "upi_id": str(saved.get("upi_id") or "").strip(),
        "currency": str(saved.get("currency") or "INR").strip().upper(),
        "prices": {
            "bronze": max(0, int(saved.get("bronze_price", 0))),
            "platinum": max(0, int(saved.get("platinum_price", 0))),
            "diamond": max(0, int(saved.get("diamond_price", 0))),
        },
        "duration_days": max(1, int(saved.get("duration_days", 30))),
        "durations": {
            "bronze": max(1, int(saved.get("bronze_duration_days", saved.get("duration_days", 7)))),
            "platinum": max(1, int(saved.get("platinum_duration_days", saved.get("duration_days", 30)))),
            "diamond": max(1, int(saved.get("diamond_duration_days", saved.get("duration_days", 30)))),
        },
        "qr_url": str(saved.get("qr_url") or "").strip(),
    }


def valid_utr(utr: str) -> bool:
    return bool(_UTR_RE.fullmatch(utr.strip()))


def create_payment(user_id: int, plan: str, utr: str) -> dict[str, Any]:
    plan = plan.lower().strip()
    utr = utr.strip()
    cfg = payment_config()
    if plan not in PLANS:
        raise ValueError("Invalid plan")
    amount = int(cfg["prices"][plan])
    if amount <= 0:
        raise ValueError("This plan price is not configured")
    if not valid_utr(utr):
        raise ValueError("Invalid UTR/transaction reference")
    return storage.create_payment(
        payment_id=secrets.token_hex(6).upper(),
        user_id=user_id,
        plan=plan,
        amount=amount,
        currency=cfg["currency"],
        utr=utr,
        duration_days=int(cfg["durations"][plan]),
    )


def format_payment_time(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return "Unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def payment_summary(doc: dict[str, Any]) -> str:
    label = PLAN_LABELS.get(str(doc.get("plan")), str(doc.get("plan", "")).title())
    return (
        f"💳 <b>Payment {doc.get('payment_id', '—')}</b>\n\n"
        f"👤 User: <code>{doc.get('user_id', '—')}</code>\n"
        f"📦 Plan: <b>{label}</b>\n"
        f"💰 Amount: <b>{doc.get('amount', 0)} {doc.get('currency', 'INR')}</b>\n"
        f"🔢 UTR: <code>{doc.get('utr', '—')}</code>\n"
        f"📌 Status: <b>{str(doc.get('status', 'pending')).upper()}</b>\n"
        f"🕒 Created: <code>{format_payment_time(doc.get('created_at'))}</code>"
    )
