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

# File Store renders its inline button in Unicode small-cap characters
# (ɢᴇɴᴇʀᴀᴛᴇ ʟɪɴᴋ), not ordinary ASCII uppercase.
_SMALL_CAPS = str.maketrans({
    "ɢ": "g", "ᴇ": "e", "ɴ": "n", "ᴇ": "e", "ʀ": "r",
    "ᴀ": "a", "ᴛ": "t", "ʟ": "l", "ɪ": "i", "ᴋ": "k",
})

def normalize_button_label(value):
    return " ".join((value or "").translate(_SMALL_CAPS).casefold().split())

def is_generate_link_button(value):
    return normalize_button_label(value) == "generate link"


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
    def _message_buttons(message):
        """Read markup from Telethon's convenience property or raw reply_markup."""
        buttons = get_buttons(message)
        if buttons:
            return buttons
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "rows", None) or []
        found = []
        for row_i, row in enumerate(rows):
            for col_i, button in enumerate(getattr(row, "buttons", None) or []):
                label = (getattr(button, "text", "") or "").strip()
                if label:
                    found.append({
                        "label": label,
                        "url": getattr(button, "url", None),
                        "row": row_i,
                        "col": col_i,
                        "has_callback": getattr(button, "data", None) is not None,
                        "callback_data": getattr(button, "data", None),
                    })
        return found

    @classmethod
    def _has_generate_button(cls, message):
        return any(
            is_generate_link_button(button["label"])
            for button in cls._message_buttons(message)
        )

    @classmethod
    def _is_store_ready_message(cls, message):
        text = (message.raw_text or "").lower()
        # Do not require the keyboard in the event predicate. Telegram updates
        # can deliver the text first and attach/refresh markup in an edit.
        return "stored messages" in text

    @staticmethod
    def _is_store_prompt(message):
        text = (message.raw_text or "").lower()
        return (
            "send me the messages you want to store" in text
            or "send the messages you want to store" in text
            or "send me the file" in text
        )

    async def _wait_or_find_recent(self, peer, future, predicate, after_id, label):
        """
        Event-first wait with a one-time history reconciliation. This catches
        updates that arrived while Telethon was reconnecting or while a handler
        was being attached. No polling loop, fixed sleep, or arbitrary timeout.
        """
        if future.done():
            return future.result()

        async for message in self.client.iter_messages(
            peer, limit=30, min_id=after_id
        ):
            if predicate(message):
                log.info("Recovered File Store %s from chat history (message %s)",
                         label, message.id)
                return message

        return await future

    async def store_link(self, renamed_video):
        peer = await self.client.get_entity(self.store_bot)
        log.info("Starting File Store workflow for renamed message %s", renamed_video.id)

        # Phase 1: send /genlink and wait for a NEW upload prompt.
        prompt_future, prompt_handler, prompt_new, prompt_edit = self._listen(
            peer, self._is_store_prompt
        )
        try:
            command_message = await self.client.send_message(peer, "/genlink")
            log.info("Sent /genlink (message %s); waiting for upload prompt",
                     command_message.id)
            prompt = await self._wait_or_find_recent(
                peer, prompt_future, self._is_store_prompt,
                command_message.id, "upload prompt"
            )
            log.info("File Store upload prompt received (message %s)", prompt.id)
        finally:
            self._unlisten(prompt_handler, prompt_new, prompt_edit)

        # Phase 2: forward only after the prompt. Listen before forwarding.
        ready_future, ready_handler, ready_new, ready_edit = self._listen(
            peer, self._is_store_ready_message
        )
        try:
            forwarded = await self.client.forward_messages(peer, renamed_video)
            forwarded_message = forwarded[0] if isinstance(forwarded, (list, tuple)) else forwarded
            forwarded_id = getattr(forwarded_message, "id", command_message.id)
            log.info(
                "Forwarded renamed video to File Store (message %s); "
                "waiting for Stored Messages status",
                forwarded_id
            )
            ready_message = await self._wait_or_find_recent(
                peer, ready_future, self._is_store_ready_message,
                forwarded_id, "Stored Messages status"
            )
        finally:
            self._unlisten(ready_handler, ready_new, ready_edit)

        # Refresh the message so the latest inline keyboard is available.
        refreshed = await self.client.get_messages(peer, ids=ready_message.id)
        if refreshed:
            ready_message = refreshed

        buttons = self._message_buttons(ready_message)
        generate_button = next(
            (
                button for button in buttons
                if is_generate_link_button(button["label"])
            ),
            None,
        )
        if not generate_button:
            log.error(
                "Stored Messages message %s has no Generate Link button. "
                "Text=%r buttons=%r",
                ready_message.id, ready_message.raw_text,
                [(b["label"], b.get("url"), b.get("has_callback")) for b in buttons]
            )
            raise RuntimeError(
                "File Store sent Stored Messages status but no Generate Link "
                "inline button was available on the refreshed message."
            )

        # Phase 3: register response listener before callback click.
        def is_generated_link(message):
            text = (message.raw_text or "").lower()
            return (
                "here is your link" in text
                or bool(extract_store_link(message))
            )

        link_future, link_handler, link_new, link_edit = self._listen(
            peer, is_generated_link
        )
        try:
            log.info(
                "Clicking File Store GENERATE LINK on message %s "
                "(row=%s col=%s)",
                ready_message.id, generate_button["row"], generate_button["col"]
            )
            await ready_message.click(
                i=generate_button["row"],
                j=generate_button["col"]
            )
            result_message = await self._wait_or_find_recent(
                peer, link_future, is_generated_link,
                ready_message.id, "generated link"
            )
        finally:
            self._unlisten(link_handler, link_new, link_edit)

        link = extract_store_link(result_message)
        if not link:
            # Re-fetch the response in case Telegram attached its URL entity
            # after the initial update.
            refreshed_result = await self.client.get_messages(
                peer, ids=result_message.id
            )
            if refreshed_result:
                link = extract_store_link(refreshed_result)
        if not link:
            raise RuntimeError(
                "File Store returned a response, but no URL was found in "
                "its text, entities, or inline URL buttons."
            )

        log.info("File Store link captured successfully from message %s",
                 result_message.id)
        return link

    async def process_video(self, video_message):
        renamed = await self.rename(video_message)
        link = await self.store_link(renamed)
        return renamed, link
