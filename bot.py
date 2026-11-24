import logging
from telegram.ext import Application, CommandHandler
from config import Config
from database.mongo import MongoStore
from services.pipeline import MahiruPipeline
from handlers.admin import start, status, pause, resume, setposter, pending, retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("mahiru")

async def post_init(app):
    store = MongoStore(Config.MONGO_URI, Config.MONGO_DB)
    await store.connect()
    app.bot_data["store"] = store
    app.bot_data["paused"] = False
    app.bot_data["pipeline"] = MahiruPipeline(app, store)
    await app.bot_data["pipeline"].start()
    log.info("Mahiru Auto Anime Uploader is online")

async def post_shutdown(app):
    pipeline = app.bot_data.get("pipeline")
    if pipeline:
        await pipeline.stop()
    store = app.bot_data.get("store")
    if store:
        await store.close()

def main():
    Config.validate()
    app = (Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("setposter", setposter))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("retry", retry))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
