# Mahiru Auto Anime Uploader — Integrated build

This project can run as a Render Web Service using a lightweight HTTP health endpoint. It uses:
- Telethon user session for authorized source-channel monitoring and permitted forwarding.
- python-telegram-bot for admin commands and destination publishing.
- MongoDB for episode state, quality links and posters.

## What it automates
1. Monitors configured source channels for announcement posts with a URL button containing WATCH in its label.
2. Opens supported Telegram channel/post links and locates quality buttons.
3. Supports direct `t.me/<bot>?start=<payload>` quality links and channel-post links that contain such a bot-start button.
4. Waits for an authorized retrieval bot to return a video.
5. Forwards the video to the rename bot and waits for its returned video.
6. Sends `/genlink` to the File Store bot, forwards the renamed video, clicks its GENERATE LINK button, and extracts the generated link.
7. Stores each quality URL in MongoDB and publishes a combined episode post with up to three buttons per row.
8. Supports poster management and admin status/pause/resume commands.

## Important limitations / safe operation
- The source navigation is implemented for Telegram URL buttons (`t.me` channel/post links and bot deep links). Arbitrary websites, custom web apps, callback-only quality buttons, and undocumented bot flows need a site-specific adapter.
- It does not circumvent protected-content restrictions, forced-join requirements, bot membership checks, approval requirements, or other access controls. Use only media and channels your account is authorized to access and redistribute.
- `JOIN_CHANNELS_AUTOMATICALLY` is intentionally false by default. Do not turn it on unless you have permission to join the linked public/private channels. The initial integrated build does not auto-solve forced-join gates.
- Third-party bot messages/buttons can change. Test each integration with one authorized test episode before enabling the worker on live sources.
- Multi-quality grouping uses title/season/episode/audio as its episode key. If source captions omit season/episode, distinct episodes can collide; normalize the parser for the actual channel captions.
- The first quality post is processed sequentially; if one quality fails, the episode remains partial and can be inspected with `/pending`.
- Telegram user sessions are highly sensitive. Never commit `.session` files, session strings, API hashes, bot tokens or MongoDB credentials to GitHub.

## Render deployment
Create a **Web Service** (not a Background Worker):
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`

Build command: `pip install -r requirements.txt`

Start command: `python bot.py`

The app starts a small HTTP server on `0.0.0.0:$PORT` and answers Render health checks at `/health`; Telegram polling and the automation pipeline continue in the same process. Set environment variables using `.env.example`. `USER_SESSION` must be a Telethon StringSession created on your own device. Do not put a raw `.session` file path in this variable.

## Admin commands
- `/start`
- `/status`
- `/pause`
- `/resume`
- `/pending`
- `/setposter` — send a photo with caption `/setposter Anime Name`

The poster key must match the parsed anime title (case-insensitive).

## Session generation
Run this locally in a trusted environment after installing Telethon:
```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = 123456
api_hash = "YOUR_API_HASH"
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(client.session.save())
```
Keep the printed string secret and add it to Render as `USER_SESSION`.
