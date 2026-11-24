import re
from urllib.parse import urlparse, parse_qs

QUALITY_ORDER = ["2160P", "4K", "1080P", "HDRIP", "720P", "480P"]

def normalize_quality(value):
    v = (value or "").upper().replace(" ", "")
    if "2160" in v or "4K" in v:
        return "2160p [4K]"
    if "1080" in v:
        return "1080p"
    if "720" in v:
        return "720p"
    if "480" in v:
        return "480p"
    if "HDRIP" in v:
        return "HDRIP"
    return value or "Unknown"

def parse_episode(text):
    text = text or ""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    title = lines[0] if lines else "Unknown Anime"
    title = re.sub(r"(?i)^(➲\s*title\s*[-:]\s*)", "", title).strip()
    season = re.search(r"(?i)\b(?:S|Season)\s*0?(\d{1,2})\b", text)
    episode = re.search(r"(?i)\b(?:E|Ep|Episode)\s*0?(\d{1,3})\b", text)
    audio = "English Dub" if re.search(r"(?i)english\s*dub|\beng\s*dub\b", text) else "Japanese Audio [English Subtitles]"
    return {
        "title": title[:150],
        "season": int(season.group(1)) if season else None,
        "episode": int(episode.group(1)) if episode else None,
        "audio": audio,
    }

def episode_key(info):
    return "|".join([
        re.sub(r"\W+", "", info["title"].lower()),
        str(info.get("season") or 0),
        str(info.get("episode") or 0),
        info["audio"].lower()
    ])

def get_buttons(message):
    found = []
    if not getattr(message, "buttons", None):
        return found
    for row_i, row in enumerate(message.buttons):
        for col_i, button in enumerate(row):
            label = (getattr(button, "text", "") or "").strip()
            url = getattr(button, "url", None)
            if label:
                found.append({"label": label, "url": url, "row": row_i, "col": col_i})
    return found

def is_quality(label):
    return bool(re.search(r"(?i)(2160|4k|1080|720|480|hdrip)", label or ""))

def parse_tme_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
        return None
    path = [p for p in parsed.path.split("/") if p]
    if not path:
        return None
    if path[0] == "c" and len(path) >= 3:
        return {"kind": "private_post", "channel": int("-100" + path[1]), "message_id": int(path[2])}
    if path[0].startswith("+") or path[0] == "joinchat":
        return {"kind": "invite", "hash": path[0][1:] if path[0].startswith("+") else path[1]}
    if len(path) >= 2 and path[1].isdigit():
        return {"kind": "public_post", "channel": path[0], "message_id": int(path[1])}
    query = parse_qs(parsed.query)
    if "start" in query:
        return {"kind": "bot_start", "bot": path[0], "payload": query["start"][0]}
    return {"kind": "public_channel", "channel": path[0]}
