from __future__ import annotations

import asyncio
import logging
import math
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from app.core.config import settings

logger = logging.getLogger("mediafetch.mtproto")

ProgressCallback = Callable[[float, str], Awaitable[None] | None]


class LargeUploadError(RuntimeError):
    pass


class MTProtoUploader:
    """Dedicated user-session transport for files above the normal Bot API limit.

    The session is never logged or persisted by this module. Pyrofork keeps the
    supplied session in memory, which is suitable for ephemeral Koyeb workers.
    """

    def __init__(self) -> None:
        self.client = None
        self.me = None
        self.ready = False
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(
            settings.mtproto_upload_enabled
            and settings.api_id
            and settings.api_hash
            and settings.user_session_string
        )

    async def start(self) -> bool:
        if not self.configured:
            return False
        async with self._lock:
            if self.ready and self.client is not None:
                return True
            try:
                from pyrogram import Client

                self.client = Client(
                    "mediafetch_uploader",
                    api_id=settings.api_id,
                    api_hash=settings.api_hash,
                    session_string=settings.user_session_string,
                    in_memory=True,
                    no_updates=True,
                )
                await self.client.start()
                self.me = await self.client.get_me()
                if not self.me or getattr(self.me, "is_bot", False):
                    await self.client.stop()
                    self.client = None
                    raise LargeUploadError("USER_SESSION_STRING must belong to a Telegram user account.")
                self.ready = True
                logger.info(
                    "MTProto uploader ready: premium=%s",
                    bool(getattr(self.me, "is_premium", False)),
                )
                return True
            except Exception:
                self.ready = False
                self.client = None
                logger.exception("MTProto uploader startup failed.")
                return False

    async def stop(self) -> None:
        async with self._lock:
            if self.client is not None:
                try:
                    await self.client.stop()
                except Exception:
                    logger.exception("MTProto uploader shutdown failed.")
            self.client = None
            self.me = None
            self.ready = False

    @property
    def account_is_premium(self) -> bool:
        return bool(self.me and getattr(self.me, "is_premium", False))

    @property
    def telegram_single_file_limit_mb(self) -> int:
        # Current Telegram MTProto configuration exposes 4000 MB for
        # Premium accounts and 2000 MB for non-Premium accounts.
        return int(
            settings.mtproto_premium_max_mb
            if self.account_is_premium
            else settings.mtproto_nonpremium_max_mb
        )

    def transport_available_for(self, size_bytes: int) -> bool:
        return self.configured and self.ready and size_bytes > 50 * 1024 * 1024

    async def _progress(self, callback: ProgressCallback | None, current: int, total: int, started: float) -> None:
        if not callback:
            return
        now = asyncio.get_running_loop().time()
        elapsed = max(now - started, 0.001)
        percent = current * 100 / total if total else 0.0
        speed = current / elapsed / (1024 * 1024)
        result = callback(percent, f"{current / (1024 * 1024):.1f}/{total / (1024 * 1024):.1f} MB • {speed:.1f} MB/s")
        if asyncio.iscoroutine(result):
            await result

    async def _copy_to_target(
        self,
        source_chat: int | str,
        message_id: int,
        target_chat: int | str,
        reply_to_message_id: int | None = None,
    ):
        return await self.client.copy_message(
            chat_id=target_chat,
            from_chat_id=source_chat,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_file(
        self,
        path: Path,
        target_chat_id: int,
        caption: str,
        progress_callback: ProgressCallback | None = None,
        reply_to_message_id: int | None = None,
        dump_channel_id: str | int | None = None,
    ) -> list:
        if not self.ready or self.client is None:
            raise LargeUploadError("Large-file Telegram session is not configured or connected.")

        size = path.stat().st_size
        limit_mb = self.telegram_single_file_limit_mb
        limit_bytes = limit_mb * 1024 * 1024
        if size <= limit_bytes:
            part_paths = [path]
        else:
            part_paths = []
            remaining = size
            index = 1
            # Keep the configured Telegram per-message ceiling. Each part is
            # independently uploaded and then copied to the destination.
            while remaining > 0:
                part_size = min(limit_bytes, remaining)
                part_path = path.with_name(
                    f"{path.name}.part{index:02d}-of-{math.ceil(size / limit_bytes):02d}"
                )
                with path.open("rb") as src, part_path.open("wb") as dst:
                    src.seek((index - 1) * limit_bytes)
                    remaining_bytes = part_size
                    while remaining_bytes:
                        chunk = src.read(min(8 * 1024 * 1024, remaining_bytes))
                        if not chunk:
                            raise LargeUploadError("Failed while creating a Telegram split part.")
                        dst.write(chunk)
                        remaining_bytes -= len(chunk)
                part_paths.append(part_path)
                remaining -= part_size
                index += 1

        sent = []
        total_parts = len(part_paths)
        for index, part_path in enumerate(part_paths, start=1):
            part_caption = caption
            if total_parts > 1:
                part_caption = (
                    f"{caption}\n\n"
                    f"📦 <b>Part {index}/{total_parts}</b> • "
                    f"{part_path.stat().st_size / (1024 * 1024):.1f} MB"
                )
            started = asyncio.get_running_loop().time()

            async def upload_progress(current: int, total: int, *args) -> None:
                await self._progress(progress_callback, current, total, started)

            try:
                saved = await self.client.send_document(
                    "me",
                    str(part_path),
                    caption=part_caption,
                    force_document=True,
                    progress=upload_progress,
                )
                if not saved:
                    raise LargeUploadError("Telegram cancelled the MTProto upload.")
                delivered = await self._copy_to_target(
                    "me",
                    saved.id,
                    target_chat_id,
                    reply_to_message_id=reply_to_message_id if index == 1 else None,
                )
                sent.append(delivered)

                if dump_channel_id:
                    try:
                        await self.client.copy_message(
                            chat_id=dump_channel_id,
                            from_chat_id="me",
                            message_id=saved.id,
                        )
                    except Exception:
                        logger.exception("MTProto dump-channel copy failed.")
            except Exception as exc:
                raise LargeUploadError(str(exc)) from exc
            finally:
                if part_path != path:
                    try:
                        part_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        if progress_callback:
            result = progress_callback(100.0, "Telegram upload complete")
            if asyncio.iscoroutine(result):
                await result
        return sent

    async def copy_cached(
        self,
        target_chat_id: int,
        source_chat_id: int | str,
        message_ids: list[int],
        reply_to_message_id: int | None = None,
    ) -> list:
        if not self.ready or self.client is None:
            raise LargeUploadError("MTProto uploader is not connected.")
        result = []
        for index, message_id in enumerate(message_ids):
            result.append(
                await self._copy_to_target(
                    source_chat_id,
                    int(message_id),
                    target_chat_id,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                )
            )
        return result


mtproto_uploader = MTProtoUploader()
