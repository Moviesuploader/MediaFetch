from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.bot.handlers import (
    about,
    download_choice,
    handle_url,
    help_command,
    start,
    supported,
)
from app.core.config import settings


def build_application() -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required to start MediaFetch.")

    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("supported", supported))
    application.add_handler(CallbackQueryHandler(download_choice, pattern=r"^mf:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    return application
