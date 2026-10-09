from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("mediafetch.storage")

try:
    from pymongo import MongoClient, ReturnDocument
except Exception:  # pragma: no cover
    MongoClient = None


class Storage:
    """Small persistence layer with MongoDB when configured and memory fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._usage: dict[tuple[int, str], int] = {}
        self._premium: dict[int, float] = {}
        self._plans: dict[int, dict[str, Any]] = {}
        self._users: set[int] = set()
        self._user_panels: dict[int, dict[str, int]] = {}
        self._stats = {"downloads": 0, "cache_hits": 0, "failures": 0, "bytes": 0}
        self._payment_settings: dict[str, Any] = {}
        self._history: dict[int, list[dict[str, Any]]] = {}
        self._file_limits = {"free": 100, "bronze": 500, "platinum": 1024, "diamond": 2048, "admin": 0}
        self._client = None
        self._db = None
        try:
            from app.core.config import settings
            if not settings.mongodb_uri:
                logger.warning(
                    "MongoDB persistence disabled: MONGODB_URI is empty; "
                    "runtime settings will reset on restart."
                )
            elif MongoClient is None:
                logger.warning(
                    "MongoDB persistence unavailable: pymongo is not installed."
                )
            else:
                self._client = MongoClient(
                    settings.mongodb_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=5000,
                )
                # Force an actual connection check. MongoClient is lazy, so
                # creating the client alone is not enough to know whether
                # persistence is really available.
                self._client.admin.command("ping")
                self._db = self._client[settings.mongodb_db]
                self._db.settings.create_index("key", unique=True)
                self._db.cache.create_index("key", unique=True)
                self._db.users.create_index("user_id", unique=True)
                self._db.usage.create_index([("user_id", 1), ("day", 1)], unique=True)
                self._db.events.create_index("created_at")
                self._db.history.create_index([("user_id", 1), ("created_at", -1)])
                self._db.payments.create_index("payment_id", unique=True)
                self._db.payments.create_index("utr", unique=True)
                self._db.payments.create_index([("status", 1), ("created_at", -1)])
                logger.info(
                    "MongoDB persistence connected db=%s; channel/runtime settings "
                    "will survive restarts.",
                    settings.mongodb_db,
                )
        except Exception as exc:
            logger.warning(
                "MongoDB persistence connection failed; using memory fallback "
                "and runtime settings will reset on restart. error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
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

    def set_user_panel(self, user_id: int, chat_id: int, message_id: int) -> None:
        """Persist the user's single navigation-panel location across restarts."""
        panel = {"user_id": int(user_id), "panel_chat_id": int(chat_id), "panel_message_id": int(message_id)}
        if self._db is not None:
            self._db.users.update_one({"user_id": int(user_id)}, {"$set": panel}, upsert=True)
            return
        with self._lock:
            self._users.add(int(user_id))
            self._user_panels[int(user_id)] = {"chat_id": int(chat_id), "message_id": int(message_id)}

    def get_user_panel(self, user_id: int, chat_id: int | None = None) -> dict[str, int] | None:
        """Return a saved panel, optionally only when it belongs to this chat."""
        if self._db is not None:
            doc = self._db.users.find_one({"user_id": int(user_id)}, {"panel_chat_id": 1, "panel_message_id": 1}) or {}
            saved_chat = int(doc.get("panel_chat_id", 0) or 0)
            saved_message = int(doc.get("panel_message_id", 0) or 0)
        else:
            with self._lock:
                doc = self._user_panels.get(int(user_id), {})
                saved_chat = int(doc.get("chat_id", 0) or 0)
                saved_message = int(doc.get("message_id", 0) or 0)
        if not saved_chat or not saved_message or (chat_id is not None and saved_chat != int(chat_id)):
            return None
        return {"chat_id": saved_chat, "message_id": saved_message}

    def clear_user_panel(self, user_id: int) -> None:
        if self._db is not None:
            self._db.users.update_one(
                {"user_id": int(user_id)},
                {"$unset": {"panel_chat_id": "", "panel_message_id": ""}},
            )
            return
        with self._lock:
            self._user_panels.pop(int(user_id), None)

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

    def set_plan(self, user_id: int, plan: str, days: int) -> float:
        plan = str(plan).lower().strip()
        if plan not in {"free", "bronze", "platinum", "diamond"}:
            raise ValueError("plan must be free, bronze, platinum or diamond")
        expires = 0.0 if plan == "free" or days <= 0 else time.time() + days * 86400
        if self._db is not None:
            self._db.users.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "plan": plan,
                    "plan_until": expires,
                    "premium_until": expires if plan != "free" else 0.0,
                }},
                upsert=True,
            )
        else:
            with self._lock:
                if plan == "free" or expires <= 0:
                    self._plans.pop(user_id, None)
                    self._premium.pop(user_id, None)
                else:
                    self._plans[user_id] = {"plan": plan, "until": expires}
                    self._premium[user_id] = expires
        return expires

    def plan_info(self, user_id: int) -> dict[str, Any]:
        now = time.time()
        if self._db is not None:
            doc = self._db.users.find_one({"user_id": user_id}) or {}
            plan = str(doc.get("plan") or "").lower().strip()
            until = float(doc.get("plan_until", doc.get("premium_until", 0)) or 0)
            # Backward compatibility: old premium records become Bronze until
            # their existing premium_until timestamp expires.
            if not plan and float(doc.get("premium_until", 0) or 0) > now:
                plan = "bronze"
        else:
            with self._lock:
                item = self._plans.get(user_id, {})
                plan = str(item.get("plan") or "").lower().strip()
                until = float(item.get("until", 0) or 0)
                if not plan and self._premium.get(user_id, 0) > now:
                    plan = "bronze"
                    until = self._premium[user_id]
        if plan not in {"bronze", "platinum", "diamond"} or until <= now:
            return {"plan": "free", "until": 0.0, "active": False}
        return {"plan": plan, "until": until, "active": True}

    def set_premium(self, user_id: int, days: int) -> float:
        return self.set_plan(user_id, "bronze", days)

    def premium_until(self, user_id: int) -> float:
        return float(self.plan_info(user_id).get("until", 0) or 0)

    def is_premium(self, user_id: int) -> bool:
        return bool(self.plan_info(user_id).get("active"))

    def daily_limit(self, user_id: int, is_admin: bool = False) -> int:
        """Return the effective daily download limit; 0 means unlimited."""
        if is_admin:
            return 0
        from app.core.config import settings
        plan = str(self.plan_info(user_id).get("plan") or "free").lower()
        values = {
            "free": int(settings.free_daily_limit),
            "bronze": int(settings.bronze_daily_limit),
            "platinum": int(settings.platinum_daily_limit),
            "diamond": int(settings.diamond_daily_limit),
        }
        return max(0, values.get(plan, values["free"]))


    def payment_settings(self) -> dict[str, Any]:
        from app.core.config import settings
        defaults = {
            "upi_id": str(settings.payment_upi_id or ""),
            "currency": str(settings.payment_currency or "INR"),
            "bronze_price": int(settings.bronze_price),
            "platinum_price": int(settings.platinum_price),
            "diamond_price": int(settings.diamond_price),
            "bronze_duration_days": max(1, int(getattr(settings, "bronze_duration_days", settings.payment_duration_days))),
            "platinum_duration_days": max(1, int(getattr(settings, "platinum_duration_days", settings.payment_duration_days))),
            "diamond_duration_days": max(1, int(getattr(settings, "diamond_duration_days", settings.payment_duration_days))),
            "duration_days": max(1, int(settings.payment_duration_days)),
            "qr_url": str(settings.payment_qr_url or ""),
        }
        if self._db is not None:
            doc = self._db.settings.find_one({"key": "payment_config"}) or {}
            for key in defaults:
                if key in doc:
                    defaults[key] = doc[key]
        else:
            with self._lock:
                defaults.update({k: v for k, v in self._payment_settings.items() if k in defaults})
        return defaults

    def set_payment_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = {
            "upi_id": str(values.get("upi_id", "")).strip(),
            "currency": str(values.get("currency", "INR")).strip().upper() or "INR",
            "bronze_price": max(0, int(values.get("bronze_price", 0))),
            "platinum_price": max(0, int(values.get("platinum_price", 0))),
            "diamond_price": max(0, int(values.get("diamond_price", 0))),
            "bronze_duration_days": max(1, min(int(values.get("bronze_duration_days", values.get("duration_days", 7))), 3650)),
            "platinum_duration_days": max(1, min(int(values.get("platinum_duration_days", values.get("duration_days", 30))), 3650)),
            "diamond_duration_days": max(1, min(int(values.get("diamond_duration_days", values.get("duration_days", 30))), 3650)),
            "duration_days": max(1, min(int(values.get("duration_days", 30)), 3650)),
            "qr_url": str(values.get("qr_url", "")).strip(),
        }
        if self._db is not None:
            self._db.settings.update_one({"key": "payment_config"}, {"$set": {"key": "payment_config", **clean}}, upsert=True)
        else:
            with self._lock:
                self._payment_settings = dict(clean)
        return clean

    def create_payment(self, payment_id: str, user_id: int, plan: str, amount: int, currency: str, utr: str, duration_days: int = 30, provider: str = "manual", gateway_order_id: str = "", payment_session_id: str = "", screenshot_file_id: str = "", user_name: str = "", username: str = "", status_chat_id: int | str = 0, status_message_id: int | str = 0) -> dict[str, Any]:
        doc = {
            "payment_id": payment_id, "user_id": int(user_id), "plan": plan,
            "amount": int(amount), "currency": currency, "utr": utr,
            "status": "pending", "duration_days": max(1, int(duration_days)), "created_at": datetime.now(timezone.utc),
            "verified_at": None, "verified_by": None, "subscription_until": None,
            "provider": str(provider or "manual"), "gateway_order_id": str(gateway_order_id or ""),
            "payment_session_id": str(payment_session_id or ""),
            "screenshot_file_id": str(screenshot_file_id or ""),
            "user_name": str(user_name or user_id)[:120],
            "username": str(username or "").lstrip("@")[:64],
            "status_chat_id": int(status_chat_id) if str(status_chat_id).lstrip("-").isdigit() else 0,
            "status_message_id": int(status_message_id) if str(status_message_id).isdigit() else 0,
        }
        if self._db is not None:
            if self._db.payments.find_one({"utr": utr}):
                raise ValueError("UTR already submitted")
            self._db.payments.insert_one(doc)
            return {k: v for k, v in doc.items()}
        with self._lock:
            self._payments = getattr(self, "_payments", {})
            if any(str(item.get("utr", "")).lower() == utr.lower() for item in self._payments.values()):
                raise ValueError("UTR already submitted")
            self._payments[payment_id] = doc
            return dict(doc)

    def payment_by_id(self, payment_id: str) -> dict[str, Any] | None:
        if self._db is not None:
            return self._db.payments.find_one({"payment_id": payment_id}, {"_id": 0})
        with self._lock:
            item = getattr(self, "_payments", {}).get(payment_id)
            return dict(item) if item else None

    def payment_by_gateway_order(self, order_id: str) -> dict[str, Any] | None:
        if self._db is not None:
            return self._db.payments.find_one({"gateway_order_id": order_id, "provider": "cashfree"}, {"_id": 0})
        with self._lock:
            for item in getattr(self, "_payments", {}).values():
                if item.get("provider") == "cashfree" and item.get("gateway_order_id") == order_id:
                    return dict(item)
        return None

    def pending_payments(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        if self._db is not None:
            return list(self._db.payments.find({"status": "pending", "provider": {"$ne": "cashfree"}}, {"_id": 0}).sort("created_at", -1).limit(limit))
        with self._lock:
            items = [dict(v) for v in getattr(self, "_payments", {}).values() if v.get("status") == "pending" and v.get("provider") != "cashfree"]
            return sorted(items, key=lambda x: str(x.get("created_at", "")), reverse=True)[:limit]

    def payment_stats(self) -> dict[str, int]:
        if self._db is not None:
            return {
                "pending": int(self._db.payments.count_documents({"status": "pending", "provider": {"$ne": "cashfree"}})),
                "approved": int(self._db.payments.count_documents({"status": "approved"})),
                "rejected": int(self._db.payments.count_documents({"status": "rejected"})),
            }
        with self._lock:
            items = list(getattr(self, "_payments", {}).values())
            return {
                "pending": sum(1 for x in items if x.get("status") == "pending" and x.get("provider") != "cashfree"),
                "approved": sum(1 for x in items if x.get("status") == "approved"),
                "rejected": sum(1 for x in items if x.get("status") == "rejected"),
            }

    def approve_payment(self, payment_id: str, verified_by: int, days: int) -> dict[str, Any]:
        days = max(1, int(days))
        now = datetime.now(timezone.utc)
        if self._db is not None:
            doc = self._db.payments.find_one_and_update(
                {"payment_id": payment_id, "status": "pending"},
                {"$set": {"status": "approved", "verified_at": now, "verified_by": int(verified_by)}},
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                raise ValueError("Payment is no longer pending")
            effective_days = max(1, int(doc.get("duration_days", days)))
            expires = self.set_plan(int(doc["user_id"]), str(doc["plan"]), effective_days)
            self._db.payments.update_one({"payment_id": payment_id}, {"$set": {"subscription_until": expires}})
            doc["subscription_until"] = expires
            return doc
        with self._lock:
            doc = getattr(self, "_payments", {}).get(payment_id)
            if not doc or doc.get("status") != "pending":
                raise ValueError("Payment is no longer pending")
            doc["status"] = "approved"; doc["verified_at"] = now; doc["verified_by"] = int(verified_by)
            effective_days = max(1, int(doc.get("duration_days", days)))
            expires = self.set_plan(int(doc["user_id"]), str(doc["plan"]), effective_days)
            doc["subscription_until"] = expires
            return dict(doc)

    def reject_payment(self, payment_id: str, verified_by: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._db is not None:
            doc = self._db.payments.find_one_and_update(
                {"payment_id": payment_id, "status": "pending"},
                {"$set": {"status": "rejected", "verified_at": now, "verified_by": int(verified_by)}},
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                raise ValueError("Payment is no longer pending")
            return doc
        with self._lock:
            doc = getattr(self, "_payments", {}).get(payment_id)
            if not doc or doc.get("status") != "pending":
                raise ValueError("Payment is no longer pending")
            doc["status"] = "rejected"; doc["verified_at"] = now; doc["verified_by"] = int(verified_by)
            return dict(doc)

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

    def record_history(
        self,
        user_id: int,
        platform: str,
        url: str,
        title: str,
        mode: str,
        success: bool,
        size_bytes: int = 0,
    ) -> None:
        doc = {
            "user_id": user_id,
            "platform": platform,
            "url": url[:2000],
            "title": title[:300],
            "mode": mode[:40],
            "success": bool(success),
            "size_bytes": int(max(size_bytes, 0)),
            "created_at": datetime.now(timezone.utc),
        }
        if self._db is not None:
            self._db.history.insert_one(doc)
            stale = list(self._db.history.find({"user_id": user_id}, {"_id": 1}).sort("created_at", -1).skip(50))
            if stale:
                self._db.history.delete_many({"_id": {"$in": [item["_id"] for item in stale]}})
            return
        with self._lock:
            items = self._history.setdefault(user_id, [])
            items.insert(0, doc)
            del items[50:]

    def history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        if self._db is not None:
            return list(self._db.history.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1).limit(limit))
        with self._lock:
            return [dict(item) for item in self._history.get(user_id, [])[:limit]]

    def platform_stats(self) -> list[dict[str, Any]]:
        if self._db is not None:
            pipeline = [
                {"$group": {"_id": "$platform", "downloads": {"$sum": 1}, "successes": {"$sum": {"$cond": ["$success", 1, 0]}}, "bytes": {"$sum": "$size_bytes"}}},
                {"$sort": {"downloads": -1}},
            ]
            return [{"platform": item.get("_id", "Unknown"), "downloads": int(item.get("downloads", 0)), "successes": int(item.get("successes", 0)), "bytes": int(item.get("bytes", 0))} for item in self._db.events.aggregate(pipeline)]
        with self._lock:
            result: dict[str, dict[str, int]] = {}
            for items in self._history.values():
                for item in items:
                    platform = item.get("platform", "Unknown")
                    row = result.setdefault(platform, {"downloads": 0, "successes": 0, "bytes": 0})
                    row["downloads"] += 1
                    row["successes"] += int(bool(item.get("success")))
                    row["bytes"] += int(item.get("size_bytes", 0) or 0)
            return [{"platform": p, **v} for p, v in sorted(result.items(), key=lambda pair: pair[1]["downloads"], reverse=True)]

    def file_limits(self) -> dict[str, int]:
        from app.core.config import settings
        defaults = {
            "free": max(1, int(settings.free_max_file_mb)),
            "bronze": max(1, int(settings.bronze_max_file_mb)),
            "platinum": max(1, int(settings.platinum_max_file_mb)),
            "diamond": max(1, int(settings.diamond_max_file_mb)),
            "admin": max(0, int(settings.admin_max_file_mb)),
        }
        if self._db is not None:
            doc = self._db.settings.find_one({"key": "file_limits"}) or {}
            for key in defaults:
                try:
                    defaults[key] = max(0 if key == "admin" else 1, int(doc.get(key, defaults[key])))
                except (TypeError, ValueError):
                    pass
        else:
            with self._lock:
                for key in defaults:
                    if key in self._file_limits:
                        defaults[key] = max(0 if key == "admin" else 1, int(self._file_limits[key]))
        defaults["premium"] = defaults["bronze"]
        return defaults

    def set_file_limit(self, role: str, mb: int) -> dict[str, int]:
        role = role.lower().strip()
        if role == "premium":
            role = "bronze"
        if role not in {"free", "bronze", "platinum", "diamond", "admin"}:
            raise ValueError("role must be free, bronze, platinum, diamond or admin")
        mb = max(0 if role == "admin" else 1, min(int(mb), 100000))
        if self._db is not None:
            self._db.settings.update_one(
                {"key": "file_limits"},
                {"$set": {"key": "file_limits", role: mb}},
                upsert=True,
            )
            return self.file_limits()
        with self._lock:
            self._file_limits[role] = mb
            return dict(self._file_limits)

    def channel_config(self) -> dict[str, str]:
        from app.core.config import settings
        defaults = {"dump": str(settings.dump_channel_id or ""), "links": str(settings.links_log_channel_id or "")}
        if self._db is not None:
            doc = self._db.settings.find_one({"key": "channel_config"}) or {}
            return {key: str(doc.get(key, "") or "") for key in defaults}
        with self._lock:
            return dict(getattr(self, "_channel_config", defaults))

    def set_channel_config(self, kind: str, chat_id: str | int | None) -> dict[str, str]:
        kind = kind.lower().strip()
        if kind not in {"dump", "links"}:
            raise ValueError("channel kind must be dump or links")
        value = str(chat_id or "").strip()
        if self._db is not None:
            self._db.settings.update_one(
                {"key": "channel_config"},
                {"$set": {"key": "channel_config", kind: value}},
                upsert=True,
            )
            return self.channel_config()
        with self._lock:
            current = dict(getattr(self, "_channel_config", {"dump": "", "links": ""}))
            current[kind] = value
            self._channel_config = current
            return dict(current)

    def concurrent_download_limit(self) -> int:
        from app.core.config import settings
        default = max(1, min(int(settings.max_concurrent_downloads), 20))
        if self._db is not None:
            doc = self._db.settings.find_one({"key": "runtime_limits"}) or {}
            try:
                return max(1, min(int(doc.get("downloads", default)), 20))
            except (TypeError, ValueError):
                return default
        with self._lock:
            return max(1, min(int(getattr(self, "_concurrent_download_limit", default)), 20))

    def set_concurrent_download_limit(self, limit: int) -> int:
        limit = max(1, min(int(limit), 20))
        if self._db is not None:
            self._db.settings.update_one(
                {"key": "runtime_limits"},
                {"$set": {"key": "runtime_limits", "downloads": limit}},
                upsert=True,
            )
            return self.concurrent_download_limit()
        with self._lock:
            self._concurrent_download_limit = limit
            return limit

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
