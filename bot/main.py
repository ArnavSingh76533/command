import logging

from telegram import Update
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, MessageHandler, filters

from .config import Config
from .db import DB
from .llm import LLM
from .service import Service


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP URLs contain Telegram bot tokens. Do not log HTTP request URLs or update bodies.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = Config.load()
    db = DB(config.database)
    llm = LLM(config, db)
    service = Service(config, db, llm)

    async def shutdown(app):
        await llm.close()
        db.close()

    async def stop(app):
        await service.stop_workers()

    async def error(update, context):
        logging.getLogger(__name__).error("Update handler failed: %s", type(context.error).__name__)
        await service.alert("handler", "An update handler failed. Check server logs and bot permissions.")

    app = (
        Application.builder()
        .token(config.token)
        .rate_limiter(AIORateLimiter(max_retries=2))
        .post_init(service.startup)
        .post_stop(stop)
        .post_shutdown(shutdown)
        .build()
    )
    app.add_handler(CallbackQueryHandler(service.on_callback))
    app.add_handler(MessageHandler(filters.ALL, service.on_message))
    app.add_error_handler(error)
    # Telegram queues updates for only a limited time. Never discard queued messages at startup.
    app.run_polling(
        allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
