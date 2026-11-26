import asyncio, logging, re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from config import Config
from services.parser import parse_episode, episode_key, get_buttons, is_quality, parse_tme_url
from services.telegram_bots import BotAutomation
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

class MahiruPipeline:
    def __init__(self, app, store):
        self.app, self.store = app, store
        self.client = TelegramClient(StringSession(Config.USER_SESSION), Config.API_ID, Config.API_HASH)
        self.flow = BotAutomation(self.client, Config.RENAME_BOT, Config.FILE_STORE_BOT, Config.BOT_TIMEOUT)
        self.lock = asyncio.Lock()
        self.handlers = []

    async def start(self):
        await self.client.start()
        for source in Config.SOURCE_CHANNELS:
            entity = await self.client.get_entity(int(source) if source.lstrip("-").isdigit() else source)
            handler = self._make_handler(entity)
            self.client.add_event_handler(handler, events.NewMessage(chats=entity))
            self.handlers.append(handler)
        log.info("Monitoring %d source channels", len(self.handlers))

    def _make_handler(self, entity):
        async def handler(event):
            if self.app.bot_data.get("paused"):
                return
            async with self.lock:
                try:
                    await self.process_announcement(event.message)
                except FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds)
                except Exception:
                    log.exception("Announcement processing failed")
        return handler

    async def process_announcement(self, announcement):
        caption = announcement.message or ""
        info = parse_episode(caption)
        buttons = get_buttons(announcement)
        watch = next((b for b in buttons if "WATCH" in b["label"].upper()), None)
        if not watch or not watch.get("url"):
            log.info("Skipping post without a WATCH URL: %s", announcement.id)
            return
        key = episode_key(info)
        existing = await self.store.get_episode(key)
        if existing and existing.get("status") == "published":
            return
        await self.store.create_episode(key, {
            "title": info["title"], "season": info["season"], "episode": info["episode"],
            "audio": info["audio"], "source_chat_id": announcement.chat_id,
            "source_message_id": announcement.id, "status": "processing"
        })
        target_post = await self.open_watch_target(watch["url"], info)
        quality_buttons = [
            dict(b, _message=target_post)
            for b in get_buttons(target_post)
            if is_quality(b["label"])
        ]
        if not quality_buttons:
            raise RuntimeError("WATCH target post has no quality buttons")
        for button in quality_buttons[:Config.MAX_QUALITY_JOBS]:
            doc = await self.store.get_episode(key)
            if doc and button["label"] in doc.get("qualities", {}):
                continue
            await self.store.set_status(key, "processing")
            try:
                video = await self.flow.retrieve_quality(button)
                renamed, link = await self.flow.process_video(video)
                await self.store.update_quality(key, button["label"], link, renamed.message or "")
            except Exception as exc:
                await self.store.set_status(key, "partial", str(exc))
                log.exception("Quality %s failed; continuing with remaining qualities", button["label"])
                continue
        await self.publish_if_ready(key)

    async def open_watch_target(self, url, info):
        parsed = parse_tme_url(url)
        if not parsed:
            raise RuntimeError("Unsupported WATCH NOW URL")
        if parsed["kind"] == "public_post":
            entity = await self.client.get_entity(parsed["channel"])
            return await self.client.get_messages(entity, ids=parsed["message_id"])
        if parsed["kind"] == "private_post":
            entity = await self.client.get_entity(parsed["channel"])
            return await self.client.get_messages(entity, ids=parsed["message_id"])
        if parsed["kind"] == "public_channel":
            entity = await self.client.get_entity(parsed["channel"])
            messages = await self.client.get_messages(entity, limit=40)
            return self.find_episode_post(messages, info)
        if parsed["kind"] == "invite":
            await self.flow.join_invite(url)
            raise RuntimeError("Joined invitation; target channel needs a post link to continue")
        raise RuntimeError("WATCH NOW link is not a channel/post URL")

    def find_episode_post(self, messages, info):
        title_tokens = [t for t in re.findall(r"\w+", info["title"].lower()) if len(t) > 2]
        best, best_score = None, 0
        for msg in messages:
            text = (msg.message or "").lower()
            score = sum(1 for token in title_tokens if token in text)
            if info["episode"] is not None and re.search(rf"\b(?:e|ep|episode)\s*0?{info['episode']}\b", text):
                score += 3
            if info["season"] is not None and re.search(rf"\b(?:s|season)\s*0?{info['season']}\b", text):
                score += 2
            if score > best_score and any(is_quality(b["label"]) for b in get_buttons(msg)):
                best, best_score = msg, score
        if not best:
            raise RuntimeError("Could not find matching episode post with quality buttons")
        return best

    async def publish_if_ready(self, key):
        doc = await self.store.get_episode(key)
        if not doc or not doc.get("qualities"):
            return
        info = {
            "title": doc["title"], "season": doc.get("season"),
            "episode": doc.get("episode"), "audio": doc["audio"]
        }
        caption = (
            f"➲ Title - {info['title']}\n"
            f"✧ Season - {info['season'] if info['season'] is not None else 'Unknown'}\n"
            f"✧ Episode - {info['episode'] if info['episode'] is not None else 'Unknown'}\n"
            f"✧ Audio - {info['audio']}\n"
            f"✧ Quality - Multi Quality"
        )
        if Config.MAIN_CHANNEL_TEXT:
            caption += f"\n\n{Config.MAIN_CHANNEL_TEXT}"
        qualities = doc["qualities"]
        rows, current = [], []
        for label, item in qualities.items():
            current.append(InlineKeyboardButton(label, url=item["link"]))
            if len(current) == 3:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        markup = InlineKeyboardMarkup(rows)
        poster = await self.store.get_poster(info["title"].lower().strip())
        if not poster:
            poster = await self.store.get_default_poster()

        if poster:
            try:
                await self.app.bot.send_photo(
                    Config.DESTINATION_CHANNEL,
                    poster,
                    caption=caption,
                    reply_markup=markup,
                )
            except Exception:
                log.exception("Poster send failed; publishing episode as text instead")
                await self.app.bot.send_message(
                    Config.DESTINATION_CHANNEL,
                    caption,
                    reply_markup=markup,
                )
        else:
            await self.app.bot.send_message(
                Config.DESTINATION_CHANNEL,
                caption,
                reply_markup=markup,
            )
        await self.store.set_status(key, "published")

    async def stop(self):
        await self.client.disconnect()
