import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from app.bot.application import build_application
from app.core.config import settings

logger = logging.getLogger("mediafetch")


def _webhook_base_url() -> str:
    # On Koyeb, always prefer the platform-provided public domain.
    if settings.koyeb_public_domain:
        return f"https://{settings.koyeb_public_domain.rstrip('/')}"
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = build_application()
    app.state.bot = bot
    app.state.webhook_tasks = set()

    await bot.initialize()
    await bot.start()

    if settings.webhook_mode:
        webhook_base_url = _webhook_base_url()
        if not webhook_base_url:
            raise RuntimeError(
                "WEBHOOK_MODE requires KOYEB_PUBLIC_DOMAIN or PUBLIC_BASE_URL."
            )

        # Do not generate an ephemeral secret here. A rolling Koyeb deployment can
        # briefly have old and new instances receiving Telegram webhooks; an
        # auto-generated per-process secret makes the old instance return 403.
        # If WEBHOOK_SECRET is empty, Telegram's secret-token check is disabled.
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
        await bot.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Telegram long polling started.")

    yield

    for task in list(app.state.webhook_tasks):
        task.cancel()
    if app.state.webhook_tasks:
        await asyncio.gather(*app.state.webhook_tasks, return_exceptions=True)

    if settings.webhook_mode:
        await bot.bot.delete_webhook(drop_pending_updates=False)
    elif bot.updater is not None:
        await bot.updater.stop()

    await bot.stop()
    await bot.shutdown()


app = FastAPI(title="MediaFetch", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "MediaFetch",
        "telegram_mode": "webhook" if settings.webhook_mode else "polling",
    }


async def _process_webhook_update(update: Update) -> None:
    try:
        logger.info("Processing Telegram update: update_id=%s", update.update_id)
        await app.state.bot.process_update(update)
        logger.info("Finished Telegram update: update_id=%s", update.update_id)
    except Exception:
        logger.exception("Unhandled Telegram update processing error: update_id=%s", update.update_id)
    finally:
        app.state.webhook_tasks.discard(asyncio.current_task())


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
        "Telegram webhook update received: update_id=%s",
        payload.get("update_id"),
    )

    task = app.state.bot.create_task(_process_webhook_update(update), update=update)
    app.state.webhook_tasks.add(task)
    return {"ok": True}
