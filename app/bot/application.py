from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.admin import (
    admin_command,
    broadcast_command,
    cookies_clear,
    cookies_command,
    maintenance_command,
    premium_command,
    revoke_premium,
)
from app.bot.handlers import (
    about,
    download_choice,
    handle_url,
    help_command,
    premium_status,
    start,
    supported,
)
from app.core.config import settings


async def _error_handler(update, context) -> None:
    import logging
    logger = logging.getLogger("mediafetch")
    logger.error(
        "Unhandled Telegram handler error: update_id=%s error=%r",
        getattr(update, "update_id", None),
        context.error,
        exc_info=context.error,
    )


def build_application() -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required to start MediaFetch.")

    builder = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(8)
    )
    if settings.webhook_mode:
        # The FastAPI service owns the webhook endpoint in Koyeb.
        builder = builder.updater(None)

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("supported", supported))
    application.add_handler(CommandHandler("premium", premium_status))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("premium_grant", premium_command))
    application.add_handler(CommandHandler("revoke", revoke_premium))
    application.add_handler(CommandHandler("maintenance", maintenance_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CommandHandler("cookies_clear", cookies_clear))

    application.add_handler(CallbackQueryHandler(download_choice, pattern=r"^mf:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    application.add_error_handler(_error_handler)
    return application
