from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Update

from app.bot.application import build_application


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = build_application()
    app.state.bot = bot

    await bot.initialize()
    await bot.start()
    if bot.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await bot.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    yield

    await bot.updater.stop()
    await bot.stop()
    await bot.shutdown()


app = FastAPI(title="MediaFetch", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "MediaFetch"}
