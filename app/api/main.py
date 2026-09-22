import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from app.bot.application import build_application
from app.core.config import settings


def _webhook_base_url() -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    if settings.koyeb_public_domain:
        return f"https://{settings.koyeb_public_domain.rstrip('/')}"
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
                "WEBHOOK_MODE requires PUBLIC_BASE_URL or KOYEB_PUBLIC_DOMAIN."
            )

        webhook_secret = settings.webhook_secret or secrets.token_urlsafe(32)
        app.state.webhook_secret = webhook_secret

        await bot.bot.set_webhook(
            url=f"{webhook_base_url}/telegram/webhook",
            secret_token=webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        if bot.updater is None:
            raise RuntimeError("Telegram updater is unavailable.")
        await bot.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    yield

    for task in list(app.state.webhook_tasks):
        task.cancel()
    if app.state.webhook_tasks:
        await asyncio.gather(*app.state.webhook_tasks, return_exceptions=True)

    if settings.webhook_mode:
        await bot.bot.delete_webhook()
    elif bot.updater is not None:
        await bot.updater.stop()

    await bot.stop()
    await bot.shutdown()


app = FastAPI(title="MediaFetch", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "MediaFetch"}


async def _process_webhook_update(update: Update) -> None:
    try:
        await app.state.bot.process_update(update)
    finally:
        app.state.webhook_tasks.discard(asyncio.current_task())


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected_secret = getattr(app.state, "webhook_secret", "")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    payload = await request.json()
    update = Update.de_json(payload, app.state.bot.bot)

    task = asyncio.create_task(_process_webhook_update(update))
    app.state.webhook_tasks.add(task)
    return {"ok": True}
