from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Awaitable, Callable

from app.core.config import settings

logger = logging.getLogger("mediafetch.mtproto")

ProgressCallback = Callable[[float, str], Awaitable[None] | None]


class LargeUploadError(RuntimeError):
    pass


def split_part_count(size_bytes: int, split_bytes: int) -> int:
    if size_bytes <= 0:
        return 1
    if split_bytes <= 0:
        raise ValueError("split_bytes must be positive")
    return math.ceil(size_bytes / split_bytes)


def split_part_name(path: Path, index: int, total_parts: int) -> str:
    """Return the requested filename.partNN.ext naming convention."""
    return f"{path.stem}.part{index:02d}{path.suffix}"


class MTProtoUploader:
    """Telegram user-session transport for files beyond Bot API upload limits.

    The session is kept in memory only. No API hash or session string is logged.
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
                    raise LargeUploadError(
                        "USER_SESSION_STRING must belong to a Telegram user account."
                    )
                self.ready = True
                logger.info(
                    "MTProto uploader ready: premium=%s",
                    bool(getattr(self.me, "is_premium", False)),
                )
                return True
            except Exception:
                self.ready = False
                self.client = None
                self.me = None
                logger.exception("MTProto uploader startup failed.")
                return False

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        client, self.client = self.client, None
        self.me = None
        self.ready = False
        if client is not None:
            try:
                await client.stop()
            except Exception:
                logger.warning("MTProto uploader shutdown encountered a stale transport.", exc_info=True)

    async def reconnect(self) -> bool:
        """Rebuild a stale Pyrogram transport without exposing session secrets."""
        if not self.configured:
            return False
        async with self._lock:
            await self._stop_unlocked()
            try:
                from pyrogram import Client

                client = Client(
                    "mediafetch_uploader",
                    api_id=settings.api_id,
                    api_hash=settings.api_hash,
                    session_string=settings.user_session_string,
                    in_memory=True,
                    no_updates=True,
                )
                self.client = client
                await client.start()
                me = await client.get_me()
                if not me or getattr(me, "is_bot", False):
                    await self._stop_unlocked()
                    logger.error("MTProto reconnect rejected: session is not a user account.")
                    return False
                self.me = me
                self.ready = True
                logger.info("MTProto uploader transport reconnected.")
                return True
            except Exception:
                await self._stop_unlocked()
                logger.warning("MTProto uploader reconnect failed.", exc_info=True)
                return False

    @property
    def account_is_premium(self) -> bool:
        return bool(self.me and getattr(self.me, "is_premium", False))

    @property
    def telegram_single_file_limit_mb(self) -> int:
        # Telegram's current MTProto upload part configuration is exposed as
        # 4000 parts for non-Premium and 8000 for Premium, with 512 KiB chunks:
        # roughly 2000 MiB and 4000 MiB respectively.
        return int(
            settings.mtproto_premium_max_mb
            if self.account_is_premium
            else settings.mtproto_nonpremium_max_mb
        )

    @property
    def application_split_limit_mb(self) -> int:
        configured = max(1, int(settings.large_upload_split_mb))
        telegram_ceiling = max(1, int(self.telegram_single_file_limit_mb))
        return min(configured, telegram_ceiling)

    def transport_available_for(self, size_bytes: int) -> bool:
        return bool(self.configured and self.ready and size_bytes > 50 * 1024 * 1024)

    async def _progress(
        self,
        callback: ProgressCallback | None,
        current: int,
        total: int,
        started: float,
    ) -> None:
        if not callback:
            return
        now = asyncio.get_running_loop().time()
        elapsed = max(now - started, 0.001)
        percent = current * 100 / total if total else 0.0
        speed = current / elapsed / (1024 * 1024)
        result = callback(
            percent,
            f"{current / (1024 * 1024):.1f}/{total / (1024 * 1024):.1f} MB • {speed:.1f} MB/s",
        )
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

    async def _upload_one_part(
        self,
        part_path: Path,
        target_chat_id: int,
        caption: str,
        progress_callback: ProgressCallback | None,
        total_size: int,
        completed_before: int,
        reply_to_message_id: int | None,
        dump_channel_id: str | int | None,
    ) -> object:
        started = asyncio.get_running_loop().time()
        part_size = part_path.stat().st_size

        async def upload_progress(current: int, _total: int, *args) -> None:
            global_current = min(total_size, completed_before + current)
            await self._progress(progress_callback, global_current, total_size, started)

        try:
            # Upload directly to the bridge channel when configured. The
            # previous implementation uploaded to Saved Messages first and
            # then tried to copy that message into the bridge. That requires
            # the MTProto session to resolve the bridge as a peer and was the
            # source of PEER_ID_INVALID on deployments where the user session
            # had not met that channel.
            destination = dump_channel_id or target_chat_id
            if isinstance(destination, str) and destination.strip().lstrip("-").isdigit():
                destination = int(destination.strip())
            # Populate Pyrogram's peer/access-hash cache before sending. A
            # freshly started in-memory session with no updates can otherwise
            # know the numeric channel ID but still reject the send with
            # PEER_ID_INVALID. This does not bypass Telegram membership rules:
            # the MTProto account must have access to the bridge channel.
            try:
                await self.client.get_chat(destination)
            except Exception as first_exc:
                # A closed TCPTransport can leave ready=True while the
                # underlying Pyrogram session is dead. Reconnect once, then
                # resolve the peer again before starting any upload (avoids
                # blindly retrying a send that might already have succeeded).
                logger.warning("MTProto peer lookup failed; rebuilding transport once: %s", first_exc)
                if not await self.reconnect():
                    raise LargeUploadError(
                        f"MTProto connection is unavailable and reconnect failed: {first_exc}"
                    ) from first_exc
                try:
                    await self.client.get_chat(destination)
                except Exception as second_exc:
                    raise LargeUploadError(
                        f"MTProto bridge peer is unavailable after reconnect: {second_exc}"
                    ) from second_exc
            # Preserve MP4 videos as real Telegram videos instead of
            # documents. This is important because the Bot API copy step
            # preserves the media type of the bridge message.
            if part_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
                saved = await self.client.send_video(
                    destination,
                    str(part_path),
                    caption=caption,
                    supports_streaming=True,
                    progress=upload_progress,
                )
            else:
                saved = await self.client.send_document(
                    destination,
                    str(part_path),
                    caption=caption,
                    force_document=True,
                    progress=upload_progress,
                )
            if not saved:
                raise LargeUploadError("Telegram cancelled the MTProto upload.")
            return saved
        except LargeUploadError:
            raise
        except Exception as exc:
            raise LargeUploadError(str(exc)) from exc

    def _create_part(self, source: Path, index: int, total_parts: int, offset: int, size: int) -> Path:
        part = source.with_name(split_part_name(source, index, total_parts))
        with source.open("rb") as src, part.open("wb") as dst:
            src.seek(offset)
            remaining = size
            while remaining:
                chunk = src.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    part.unlink(missing_ok=True)
                    raise LargeUploadError("Failed while creating a Telegram split part.")
                dst.write(chunk)
                remaining -= len(chunk)
        return part

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
            raise LargeUploadError(
                "Large-file Telegram session is not configured or connected."
            )

        total_size = path.stat().st_size
        split_bytes = self.application_split_limit_mb * 1024 * 1024
        total_parts = split_part_count(total_size, split_bytes)

        # MediaFetch deliberately splits anything above 2 GB into sequential
        # Telegram messages, even if a Premium session can technically upload
        # a larger single file. This keeps the requested 2 GB part contract.
        sent: list = []
        completed = 0
        offset = 0

        for index in range(1, total_parts + 1):
            part_size = min(split_bytes, total_size - offset)
            part_path = path
            temporary = total_parts > 1
            if temporary:
                part_path = self._create_part(
                    path, index, total_parts, offset, part_size
                )

            part_caption = caption
            if total_parts > 1:
                part_caption = (
                    f"{caption}\n\n"
                    f"📦 <b>Part {index}/{total_parts}</b> • "
                    f"{part_size / (1024 * 1024):.1f} MB"
                )

            try:
                delivered = await self._upload_one_part(
                    part_path=part_path,
                    target_chat_id=target_chat_id,
                    caption=part_caption,
                    progress_callback=progress_callback,
                    total_size=total_size,
                    completed_before=completed,
                    reply_to_message_id=reply_to_message_id if index == 1 else None,
                    dump_channel_id=dump_channel_id,
                )
                sent.append(delivered)
                completed += part_size
                offset += part_size
            finally:
                if temporary:
                    try:
                        part_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        if progress_callback:
            result = progress_callback(100.0, "Telegram upload complete")
            if asyncio.iscoroutine(result):
                await result
        return sent


mtproto_uploader = MTProtoUploader()
