"""Durable, non-secret plugin state: status and Telegram update receipts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

RECEIPT_TTL_SECONDS = 24 * 60 * 60
RECEIPT_CAP = 4096


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("multi-telegram")
    except Exception:
        pass
    candidates: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        candidates.append(get_hermes_home() / "plugin-data" / "multi-telegram")
    except Exception:
        pass
    candidates.append(Path.cwd() / ".multi-telegram-data")
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return Path.cwd()


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (_data_dir() / "status.json")
        self._lock = asyncio.Lock()
        self._ready = False
        self._state: dict[str, Any] = {
            "schema": 1,
            "pid": os.getpid(),
            "started_at": _utcnow(),
            "updated_at": _utcnow(),
            "bots": {},
            # shared synthetic chat_id -> bot id that most recently received
            # there.  Used ONLY as the final fallback for proactive sends that
            # have no event binding; never consulted for a bound turn.
            "routes": {},
        }

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def _persist(self) -> None:
        try:
            _atomic_write(self.path, self._state)
        except Exception as exc:
            logger.warning("[multi-telegram] could not write status file: %s", exc)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                return loaded
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not read multi-telegram status file: %s", exc)
        return {}

    async def start(self) -> None:
        async with self._lock:
            if self._ready:
                return
            self._ready = True
            loaded = await asyncio.to_thread(self.load)
            if isinstance(loaded, dict) and loaded:
                # Routes survive a restart (they make proactive delivery
                # deterministic).  Per-bot runtime states do not: they describe
                # the previous process, so start them clean.
                if isinstance(loaded.get("routes"), dict):
                    self._state["routes"] = {
                        str(k): str(v) for k, v in loaded["routes"].items()
                    }
            self._state["bots"] = {}
            self._state["state"] = "starting"
            self._state["pid"] = os.getpid()
            self._state["started_at"] = _utcnow()
            self._state["updated_at"] = _utcnow()
            await asyncio.to_thread(self._persist)

    def route(self, chat_id: Any) -> Optional[str]:
        """Per-shared-conversation receiving bot, if one was recorded."""
        routes = self._state.get("routes")
        if not isinstance(routes, dict):
            return None
        value = routes.get(str(chat_id or ""))
        return str(value) if value else None

    async def set_route(self, chat_id: str, bot_id: str) -> None:
        if not chat_id or not bot_id:
            return
        async with self._lock:
            routes = self._state.setdefault("routes", {})
            if not isinstance(routes, dict):
                routes = self._state["routes"] = {}
            if routes.get(str(chat_id)) == str(bot_id):
                return
            routes[str(chat_id)] = str(bot_id)
            # Bound the map: shared conversations are normally few, but never
            # let a long-lived gateway grow it without limit.
            if len(routes) > 2048:
                for key in list(routes)[: len(routes) - 2048]:
                    routes.pop(key, None)
            self._state["updated_at"] = _utcnow()
            await asyncio.to_thread(self._persist)

    async def set_bot(
        self,
        bot_id: str,
        *,
        state: str,
        username: Optional[str] = None,
        bot_user_id: Optional[int] = None,
        error_code: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            entry = dict(self._state.setdefault("bots", {}).get(bot_id) or {})
            entry.update(
                {
                    "state": str(state),
                    "updated_at": _utcnow(),
                    "username": username if username is not None else entry.get("username"),
                    "bot_user_id": bot_user_id if bot_user_id is not None else entry.get("bot_user_id"),
                    "error_code": error_code,
                    "error": error,
                }
            )
            self._state["bots"][bot_id] = entry
            self._state["updated_at"] = _utcnow()
            await asyncio.to_thread(self._persist)

    async def set_overall(self, state: str) -> None:
        async with self._lock:
            self._state["state"] = str(state)
            self._state["updated_at"] = _utcnow()
            await asyncio.to_thread(self._persist)

    async def flush(self) -> None:
        async with self._lock:
            self._state["updated_at"] = _utcnow()
            await asyncio.to_thread(self._persist)


class UpdateReceiptStore:
    """Per-bot update-id receipts so a restart cannot duplicate an agent turn.

    Telegram may redeliver a getUpdates batch whose acknowledgement never landed.
    PTB's in-memory offset does not survive a process restart; this bounded,
    persisted receipt set does.
    """

    def __init__(self, bot_id: str, path: Optional[Path] = None):
        self.bot_id = bot_id
        self.path = path or (_data_dir() / f"update_receipts_{bot_id}.json")
        self._seen: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            ids = payload.get("update_ids") if isinstance(payload, dict) else None
            if isinstance(ids, dict):
                cutoff = time.time() - RECEIPT_TTL_SECONDS
                self._seen = {
                    str(uid): float(ts)
                    for uid, ts in ids.items()
                    if isinstance(ts, (int, float)) and float(ts) > cutoff
                }
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("[multi-telegram:%s] unreadable update receipts: %s", self.bot_id, exc)

    def is_duplicate(self, update_id: Any) -> bool:
        self.load()
        key = str(update_id)
        cutoff = time.time() - RECEIPT_TTL_SECONDS
        # Opportunistic TTL trim.
        if len(self._seen) > RECEIPT_CAP // 2:
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if self._seen.get(key, 0) > cutoff:
            return True
        while len(self._seen) >= RECEIPT_CAP:
            oldest = min(self._seen, key=self._seen.get)
            self._seen.pop(oldest, None)
        self._seen[key] = time.time()
        self._dirty = True
        self._schedule_flush()
        return False

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = loop.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        try:
            await asyncio.sleep(1.0)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[multi-telegram:%s] receipt flush failed", self.bot_id, exc_info=True)

    async def flush(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            cutoff = time.time() - RECEIPT_TTL_SECONDS
            payload = {
                "schema": 1,
                "bot_id": self.bot_id,
                "update_ids": {k: v for k, v in self._seen.items() if v > cutoff},
            }
            await asyncio.to_thread(_atomic_write, self.path, payload)
            self._dirty = False
