import os

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    API_ID = int(os.getenv("API_ID", "0"))
    API_HASH = os.getenv("API_HASH", "")
    USER_SESSION = os.getenv("USER_SESSION", "")
    MONGO_URI = os.getenv("MONGO_URI", "")
    MONGO_DB = os.getenv("MONGO_DB", "mahiru_auto_uploader")
    SOURCE_CHANNELS = [x.strip() for x in os.getenv("SOURCE_CHANNELS", "").split(",") if x.strip()]
    DESTINATION_CHANNEL = os.getenv("DESTINATION_CHANNEL", "")
    ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
    RENAME_BOT = os.getenv("RENAME_BOT", "@AdvFileRenameBot")
    FILE_STORE_BOT = os.getenv("FILE_STORE_BOT", "@Naruto_AnimeRobot")
    MAIN_CHANNEL_TEXT = os.getenv("MAIN_CHANNEL_TEXT", "")
    MAX_QUALITY_JOBS = int(os.getenv("MAX_QUALITY_JOBS", "8"))
    BOT_TIMEOUT = int(os.getenv("BOT_TIMEOUT", "240"))
    JOIN_CHANNELS_AUTOMATICALLY = os.getenv("JOIN_CHANNELS_AUTOMATICALLY", "false").lower() == "true"
    ALLOWED_JOIN_DOMAINS = {"t.me", "telegram.me"}

    @classmethod
    def validate(cls):
        missing = []
        for key in ("BOT_TOKEN", "API_HASH", "MONGO_URI", "DESTINATION_CHANNEL"):
            if not getattr(cls, key):
                missing.append(key)
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.USER_SESSION:
            missing.append("USER_SESSION")
        if not cls.SOURCE_CHANNELS:
            missing.append("SOURCE_CHANNELS")
        if not cls.ADMIN_IDS:
            missing.append("ADMIN_IDS")
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
