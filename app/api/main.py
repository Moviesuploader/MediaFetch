import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from telegram import Update

from app.bot.application import build_application
from app.core.config import settings
from app.core import cashfree
from app.core.storage import storage
from app.bot.mtproto import mtproto_uploader

logger = logging.getLogger("mediafetch")


def _webhook_base_url() -> str:
    # Explicit URL wins on any hosting provider. Antideploy exposes the app
    # over HTTPS but does not document a built-in public-URL environment
    # variable, so PUBLIC_BASE_URL or ANTIDEPLOY_PUBLIC_URL must be set there.
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    if settings.antideploy_public_url:
        return settings.antideploy_public_url.rstrip("/")
    if settings.koyeb_public_domain:
        return f"https://{settings.koyeb_public_domain.rstrip('/')}"
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = build_application()
    app.state.bot = bot

    webhook_base_url = _webhook_base_url() if settings.webhook_mode else ""
    if settings.webhook_mode and not webhook_base_url:
        raise RuntimeError(
            "WEBHOOK_MODE requires PUBLIC_BASE_URL, ANTIDEPLOY_PUBLIC_URL, "
            "or KOYEB_PUBLIC_DOMAIN."
        )

    await bot.initialize()
    await bot.start()

    if settings.mtproto_upload_enabled and settings.user_session_string:
        await mtproto_uploader.start()

    if settings.webhook_mode:

        # Keep this stable across rolling deployments. An empty secret disables
        # secret-token validation and avoids old/new instance mismatches.
        webhook_secret = settings.webhook_secret.strip()
        app.state.webhook_secret = webhook_secret
        webhook_url = f"{webhook_base_url}/telegram/webhook"

        webhook_kwargs = {
            "url": webhook_url,
            "allowed_updates": Update.ALL_TYPES,
            "drop_pending_updates": False,
        }
        if webhook_secret:
            webhook_kwargs["secret_token"] = webhook_secret

        await bot.bot.set_webhook(**webhook_kwargs)
        webhook_info = await bot.bot.get_webhook_info()
        logger.info(
            "Telegram webhook configured: url=%s pending=%s last_error=%s",
            webhook_info.url,
            webhook_info.pending_update_count,
            webhook_info.last_error_message,
        )
    else:
        if bot.updater is None:
            raise RuntimeError("Telegram updater is unavailable.")

        # Polling and webhook mode are mutually exclusive. Remove any stale
        # webhook before starting the local/polling updater.
        await bot.bot.delete_webhook(drop_pending_updates=False)
        await bot.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Telegram long polling started.")

    yield

    # IMPORTANT: In webhook mode, never delete the webhook during application
    # shutdown. Koyeb can stop/restart/roll instances, and an old instance
    # deleting the webhook after a new instance configured it can silently
    # break Telegram -> Koyeb delivery. The webhook is intentionally persistent
    # and is refreshed on the next startup.
    if not settings.webhook_mode and bot.updater is not None:
        await bot.updater.stop()

    await bot.stop()
    await bot.shutdown()
    await mtproto_uploader.stop()


app = FastAPI(title="MediaFetch", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "MediaFetch",
        "health": "/health",
    }


@app.head("/")
async def root_head() -> None:
    return None


@app.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready", "service": "MediaFetch"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "MediaFetch",
        "telegram_mode": "webhook" if settings.webhook_mode else "polling",
    }


