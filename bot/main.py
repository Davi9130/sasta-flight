import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot import handlers
from bot.config import BOT_TOKEN
from bot.runner import scan_and_notify
from bot.scheduler import ScanQueue, schedule_retry, schedule_scan_jobs
from bot.services import SERVICES_KEY, build_services, get_services

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

COMMANDS = {
    "start": handlers.start_command,
    "help": handlers.help_command,
    "add": handlers.add_command,
    "via": handlers.via_command,
    "remove": handlers.remove_command,
    "routes": handlers.routes_command,
    "check": handlers.check_command,
    "time": handlers.time_command,
    "history": handlers.history_command,
    "status": handlers.status_command,
    "pause": handlers.pause_command,
    "resume": handlers.resume_command,
    "stops": handlers.stops_command,
    "frequency": handlers.frequency_command,
    "alert": handlers.alert_command,
}


async def post_init(application: Application):
    svc = await build_services()
    application.bot_data[SERVICES_KEY] = svc
    await svc.fares.maintenance()

    async def runner(route_id: int, manual: bool, is_retry: bool) -> str:
        await svc.fares.maintenance()
        return await scan_and_notify(
            svc,
            application.bot,
            route_id,
            manual=manual,
            is_retry=is_retry,
            schedule_retry=lambda rid, delay, retry: schedule_retry(application, rid, delay, retry),
        )

    svc.queue = ScanQueue(runner)
    svc.queue.start()
    await schedule_scan_jobs(application)
    logger.info("SastaFlight bot started (provider=%s, proxies=%d)", svc.fares.name, len(svc.fares.guard.proxies))


async def post_shutdown(application: Application):
    svc = application.bot_data.get(SERVICES_KEY)
    if not svc:
        return
    if svc.queue:
        await svc.queue.stop()
    await get_services(application).db.close()


def build_application() -> Application:
    application = (
        Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    )
    for name, callback in COMMANDS.items():
        application.add_handler(CommandHandler(name, callback))
    application.add_handler(CallbackQueryHandler(handlers.stops_callback, pattern=r"^stops_"))
    application.add_handler(CallbackQueryHandler(handlers.frequency_callback, pattern=r"^freq_"))
    application.add_handler(CallbackQueryHandler(handlers.via_save_callback, pattern=r"^via_save:"))
    return application


def main():
    build_application().run_polling()


if __name__ == "__main__":
    main()
