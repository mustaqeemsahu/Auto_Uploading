import asyncio
import logging
import re

from telethon import events
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from services.parser import get_buttons, parse_tme_url

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


def is_video_message(message):
    return bool(
        getattr(message, "video", None)
        or (
            getattr(message, "document", None)
            and (message.document.mime_type or "").startswith("video/")
        )
    )


def extract_store_link(message):
    """Extract a generated link from message text/entities or inline URL buttons."""
    raw = message.raw_text or ""
    match = URL_RE.search(raw)
    if match:
        return match.group(0).rstrip(").,]")

    # Telegram may expose a link as a text entity rather than visible raw text.
    for entity in getattr(message, "entities", None) or []:
        if getattr(entity, "url", None):
            return entity.url.strip()

    for button in get_buttons(message):
        url = button.get("url")
        if url:
            return url.strip()
    return None


class BotAutomation:
    def __init__(self, client, rename_bot, store_bot):
        self.client = client
        self.rename_bot = rename_bot
        self.store_bot = store_bot

    def _listen(self, peer, predicate):
        """Subscribe before an action; no polling, arbitrary timeout, or sleep."""
        future = asyncio.get_running_loop().create_future()

        async def handler(event):
            message = event.message
            if getattr(message, "out", False):
                return
            try:
                if predicate(message) and not future.done():
                    future.set_result(message)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

        new_builder = events.NewMessage(chats=peer)
        edit_builder = events.MessageEdited(chats=peer)
        self.client.add_event_handler(handler, new_builder)
        self.client.add_event_handler(handler, edit_builder)
        return future, handler, new_builder, edit_builder

    def _unlisten(self, handler, new_builder, edit_builder):
        self.client.remove_event_handler(handler, new_builder)
        self.client.remove_event_handler(handler, edit_builder)

    async def _send_and_wait(self, peer, action, predicate):
        future, handler, new_builder, edit_builder = self._listen(peer, predicate)
        try:
            await action()
            return await future
        finally:
            self._unlisten(handler, new_builder, edit_builder)

    async def _resolve_channel(self, channel):
        """Resolve a channel from dialogs so Telethon has its access_hash."""
        if isinstance(channel, str) and not channel.lstrip("-").isdigit():
            return await self.client.get_entity(channel)
        wanted = abs(int(channel))
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "id", None) == wanted:
                return entity
        raise RuntimeError(
            f"Channel {channel} is not present in the logged-in account's dialogs. "
            "Join that channel with this Telegram account first."
        )

    async def join_invite(self, url):
        parsed = parse_tme_url(url)
        if not parsed:
            return False
        try:
            if parsed["kind"] == "invite":
                await self.client(ImportChatInviteRequest(parsed["hash"]))
                return True
            if parsed["kind"] in ("public_channel", "public_post"):
                await self.client(JoinChannelRequest(parsed["channel"]))
                return True
        except UserAlreadyParticipantError:
            return True
        except Exception as exc:
            log.warning("Could not join %s: %s", url, exc)
        return False

    async def process_retrieval_bot(self, bot_username, payload=None):
        peer = await self.client.get_entity(bot_username)
        command = "/start" + (f" {payload}" if payload else "")
        log.info("Requesting video from retrieval bot %s", bot_username)

        async def send():
            await self.client.send_message(peer, command)

        return await self._send_and_wait(peer, send, is_video_message)

    async def resolve_quality_link(self, button):
        if button.get("url"):
            return await self.process_quality_url(button["url"])

        source_message = button.get("_message")
        if source_message is not None and button.get("has_callback"):
            log.info("Clicking callback quality button: %s", button["label"])
            # Register a video listener for the retrieval bot before callback click.
            # A callback button may return an alert or open a bot flow; the actual
            # video is awaited in the retrieval bot conversation below.
            await source_message.click(i=button["row"], j=button["col"])
            raise RuntimeError(
                f"Quality button {button['label']} is callback-only and did not "
                "provide a bot URL. Its callback response must identify the bot "
                "or payload; refusing to guess a retrieval destination."
            )
        raise RuntimeError(f"Quality button {button['label']} has no URL/callback")

    async def process_quality_url(self, url):
        parsed = parse_tme_url(url)
        if not parsed:
            raise RuntimeError(f"Unsupported quality URL: {url}")

        if parsed["kind"] == "bot_start":
            return await self.process_retrieval_bot(
                "@" + parsed["bot"], parsed["payload"]
            )

        if parsed["kind"] in ("public_post", "private_post"):
            entity = await self._resolve_channel(parsed["channel"])
            message = await self.client.get_messages(
                entity, ids=parsed["message_id"]
            )
            if not message:
                raise RuntimeError("Quality link target post was not found")
            for nested in get_buttons(message):
                nested_url = nested.get("url")
                nested_parsed = parse_tme_url(nested_url)
                if nested_parsed and nested_parsed["kind"] == "bot_start":
                    return await self.process_retrieval_bot(
                        "@" + nested_parsed["bot"], nested_parsed["payload"]
                    )
            raise RuntimeError("Quality post has no supported retrieval-bot start link")

        raise RuntimeError(f"Unsupported quality target: {parsed['kind']}")

    async def retrieve_quality(self, quality_button):
        return await self.resolve_quality_link(quality_button)

    async def rename(self, source_video):
        peer = await self.client.get_entity(self.rename_bot)
        log.info("Forwarding retrieved video to rename bot")

        async def send():
            await self.client.forward_messages(peer, source_video)

        return await self._send_and_wait(peer, send, is_video_message)

    @staticmethod
    def _has_generate_button(message):
        return any(
            "GENERATE LINK" in button["label"].upper()
            for button in get_buttons(message)
        )

    @staticmethod
    def _is_store_ready_message(message):
        text = (message.raw_text or "").lower()
        return (
            "stored messages" in text
            and BotAutomation._has_generate_button(message)
        )

    @staticmethod
    def _is_store_prompt(message):
        text = (message.raw_text or "").lower()
        return (
            "send me the messages you want to store" in text
            or "send the messages you want to store" in text
            or "send me the file" in text
        )

    async def store_link(self, renamed_video):
        peer = await self.client.get_entity(self.store_bot)
        log.info("Starting File Store workflow for renamed message %s", renamed_video.id)

        # Phase 1: issue /genlink and WAIT for the bot's explicit upload prompt.
        prompt_future, prompt_handler, prompt_new, prompt_edit = self._listen(
            peer, self._is_store_prompt
        )
        try:
            await self.client.send_message(peer, "/genlink")
            log.info("Sent /genlink; waiting for File Store upload prompt")
            await prompt_future
        finally:
            self._unlisten(prompt_handler, prompt_new, prompt_edit)

        # Phase 2: only now forward the renamed file, then wait for the
        # Stored Messages keyboard. This fixes the race shown in the screenshots.
        ready_future, ready_handler, ready_new, ready_edit = self._listen(
            peer, self._is_store_ready_message
        )
        try:
            log.info("File Store upload prompt received; forwarding renamed video")
            await self.client.forward_messages(peer, renamed_video)
            log.info("Renamed video forwarded; waiting for Generate Link keyboard")
            ready_message = await ready_future
        finally:
            self._unlisten(ready_handler, ready_new, ready_edit)

        generate_button = next(
            button for button in get_buttons(ready_message)
            if "GENERATE LINK" in button["label"].upper()
        )

        # Phase 3: listen for the link before clicking to avoid missing a fast reply.
        link_future, link_handler, link_new, link_edit = self._listen(
            peer, lambda message: bool(extract_store_link(message))
        )
        try:
            log.info(
                "Clicking GENERATE LINK on File Store message %s at row=%s col=%s",
                ready_message.id, generate_button["row"], generate_button["col"]
            )
            await ready_message.click(
                i=generate_button["row"], j=generate_button["col"]
            )
            result_message = await link_future
        finally:
            self._unlisten(link_handler, link_new, link_edit)

        link = extract_store_link(result_message)
        if not link:
            raise RuntimeError("File Store response contained no generated URL")
        log.info("File Store link captured: %s", link)
        return link

    async def process_video(self, video_message):
        renamed = await self.rename(video_message)
        link = await self.store_link(renamed)
        return renamed, link