async def _verify_and_activate_cashfree_order(order_id: str) -> str:
    record = await __import__("asyncio").to_thread(storage.payment_by_gateway_order, order_id)
    if not record:
        logger.warning("Cashfree event references unknown order_id")
        return "unknown"
    order = await cashfree.fetch_order(order_id)
    if str(order.get("order_status", "")).upper() != "PAID":
        return "pending"
    try:
        amount_matches = round(float(order.get("order_amount", -1)), 2) == round(float(record.get("amount", -2)), 2)
    except (TypeError, ValueError):
        amount_matches = False
    currency_matches = str(order.get("order_currency", "")).upper() == str(record.get("currency", "INR")).upper()
    if not amount_matches or not currency_matches:
        logger.error("Cashfree order amount/currency mismatch; refusing activation")
        return "mismatch"
    try:
        approved = await __import__("asyncio").to_thread(
            storage.approve_payment,
            str(record["payment_id"]),
            0,
            int(record.get("duration_days", 30)),
        )
    except ValueError:
        current = await __import__("asyncio").to_thread(storage.payment_by_id, str(record["payment_id"]))
        return "already_paid" if current and current.get("status") == "approved" else "pending"
    try:
        bot = getattr(app.state, "bot", None)
        if bot:
            from app.core.payments import PLAN_LABELS
            await bot.bot.send_message(
                chat_id=int(approved["user_id"]),
                text=(
                    "✅ <b>Payment verified automatically!</b>\n\n"
                    f"📦 Plan: <b>{PLAN_LABELS.get(str(approved.get('plan')), str(approved.get('plan')).title())}</b>\n"
                    f"💰 Paid: <b>₹{approved.get('amount')}</b>\n"
                    f"⏳ Validity: <b>{approved.get('duration_days')} days</b>\n\n"
                    "Premium is active now. Use /premium to check your plan."
                ),
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("Cashfree payment confirmation message failed")
    logger.info("Cashfree order verified and premium activated")
    return "paid"


@app.get("/cashfree/checkout/{order_id}", response_class=HTMLResponse)
async def cashfree_checkout(order_id: str) -> HTMLResponse:
    if not cashfree.configured():
        raise HTTPException(status_code=503, detail="Cashfree checkout is not configured.")
    record = await __import__("asyncio").to_thread(storage.payment_by_gateway_order, order_id)
    if not record or record.get("provider") != "cashfree" or not record.get("payment_session_id"):
        raise HTTPException(status_code=404, detail="Checkout not found or expired.")
    if record.get("status") != "pending":
        raise HTTPException(status_code=409, detail="This payment is no longer pending.")
    return HTMLResponse(cashfree.checkout_html(str(record["payment_session_id"]))


@app.get("/cashfree/return", response_class=HTMLResponse)
async def cashfree_return(order_id: str = "") -> HTMLResponse:
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order ID.")
    try:
        result = await _verify_and_activate_cashfree_order(order_id)
    except Exception:
        logger.exception("Cashfree return status verification failed")
        result = "pending"
    if result in {"paid", "already_paid"}:
        message = "Payment verified. Your MediaFetch Premium plan is active. You can return to Telegram."
    else:
        message = "Payment status is not confirmed yet. Return to Telegram and check /premium shortly; do not pay again while the transaction is pending."
    return HTMLResponse(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>MediaFetch payment status</title></head><body style='font-family:system-ui;max-width:520px;margin:50px auto;padding:20px'>"
        "<h2>MediaFetch payment status</h2><p>" + message + "</p></body></html>"
    )


@app.post("/cashfree/webhook")
async def cashfree_webhook(
    request: Request,
    x_webhook_signature: str | None = Header(default=None),
    x_webhook_timestamp: str | None = Header(default=None),
) -> dict[str, bool]:
    if not cashfree.configured():
        raise HTTPException(status_code=503, detail="Cashfree webhook is not configured.")
    raw_body = await request.body()
    if not cashfree.verify_webhook_signature(raw_body, x_webhook_timestamp or "", x_webhook_signature or ""):
        logger.warning("Rejected Cashfree webhook: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.") from exc
    data = payload.get("data") or {}
    order = data.get("order") or {}
    order_id = str(order.get("order_id") or data.get("order_id") or "").strip()
    if not order_id:
        logger.info("Cashfree webhook ignored: event has no order ID")
        return {"ok": True}
    await _verify_and_activate_cashfree_order(order_id)
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected_secret = getattr(app.state, "webhook_secret", "")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        logger.warning("Rejected Telegram webhook request: invalid secret.")
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    payload = await request.json()
    update = Update.de_json(payload, app.state.bot.bot)
    logger.info(
        "Telegram webhook received: update_id=%s queue_before=%s",
        payload.get("update_id"),
        app.state.bot.update_queue.qsize(),
    )

    await app.state.bot.update_queue.put(update)

    logger.info(
        "Telegram update queued: update_id=%s queue_after=%s",
        payload.get("update_id"),
        app.state.bot.update_queue.qsize(),
    )
    return {"ok": True}
