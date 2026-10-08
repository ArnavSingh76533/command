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
    logging.getLogger("telethon").setLevel(logging.WARNING)
    config = Config.load()
    db = DB(config.database)
    llm = LLM(config, db)
    service = Service(config, db, llm)

    async def shutdown(app):
        await service.history.close()
        await llm.close()
        db.close()

    async def stop(app):
        await service.stop_workers()

    async def error(update, context):
        # Log source frames and the exception class, without tokens, transcript contents or HTTP URLs.
        import traceback

        frames = traceback.extract_tb(context.error.__traceback__)
        location = "; ".join(
            f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}" for frame in frames
        )
        logging.getLogger(__name__).error(
            "Update handler failed: %s at %s", type(context.error).__name__, location
        )
        await service.alert(
            "handler",
            "An operation failed: "
            + type(context.error).__name__
            + ". Retry the command or review button; details are in the server logs.",
        )

    app = (
        Application.builder()
        .token(config.token)
        # Our durable moderation workers use asyncio, not PTB's optional scheduler.
        .job_queue(None)
        .concurrent_updates(16)
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
