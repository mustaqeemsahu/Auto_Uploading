import asyncio
import logging
import re

from telethon import events
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from services.parser import get_buttons, parse_tme_url

log = logging.getLogger(__name__)

VIDEO_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\s<>]+", re.I)


def is_video_message(message):
    return bool(
        message.video
        or (
            message.document
            and (message.document.mime_type or "").startswith("video/")
        )
    )


def extract_store_link(message):
    """Read a generated link from message text or a URL inline button."""
    text = message.raw_text or ""
    match = VIDEO_RE.search(text)
    if match:
        return match.group(0).rstrip(").,]")

    for button in get_buttons(message):
        url = button.get("url")
        if url and parse_tme_url(url):
            return url.strip()
    return None


class BotAutomation:
    def __init__(self, client, rename_bot, store_bot):
        self.client = client
        self.rename_bot = rename_bot
        self.store_bot = store_bot

    def _listen(self, peer, predicate):
        """
        Register event listeners BEFORE sending/clicking so fast bot replies
        cannot be missed. There is deliberately no arbitrary timeout or sleep.
        """
        future = asyncio.get_running_loop().create_future()

        async def handler(event):
            message = event.message
            if message.out:
                return
            try:
                matched = predicate(message)
                if matched and not future.done():
                    future.set_result(message)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

        new_builder = events.NewMessage(chats=peer)
        edited_builder = events.MessageEdited(chats=peer)
        self.client.add_event_handler(handler, new_builder)
        self.client.add_event_handler(handler, edited_builder)
        return future, handler, new_builder, edited_builder

    def _unlisten(self, handler, new_builder, edited_builder):
        self.client.remove_event_handler(handler, new_builder)
        self.client.remove_event_handler(handler, edited_builder)

    async def _send_and_wait(self, peer, action, predicate):
        future, handler, new_builder, edited_builder = self._listen(peer, predicate)
        try:
            await action()
            return await future
        finally:
            self._unlisten(handler, new_builder, edited_builder)

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
            # Callback buttons are clicked on the original quality post.
            await source_message.click(i=button["row"], j=button["col"])
            raise RuntimeError(
                "Quality callback clicked. If it opens a bot chat, its resulting "
                "bot link/payload must be supplied by that source flow."
            )
        raise RuntimeError("Quality button has no supported URL or callback")

    async def process_quality_url(self, url):
        parsed = parse_tme_url(url)
        if not parsed:
            raise RuntimeError(f"Unsupported quality URL: {url}")

        if parsed["kind"] == "bot_start":
            return await self.process_retrieval_bot(
                "@" + parsed["bot"], parsed["payload"]
            )

        if parsed["kind"] in ("public_post", "private_post"):
            entity = await self.client.get_entity(parsed["channel"])
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
            raise RuntimeError(
                "Quality post has no supported retrieval-bot start link"
            )

        raise RuntimeError(f"Unsupported quality target: {parsed['kind']}")

    async def retrieve_quality(self, quality_button):
        return await self.resolve_quality_link(quality_button)

    async def rename(self, source_video):
        peer = await self.client.get_entity(self.rename_bot)
        log.info("Forwarding retrieved video to rename bot")

        async def send():
            await self.client.forward_messages(peer, source_video)

        return await self._send_and_wait(peer, send, is_video_message)

    async def store_link(self, renamed_video):
        peer = await self.client.get_entity(self.store_bot)

        def is_generate_prompt(message):
            return any(
                "GENERATE LINK" in button["label"].upper()
                for button in get_buttons(message)
            )

        log.info("Starting File Store /genlink workflow")

        # Listen before sending /genlink and forwarding, so the prompt cannot
        # be missed even if the bot responds immediately.
        prompt_future, prompt_handler, prompt_new, prompt_edit = self._listen(
            peer, is_generate_prompt
        )
        try:
            await self.client.send_message(peer, "/genlink")
            await self.client.forward_messages(peer, renamed_video)
            prompt = await prompt_future
        finally:
            self._unlisten(prompt_handler, prompt_new, prompt_edit)

        generate_button = next(
            (
                button for button in get_buttons(prompt)
                if "GENERATE LINK" in button["label"].upper()
            ),
            None,
        )
        if not generate_button:
            raise RuntimeError("File Store GENERATE LINK button was not found")

        # Register the result listener BEFORE clicking to avoid a callback race.
        result_future, result_handler, result_new, result_edit = self._listen(
            peer, lambda message: bool(extract_store_link(message))
        )
        try:
            log.info(
                "Clicking File Store GENERATE LINK (prompt message %s)",
                prompt.id,
            )
            await prompt.click(
                i=generate_button["row"],
                j=generate_button["col"],
            )
            result_message = await result_future
        finally:
            self._unlisten(result_handler, result_new, result_edit)

        link = extract_store_link(result_message)
        if not link:
            raise RuntimeError(
                "File Store response arrived but contained no text or URL-button link"
            )

        log.info("File Store link successfully captured")
        return link

    async def process_video(self, video_message):
        renamed = await self.rename(video_message)
        link = await self.store_link(renamed)
        return renamed, link
