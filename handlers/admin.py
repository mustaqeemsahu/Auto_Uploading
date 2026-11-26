from telegram import Update
from telegram.ext import ContextTypes
from config import Config

def admin(update):
    return bool(update.effective_user and update.effective_user.id in Config.ADMIN_IDS)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    await update.effective_message.reply_text(
        "Welcome to Mahiru Auto Anime Uploader!\n"
        "Commands: /status /pause /resume /setposter /pending /retry"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    store = context.application.bot_data["store"]
    pending = await store.list_pending(100)
    await update.effective_message.reply_text(
        f"Mahiru Status: {'PAUSED' if context.application.bot_data.get('paused') else 'RUNNING'}\n"
        f"Pending/partial episodes: {len(pending)}\n"
        f"Sources: {len(Config.SOURCE_CHANNELS)}\n"
        f"Rename: {Config.RENAME_BOT}\nFile Store: {Config.FILE_STORE_BOT}"
    )

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update): return
    context.application.bot_data["paused"] = True
    await update.effective_message.reply_text("Processing paused.")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update): return
    context.application.bot_data["paused"] = False
    await update.effective_message.reply_text("Processing resumed.")

async def setposter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update): return
    msg = update.effective_message
    if not msg.photo or not context.args:
        await msg.reply_text("Send a poster photo with caption: /setposter Anime Name")
        return
    name = " ".join(context.args).strip().lower()
    await context.application.bot_data["store"].set_poster(name, msg.photo[-1].file_id)
    await msg.reply_text(f"Poster saved for {name}.")

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update): return
    docs = await context.application.bot_data["store"].list_pending(15)
    if not docs:
        await update.effective_message.reply_text("No pending episodes.")
        return
    lines = [f"• {d.get('title')} S{d.get('season')} E{d.get('episode')} — {d.get('status')}" for d in docs]
    await update.effective_message.reply_text("\n".join(lines))

async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update): return
    await update.effective_message.reply_text(
        "Retry is currently performed by reposting the source announcement after checking /pending. "
        "Automatic retry scheduling is disabled to avoid duplicate third-party bot submissions."
    )


async def setdefaultposter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    msg = update.effective_message
    if not msg.photo:
        await msg.reply_text("Send a photo with caption /setdefaultposter")
        return
    await context.application.bot_data["store"].set_default_poster(
        msg.photo[-1].file_id
    )
    await msg.reply_text("Default poster saved.")
