import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telegram.ext import Application, CommandHandler
from config import Config
from database.mongo import MongoStore
from services.pipeline import MahiruPipeline
from handlers.admin import start, status, pause, resume, setposter, setdefaultposter, pending, retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("mahiru")


class HealthHandler(BaseHTTPRequestHandler):
    """Small HTTP endpoint used by Render to verify the web service is alive."""

    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Mahiru Auto Anime Uploader is running")

    def log_message(self, format, *args):
        log.info("Health check: " + format, *args)


def start_health_server():
    """Bind an HTTP health server to Render's assigned port."""
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="render-health-server",
        daemon=True,
    )
    thread.start()
    log.info("Render health server listening on 0.0.0.0:%s", port)
    return server

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
    health_server = start_health_server()
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
    app.add_handler(CommandHandler("setdefaultposter", setdefaultposter))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("retry", retry))
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        health_server.shutdown()
        health_server.server_close()

if __name__ == "__main__":
    main()
