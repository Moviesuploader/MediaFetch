from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None


class Storage:
    """Small persistence layer with MongoDB when configured and memory fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._usage: dict[tuple[int, str], int] = {}
        self._premium: dict[int, float] = {}
        self._users: set[int] = set()
        self._stats = {"downloads": 0, "cache_hits": 0, "failures": 0, "bytes": 0}
        self._client = None
        self._db = None
        try:
            from app.core.config import settings
            if settings.mongodb_uri and MongoClient is not None:
                self._client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=2500)
                self._db = self._client[settings.mongodb_db]
                self._db.cache.create_index("key", unique=True)
                self._db.users.create_index("user_id", unique=True)
                self._db.usage.create_index([("user_id", 1), ("day", 1)], unique=True)
                self._db.events.create_index("created_at")
        except Exception:
            self._client = None
            self._db = None

    @property
    def persistent(self) -> bool:
        return self._db is not None

    def get_cache(self, key: str, ttl_seconds: int) -> dict[str, Any] | None:
        now = time.time()
        if self._db is not None:
            doc = self._db.cache.find_one({"key": key})
            if not doc:
                return None
            if now - float(doc.get("created_at", 0)) > ttl_seconds:
                self._db.cache.delete_one({"_id": doc["_id"]})
                return None
            return {
                "file_ids": list(doc.get("file_ids") or []),
                "metadata": dict(doc.get("metadata") or {}),
            }
        with self._lock:
            item = self._cache.get(key)
            if not item or now - item["created_at"] > ttl_seconds:
                self._cache.pop(key, None)
                return None
            return {"file_ids": list(item["file_ids"]), "metadata": dict(item.get("metadata") or {})}

    def delete_cache(self, key: str) -> None:
        if self._db is not None:
            self._db.cache.delete_one({"key": key})
            return
        with self._lock:
            self._cache.pop(key, None)

    def set_cache(self, key: str, file_ids: list[str], metadata: dict[str, Any]) -> None:
        doc = {"key": key, "file_ids": file_ids, "metadata": metadata, "created_at": time.time()}
        if self._db is not None:
            self._db.cache.update_one({"key": key}, {"$set": doc}, upsert=True)
            return
        with self._lock:
            self._cache[key] = doc

    def touch_user(self, user_id: int, username: str | None = None) -> None:
        doc = {"user_id": user_id, "username": username, "updated_at": datetime.now(timezone.utc)}
        if self._db is not None:
            self._db.users.update_one({"user_id": user_id}, {"$set": doc}, upsert=True)
        else:
            with self._lock:
                self._users.add(user_id)

    def user_ids(self) -> list[int]:
        if self._db is not None:
            return [int(doc["user_id"]) for doc in self._db.users.find({}, {"user_id": 1}) if doc.get("user_id")]
        with self._lock:
            return sorted(self._users | {uid for uid, _ in self._usage} | set(self._premium))

    def increment_usage(self, user_id: int) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._db is not None:
            self._db.usage.update_one(
                {"user_id": user_id, "day": day},
                {"$inc": {"count": 1}},
                upsert=True,
            )
            doc = self._db.usage.find_one({"user_id": user_id, "day": day})
            return int(doc.get("count", 1)) if doc else 1
        with self._lock:
            key = (user_id, day)
            self._usage[key] = self._usage.get(key, 0) + 1
            return self._usage[key]

    def usage_today(self, user_id: int) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._db is not None:
            doc = self._db.usage.find_one({"user_id": user_id, "day": day})
            return int(doc.get("count", 0)) if doc else 0
        with self._lock:
            return self._usage.get((user_id, day), 0)

    def set_premium(self, user_id: int, days: int) -> float:
        expires = 0.0 if days <= 0 else time.time() + days * 86400
        if self._db is not None:
            self._db.users.update_one(
                {"user_id": user_id},
                {"$set": {"user_id": user_id, "premium_until": expires}},
                upsert=True,
            )
        else:
            with self._lock:
                if expires:
                    self._premium[user_id] = expires
                else:
                    self._premium.pop(user_id, None)
        return expires

    def premium_until(self, user_id: int) -> float:
        if self._db is not None:
            doc = self._db.users.find_one({"user_id": user_id})
            return float(doc.get("premium_until", 0)) if doc else 0.0
        with self._lock:
            return self._premium.get(user_id, 0.0)

    def is_premium(self, user_id: int) -> bool:
        return self.premium_until(user_id) > time.time()

    def record_event(
        self,
        user_id: int,
        platform: str,
        success: bool,
        size_bytes: int = 0,
        cache_hit: bool = False,
    ) -> None:
        event = {
            "user_id": user_id,
            "platform": platform,
            "success": success,
            "size_bytes": size_bytes,
            "cache_hit": cache_hit,
            "created_at": datetime.now(timezone.utc),
        }
        if self._db is not None:
            self._db.events.insert_one(event)
        with self._lock:
            self._stats["downloads"] += 1
            self._stats["bytes"] += max(size_bytes, 0)
            if cache_hit:
                self._stats["cache_hits"] += 1
            if not success:
                self._stats["failures"] += 1

    def set_maintenance(self, enabled: bool) -> None:
        if self._db is not None:
            self._db.settings.update_one({"key": "maintenance"}, {"$set": {"key": "maintenance", "enabled": enabled}}, upsert=True)
        else:
            with self._lock:
                self._maintenance = enabled

    def maintenance(self) -> bool:
        if self._db is not None:
            doc = self._db.settings.find_one({"key": "maintenance"})
            return bool(doc and doc.get("enabled"))
        with self._lock:
            return bool(getattr(self, "_maintenance", False))

    def stats(self) -> dict[str, int]:
        if self._db is not None:
            downloads = self._db.events.count_documents({})
            failures = self._db.events.count_documents({"success": False})
            cache_hits = self._db.events.count_documents({"cache_hit": True})
            pipeline = [{"$group": {"_id": None, "bytes": {"$sum": "$size_bytes"}}}]
            total = next(self._db.events.aggregate(pipeline), {"bytes": 0})
            return {
                "downloads": int(downloads),
                "failures": int(failures),
                "cache_hits": int(cache_hits),
                "bytes": int(total.get("bytes", 0)),
            }
        with self._lock:
            return dict(self._stats)


storage = Storage()
