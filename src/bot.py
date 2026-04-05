import logging
import os

from telegram.ext import ApplicationBuilder, MessageHandler, filters

from . import db
from .handlers import handle_message
from .scheduler import check_due_reminders, check_expired_pauses

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def post_init(app):
    await db.init_schema()
    logger.info("DB schema initialized")

    jq = app.job_queue
    jq.run_repeating(lambda ctx: check_due_reminders(app),    interval=300,  first=10)
    jq.run_repeating(lambda ctx: check_expired_pauses(app),   interval=900,  first=30)
    logger.info("Scheduler jobs registered")


def main():
    token = os.environ["TELEGRAM_TOKEN"]

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
