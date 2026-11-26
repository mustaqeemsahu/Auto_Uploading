from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone

class MongoStore:
    def __init__(self, uri, db_name):
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
        self.db = self.client[db_name]

    async def connect(self):
        await self.client.admin.command("ping")
        await self.db.episodes.create_index("episode_key", unique=True)
        await self.db.jobs.create_index([("status", 1), ("created_at", 1)])
        await self.db.posters.create_index("anime_key", unique=True)

    async def close(self):
        self.client.close()

    async def get_episode(self, key):
        return await self.db.episodes.find_one({"episode_key": key})

    async def create_episode(self, key, data):
        now = datetime.now(timezone.utc)
        await self.db.episodes.update_one(
            {"episode_key": key},
            {"$setOnInsert": {
                "episode_key": key, "qualities": {}, "status": "queued",
                "created_at": now, **data
            }},
            upsert=True
        )
        return await self.get_episode(key)

    async def update_quality(self, key, quality, link, renamed_caption=""):
        await self.db.episodes.update_one(
            {"episode_key": key},
            {"$set": {
                f"qualities.{quality}": {"link": link, "caption": renamed_caption},
                "updated_at": datetime.now(timezone.utc)
            }}
        )

    async def set_status(self, key, status, error=None):
        values = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if error:
            values["last_error"] = str(error)[:1000]
        await self.db.episodes.update_one({"episode_key": key}, {"$set": values})

    async def list_pending(self, limit=20):
        cursor = self.db.episodes.find(
            {"status": {"$in": ["queued", "processing", "partial", "error"]}}
        ).sort("created_at", 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def set_poster(self, anime_key, file_id):
        await self.db.posters.update_one(
            {"anime_key": anime_key},
            {"$set": {"file_id": file_id}},
            upsert=True
        )

    async def get_poster(self, anime_key):
        doc = await self.db.posters.find_one({"anime_key": anime_key})
        return doc.get("file_id") if doc else None


    async def set_default_poster(self, file_id):
        await self.db.settings.update_one(
            {"key": "default_poster"},
            {"$set": {"file_id": file_id}},
            upsert=True,
        )

    async def get_default_poster(self):
        doc = await self.db.settings.find_one({"key": "default_poster"})
        return doc.get("file_id") if doc else None
