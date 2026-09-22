from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.bot.handlers import handle_url, start
from app.core.config import settings


def build_application() -> Application:
    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    return application
