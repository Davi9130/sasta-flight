import logging
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.config import BOT_TOKEN, TIMEZONE
from bot.db import Database
from bot import handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SCAN_JOB_PREFIX = "scan_route_"


async def schedule_scan_jobs(application: Application):
    """Schedule or reschedule per-route scan jobs."""
    # Remove all existing scan jobs (.jobs() available in python-telegram-bot>=21.0)
    for job in application.job_queue.jobs():
        if job.name and job.name.startswith(SCAN_JOB_PREFIX):
            job.schedule_removal()

    notify_time = await handlers.db.get_config("notify_time")
    hour, minute = map(int, notify_time.split(":"))
    tz = ZoneInfo(TIMEZONE)

    routes = await handlers.db.get_active_routes()
    for route in routes:
        interval_minutes = await handlers.db.get_route_scan_interval(route["id"])
        interval_secs = interval_minutes * 60

        now = datetime.now(tz)
        today_notify = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if now < today_notify:
            first = today_notify - now
        else:
            elapsed = (now - today_notify).total_seconds()
            slots_passed = int(elapsed // interval_secs)
            next_slot = today_notify + timedelta(seconds=(slots_passed + 1) * interval_secs)
            first = next_slot - now
            if first.total_seconds() > interval_secs:
                first = timedelta(seconds=0)

        application.job_queue.run_repeating(
            handlers._scheduled_scan_route,
            interval=interval_secs,
            first=first,
            data=route,
            name=f"{SCAN_JOB_PREFIX}{route['id']}",
        )

    logger.info(
        f"Scheduled {len(routes)} route scan jobs (notify_time={notify_time} {TIMEZONE})"
    )


async def post_init(application: Application):
    """Called after bot is initialized."""
    db = Database()
    await db.init()
    handlers.db = db
    from bot.fx import FxService

    handlers.fx_service = FxService(db)
    await schedule_scan_jobs(application)
    logger.info("SastaFlight bot started")


async def post_shutdown(application: Application):
    """Called on shutdown."""
    if handlers.db:
        await handlers.db.close()


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", handlers.start_command))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("add", handlers.add_command))
    application.add_handler(CommandHandler("remove", handlers.remove_command))
    application.add_handler(CommandHandler("routes", handlers.routes_command))
    application.add_handler(CommandHandler("check", handlers.check_command))
    application.add_handler(CommandHandler("time", handlers.time_command))
    application.add_handler(CommandHandler("history", handlers.history_command))
    application.add_handler(CommandHandler("pause", handlers.pause_command))
    application.add_handler(CommandHandler("resume", handlers.resume_command))
    application.add_handler(CommandHandler("stops", handlers.stops_command))
    application.add_handler(CallbackQueryHandler(handlers.stops_callback, pattern=r"^stops_"))
    application.add_handler(CommandHandler("frequency", handlers.frequency_command))
    application.add_handler(CallbackQueryHandler(handlers.frequency_callback, pattern=r"^freq_"))
    application.add_handler(CommandHandler("alert", handlers.alert_command))

    application.run_polling()


if __name__ == "__main__":
    main()
