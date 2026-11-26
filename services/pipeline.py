import asyncio
import logging
import re

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from config import Config
from services.parser import (
    parse_episode, episode_key, get_buttons, is_quality, parse_tme_url
)
from services.telegram_bots import BotAutomation
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)


class MahiruPipeline:
    def __init__(self, app, store):
        self.app = app
        self.store = store
        self.client = TelegramClient(
            StringSession(Config.USER_SESSION), Config.API_ID, Config.API_HASH
        )
        self.flow = BotAutomation(
            self.client, Config.RENAME_BOT, Config.FILE_STORE_BOT
        )
        self.lock = asyncio.Lock()
        self.handlers = []

    async def start(self):
        await self.client.start()
        for source in Config.SOURCE_CHANNELS:
            entity = await self.client.get_entity(
                int(source) if source.lstrip("-").isdigit() else source
            )
            handler = self._make_handler(entity)
            self.client.add_event_handler(
                handler, events.NewMessage(chats=entity)
            )
            self.handlers.append(handler)
            log.info(
                "Resolved source channel: %s (%s)",
                getattr(entity, "title", getattr(entity, "username", "source")),
                getattr(entity, "id", "unknown"),
            )
        log.info("Monitoring %d source channels", len(self.handlers))

    def _make_handler(self, entity):
        async def handler(event):
            if self.app.bot_data.get("paused"):
                return
            async with self.lock:
                try:
                    log.info(
                        "SOURCE MESSAGE RECEIVED chat=%s message=%s",
                        event.chat_id, event.message.id
                    )
                    await self.process_announcement(event.message)
                except FloodWaitError as exc:
                    # Telegram explicitly requires this wait; it is not an
                    # arbitrary polling delay.
                    log.warning("Telegram FloodWait: waiting %s seconds", exc.seconds)
                    await asyncio.sleep(exc.seconds)
                except Exception:
                    log.exception("Announcement processing failed")
        return handler

    async def process_announcement(self, announcement):
        caption = announcement.message or ""
        info = parse_episode(caption)
        buttons = get_buttons(announcement)
        watch = next(
            (button for button in buttons if "WATCH" in button["label"].upper()),
            None,
        )
        if not watch or not watch.get("url"):
            log.info("Skipping post without a supported WATCH URL: %s", announcement.id)
            return

        key = episode_key(info)
        existing = await self.store.get_episode(key)
        if existing and existing.get("status") == "published":
            log.info("Episode already published: %s", key)
            return

        await self.store.create_episode(key, {
            "title": info["title"],
            "season": info["season"],
            "episode": info["episode"],
            "audio": info["audio"],
            "source_chat_id": announcement.chat_id,
            "source_message_id": announcement.id,
            "status": "processing",
        })

        target_post = await self.open_watch_target(watch["url"], info)
        quality_buttons = [
            dict(button, _message=target_post)
            for button in get_buttons(target_post)
            if is_quality(button["label"])
        ]
        if not quality_buttons:
            raise RuntimeError("WATCH target post has no quality buttons")

        # Stable quality order: 480p, 720p, 1080p, HDRip, then higher tiers.
        # Process one quality fully before starting the next one.
        def quality_rank(button):
            label = button["label"].upper().replace(" ", "")
            if "480" in label: return 0
            if "720" in label: return 1
            if "1080" in label: return 2
            if "HDRIP" in label: return 3
            if "2160" in label or "4K" in label: return 4
            return 5

        quality_buttons.sort(key=quality_rank)
        log.info(
            "Detected quality buttons in sequential order: %s",
            [button["label"] for button in quality_buttons],
        )

        failures = []
        for button in quality_buttons[:Config.MAX_QUALITY_JOBS]:
            label = button["label"]
            doc = await self.store.get_episode(key)
            if doc and label in doc.get("qualities", {}):
                log.info("Skipping already stored quality: %s", label)
                continue

            log.info("Sequential processing started: %s", label)
            await self.store.set_status(key, "processing")
            try:
                video = await self.flow.retrieve_quality(button)
                log.info("Retrieved %s video; sending to rename bot", label)
                renamed, link = await self.flow.process_video(video)
                await self.store.update_quality(
                    key, label, link, renamed.message or ""
                )
                log.info("Completed quality %s; File Store link saved", label)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                await self.store.set_status(key, "partial", str(exc))
                log.exception("Quality %s failed; moving to next quality", label)
                # Sequential means no overlap; continue only after this quality
                # has failed, rather than blocking all remaining qualities.
                continue

        await self.publish_if_ready(key)
        if failures:
            log.warning("Episode completed with quality failures: %s", failures)

    async def open_watch_target(self, url, info):
        parsed = parse_tme_url(url)
        if not parsed:
            raise RuntimeError("Unsupported WATCH NOW URL")
        if parsed["kind"] in ("public_post", "private_post"):
            entity = await self.client.get_entity(parsed["channel"])
            message = await self.client.get_messages(
                entity, ids=parsed["message_id"]
            )
            if not message:
                raise RuntimeError("WATCH NOW target post not found")
            return message
        if parsed["kind"] == "public_channel":
            entity = await self.client.get_entity(parsed["channel"])
            messages = await self.client.get_messages(entity, limit=40)
            return self.find_episode_post(messages, info)
        if parsed["kind"] == "invite":
            await self.flow.join_invite(url)
            raise RuntimeError(
                "Joined invitation; a direct target post link is needed to locate qualities"
            )
        raise RuntimeError("WATCH NOW link is not a supported channel/post URL")

    def find_episode_post(self, messages, info):
        title_tokens = [
            token for token in re.findall(r"\w+", info["title"].lower())
            if len(token) > 2
        ]
        best = None
        best_score = 0
        for message in messages:
            text = (message.message or "").lower()
            score = sum(token in text for token in title_tokens)
            if info["episode"] is not None and re.search(
                rf"\b(?:e|ep|episode)\s*0?{info['episode']}\b", text
            ):
                score += 3
            if info["season"] is not None and re.search(
                rf"\b(?:s|season)\s*0?{info['season']}\b", text
            ):
                score += 2
            if score > best_score and any(
                is_quality(button["label"]) for button in get_buttons(message)
            ):
                best, best_score = message, score
        if not best:
            raise RuntimeError(
                "Could not find matching episode post with quality buttons"
            )
        return best

    async def publish_if_ready(self, key):
        doc = await self.store.get_episode(key)
        if not doc or not doc.get("qualities"):
            log.warning("No generated quality links; destination post not published")
            return

        info = {
            "title": doc["title"],
            "season": doc.get("season"),
            "episode": doc.get("episode"),
            "audio": doc["audio"],
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

        rows = []
        current = []
        for label, item in doc["qualities"].items():
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
                log.exception("Poster failed; sending episode post as text")
                await self.app.bot.send_message(
                    Config.DESTINATION_CHANNEL, caption, reply_markup=markup
                )
        else:
            await self.app.bot.send_message(
                Config.DESTINATION_CHANNEL, caption, reply_markup=markup
            )

        await self.store.set_status(key, "published")
        log.info("Published combined destination post for %s", key)

    async def stop(self):
        await self.client.disconnect()
