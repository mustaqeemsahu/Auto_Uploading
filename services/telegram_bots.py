import asyncio, logging, re
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from services.parser import get_buttons, parse_tme_url

log = logging.getLogger(__name__)

class BotAutomation:
    def __init__(self, client, rename_bot, store_bot, timeout=240):
        self.client = client
        self.rename_bot = rename_bot
        self.store_bot = store_bot
        self.timeout = timeout

    async def wait_message(self, peer, after_id, predicate, timeout=None):
        end = asyncio.get_running_loop().time() + (timeout or self.timeout)
        while asyncio.get_running_loop().time() < end:
            messages = await self.client.get_messages(peer, limit=30)
            for message in sorted(messages, key=lambda x: x.id):
                if message.id > after_id and not message.out and predicate(message):
                    return message
            await asyncio.sleep(1.5)
        raise TimeoutError(f"Timed out waiting for response from {peer}")

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

    async def clear_and_start_bot(self, bot, payload=None):
        peer = await self.client.get_entity(bot)
        before = await self.client.get_messages(peer, limit=1)
        before_id = before[0].id if before else 0
        command = "/start" + (f" {payload}" if payload else "")
        await self.client.send_message(peer, command)
        return peer, before_id

    async def process_retrieval_bot(self, bot_username, payload=None):
        peer, before_id = await self.clear_and_start_bot(bot_username, payload)
        while True:
            msg = await self.wait_message(
                peer, before_id,
                lambda m: bool(m.video or (m.document and (m.document.mime_type or "").startswith("video/")))
            , timeout=self.timeout)
            return msg

    async def resolve_quality_link(self, button):
        # URL buttons can point directly to a retrieval bot or channel post.
        if button.get("url"):
            return await self.process_quality_url(button["url"])

        # Callback-only quality buttons must be clicked on their original message.
        source_message = button.get("_message")
        if source_message is not None:
            before = await self.client.get_messages(source_message.peer_id, limit=1)
            before_id = before[0].id if before else source_message.id
            await source_message.click(i=button["row"], j=button["col"])
            return await self.wait_message(
                source_message.peer_id,
                before_id,
                lambda m: bool(
                    m.video or
                    (m.document and (m.document.mime_type or "").startswith("video/"))
                ),
                timeout=self.timeout,
            )
        raise RuntimeError("Quality button has neither a URL nor an attached source message")

    async def process_quality_url(self, url):
        parsed = parse_tme_url(url)
        if not parsed:
            raise RuntimeError(f"Unsupported quality URL: {url}")
        if parsed["kind"] == "bot_start":
            return await self.process_retrieval_bot("@" + parsed["bot"], parsed["payload"])
        if parsed["kind"] in ("public_post", "private_post"):
            channel = parsed["channel"]
            post_id = parsed["message_id"]
            entity = await self.client.get_entity(channel)
            msg = await self.client.get_messages(entity, ids=post_id)
            if not msg:
                raise RuntimeError("Quality link target post was not found")
            for b in get_buttons(msg):
                p = parse_tme_url(b.get("url"))
                if p and p["kind"] == "bot_start":
                    return await self.process_retrieval_bot("@" + p["bot"], p["payload"])
                if b.get("has_callback"):
                    b["_message"] = msg
                    return await self.resolve_quality_link(b)
            raise RuntimeError("Quality post has no supported bot start or callback button")
        raise RuntimeError(f"Unsupported quality target: {parsed['kind']}")

    async def retrieve_quality(self, quality_button):
        return await self.resolve_quality_link(quality_button)

    async def rename(self, source_video):
        peer = await self.client.get_entity(self.rename_bot)
        before = await self.client.get_messages(peer, limit=1)
        last_id = before[0].id if before else 0
        await self.client.forward_messages(peer, source_video)
        return await self.wait_message(
            peer, last_id,
            lambda m: bool(m.video or (m.document and (m.document.mime_type or "").startswith("video/")))
        )

    async def store_link(self, renamed_video):
        peer = await self.client.get_entity(self.store_bot)
        before = await self.client.get_messages(peer, limit=1)
        last_id = before[0].id if before else 0

        await self.client.send_message(peer, "/genlink")
        await asyncio.sleep(1)
        await self.client.forward_messages(peer, renamed_video)

        # Some store bots edit their existing prompt; others send a new message.
        prompt = None
        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            recent = await self.client.get_messages(peer, limit=40)
            for msg in recent:
                text = (msg.raw_text or "").upper()
                buttons = get_buttons(msg)
                has_generate = any("GENERATE LINK" in b["label"].upper() for b in buttons)
                has_stored_caption = "STORED MESSAGES" in text
                if not msg.out and has_generate and (msg.id > last_id or has_stored_caption):
                    prompt = msg
                    break
            if prompt:
                break
            await asyncio.sleep(1)
        if not prompt:
            raise TimeoutError("File Store GENERATE LINK prompt was not found")

        target = next(
            (b for b in get_buttons(prompt) if "GENERATE LINK" in b["label"].upper()),
            None
        )
        if not target:
            raise RuntimeError("GENERATE LINK button is missing")

        log.info("Clicking File Store GENERATE LINK button (message %s)", prompt.id)
        await prompt.click(i=target["row"], j=target["col"])

        # The result may arrive as a new message OR by editing the prompt itself.
        deadline = asyncio.get_running_loop().time() + self.timeout
        url_re = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\\s<>]+", re.I)
        while asyncio.get_running_loop().time() < deadline:
            recent = await self.client.get_messages(peer, limit=40)
            candidates = [prompt] + [m for m in recent if m.id >= prompt.id]
            for msg in candidates:
                text = msg.raw_text or ""
                match = url_re.search(text)
                if match:
                    link = match.group(0).rstrip(").,]")
                    log.info("File Store link generated: %s", link)
                    return link
            await asyncio.sleep(1)
        raise TimeoutError("File Store did not return a generated link after button click")

    async def process_video(self, video_message):
        renamed = await self.rename(video_message)
        link = await self.store_link(renamed)
        return renamed, link
