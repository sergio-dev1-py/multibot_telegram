"""One independent Telegram connection (PTB Application) per configured bot.

The module is imported lazily by the adapter so the plugin can be discovered and
validated even before the optional ``python-telegram-bot`` dependency is present.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

if __package__:
    from .config import redact_error, redact_text, resolve_token, token_shape_ok
else:  # standalone import (tests, ad-hoc tools)
    from config import redact_error, redact_text, resolve_token, token_shape_ok

logger = logging.getLogger(__name__)


class _TokenRedactingFilter(logging.Filter):
    """Defense in depth for PTB/httpx logs that embed ``bot<id>:<secret>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(
                    redact_text(value) if isinstance(value, str) else value
                    for value in args
                )
            elif isinstance(args, dict):
                record.args = {
                    key: redact_text(value) if isinstance(value, str) else value
                    for key, value in args.items()
                }
        except Exception:
            return True
        return True


def _install_log_redaction() -> None:
    for name in ("telegram", "telegram.ext", "telegram.request", "httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _TokenRedactingFilter) for f in target.filters):
            target.addFilter(_TokenRedactingFilter())
    # A logger filter does not run for records propagated from child loggers;
    # handler-level filters do, so cover the configured root handlers too.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _TokenRedactingFilter) for f in handler.filters):
            handler.addFilter(_TokenRedactingFilter())


_install_log_redaction()


def _html_to_plain(value: str) -> str:
    """Readable plain-text fallback for a rejected Telegram HTML payload."""
    return html_lib.unescape(re.sub(r"<[^>]*>", "", value or ""))


@dataclass
class RuntimeState:
    state: str = "idle"
    username: Optional[str] = None
    bot_user_id: Optional[int] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    updated_at: float = field(default_factory=time.time)


class BotRuntime:
    """A supervised PTB connection for one bot token."""

    def __init__(self, adapter: Any, bot_config: Any):
        self.adapter = adapter
        self.config = bot_config
        self.bot_id = bot_config.id
        self.token = ""
        self.app: Any = None
        self.bot: Any = None
        self.state = RuntimeState()
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._lock_held = False
        self._ever_started = False
        self._backoff = 2.0
        self._last_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"multi-telegram-{self.bot_id}")

    async def wait_ready(self, timeout: float) -> str:
        """Wait until the first connection attempt settles; return the state."""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            pass
        return self.state.state

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        await self._teardown()

    @property
    def connected(self) -> bool:
        return self.state.state == "connected" and self.app is not None

    @property
    def running(self) -> bool:
        return bool(self._task is not None and not self._task.done() and not self._stop_event.is_set())

    async def _supervise(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._start_once()
                self._backoff = 2.0
                await self._wait_until_failure()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self._teardown()
                code = self._classify_fatal(exc)
                if code in {"invalid_token", "unauthorized", "conflict", "invalid_token_format", "locked", "missing_token"}:
                    await self._set_state(
                        "failed",
                        code,
                        redact_error(exc, self.token),
                    )
                    logger.error(
                        "[multi-telegram:%s] permanent connection failure (%s); "
                        "this bot is stopped, other bots are unaffected",
                        self.bot_id,
                        code,
                    )
                    return
                delay = min(60.0, self._backoff) + random.uniform(0, 0.75)
                await self._set_state(
                    "retrying",
                    "network" if self._is_network(exc) else "connection",
                    redact_error(exc, self.token),
                )
                logger.warning(
                    "[multi-telegram:%s] connection failed: %s; retrying in %.1fs",
                    self.bot_id,
                    redact_error(exc, self.token),
                    delay,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                self._backoff = min(60.0, self._backoff * 2)
        await self._teardown()

    async def _start_once(self) -> None:
        self.token = resolve_token(self.config)
        if not self.token:
            raise RuntimeError(f"{self.config.token_env} is not set")
        if not token_shape_ok(self.token):
            raise RuntimeError("invalid token shape")
        _install_log_redaction()
        await self._set_state("connecting")
        acquired, existing = self._acquire_lock()
        if not acquired:
            raise RuntimeError("another Hermes process/profile is using this Telegram token")
        self._lock_held = True

        from telegram import Bot, BotCommand  # type: ignore
        from telegram.ext import (  # type: ignore
            ApplicationBuilder,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )
        from telegram.request import HTTPXRequest  # type: ignore

        request = HTTPXRequest(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            write_timeout=self.config.request_timeout,
            pool_timeout=self.config.request_timeout,
            proxy=self.config.proxy,
        )
        updates_request = HTTPXRequest(
            connect_timeout=self.config.connect_timeout,
            read_timeout=max(self.config.read_timeout, 30.0),
            write_timeout=self.config.request_timeout,
            pool_timeout=self.config.request_timeout,
            proxy=self.config.proxy,
        )
        builder = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .request(request)
            .get_updates_request(updates_request)
        )
        if self.config.api_base_url:
            set_base_url = getattr(builder, "base_url", None)
            if callable(set_base_url):
                builder = set_base_url(self.config.api_base_url)
            else:
                logger.warning(
                    "[multi-telegram:%s] this PTB version has no ApplicationBuilder.base_url; "
                    "api_base_url is ignored",
                    self.bot_id,
                )
        if self.config.file_base_url:
            set_file_base_url = getattr(builder, "base_file_url", None)
            if callable(set_file_base_url):
                builder = set_file_base_url(self.config.file_base_url)
            else:
                logger.warning(
                    "[multi-telegram:%s] this PTB version has no ApplicationBuilder.base_file_url; "
                    "file_base_url is ignored",
                    self.bot_id,
                )
        app = builder.build()
        bot = app.bot
        self.app, self.bot = app, bot

        await app.initialize()
        me = await bot.get_me()
        self.state.username = getattr(me, "username", None)
        self.state.bot_user_id = getattr(me, "id", None)
        # Only the first process-wide connect drops stale updates.  On a
        # reconnect after a network outage, keep Telegram's queued updates so
        # outage-time messages are not discarded.
        drop_pending = bool(self.config.drop_pending_updates and not self._ever_started)
        # Polling cannot coexist with a webhook on the same token.
        try:
            await bot.delete_webhook(drop_pending_updates=drop_pending)
        except Exception as exc:
            logger.debug("[multi-telegram:%s] deleteWebhook failed: %s", self.bot_id, redact_error(exc, self.token))

        app.add_handler(MessageHandler(filters.ALL, self._on_message))
        # This release sends no inline keyboards.  Answer any stray callback so
        # a Telegram client's spinner does not hang, and state the fallback.
        if hasattr(self.adapter, "_handle_callback"):
            app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_error_handler(self._on_error)

        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=drop_pending,
            allowed_updates=["message", "callback_query"],
        )
        self._ever_started = True
        await self._set_state("connected")
        try:
            await bot.set_my_commands(
                [
                    BotCommand("start", "Start this bot"),
                    BotCommand("help", "Show help"),
                ]
            )
        except Exception as exc:
            logger.debug(
                "[multi-telegram:%s] setMyCommands failed: %s",
                self.bot_id,
                redact_error(exc, self.token),
            )
        logger.info(
            "[multi-telegram:%s] connected as @%s (%s)",
            self.bot_id,
            self.state.username or "unknown",
            self.config.display_name,
        )

    async def _wait_until_failure(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass
            updater = getattr(self.app, "updater", None) if self.app is not None else None
            if updater is None or not getattr(updater, "running", False):
                raise RuntimeError("Telegram polling stopped unexpectedly")

    async def _teardown(self) -> None:
        app, self.app = self.app, None
        self.bot = None
        if app is not None:
            updater = getattr(app, "updater", None)
            try:
                if updater is not None and getattr(updater, "running", False):
                    await asyncio.wait_for(updater.stop(), timeout=8)
            except Exception:
                pass
            try:
                await asyncio.wait_for(app.stop(), timeout=8)
            except Exception:
                pass
            try:
                await asyncio.wait_for(app.shutdown(), timeout=8)
            except Exception:
                pass
        if self._lock_held:
            self._release_lock()
            self._lock_held = False

    async def _set_state(self, state: str, error_code: Optional[str] = None, error: Optional[str] = None) -> None:
        self.state.state = state
        self.state.error_code = error_code
        self.state.error = error
        self.state.updated_at = time.time()
        if state in {"connected", "failed", "retrying"}:
            self._ready_event.set()
        try:
            await self.adapter._runtime_state_changed(self)
        except Exception:
            logger.debug("[multi-telegram:%s] status state update failed", self.bot_id, exc_info=True)

    # -- PTB callbacks -----------------------------------------------------
    async def _on_message(self, update: Any, context: Any) -> None:
        try:
            await self.adapter._handle_update(self, update, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[multi-telegram:%s] inbound handler error: %s",
                self.bot_id,
                redact_error(exc, self.token),
            )

    async def _on_callback(self, update: Any, context: Any) -> None:
        try:
            await self.adapter._handle_callback(self, update, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[multi-telegram:%s] callback handler error: %s",
                self.bot_id,
                redact_error(exc, self.token),
            )

    async def _on_error(self, update: Any, context: Any) -> None:
        error = getattr(context, "error", None)
        if error is not None:
            logger.warning(
                "[multi-telegram:%s] PTB update error: %s",
                self.bot_id,
                redact_error(error, self.token),
            )

    # -- sends -------------------------------------------------------------
    async def send_text(
        self,
        real_chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        html: bool = True,
        disable_preview: bool = True,
    ) -> Any:
        if self.bot is None:
            raise RuntimeError("bot is not connected")
        from telegram.constants import ParseMode  # type: ignore
        from telegram.error import BadRequest  # type: ignore

        kwargs: dict[str, Any] = {
            "chat_id": int(real_chat_id),
            "text": content,
        }
        if reply_to:
            kwargs["reply_to_message_id"] = int(reply_to)
        if thread_id:
            kwargs["message_thread_id"] = int(thread_id)
        if disable_preview:
            try:
                from telegram import LinkPreviewOptions  # type: ignore

                kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
            except Exception:
                kwargs["disable_web_page_preview"] = True
        if html:
            kwargs["parse_mode"] = ParseMode.HTML
            try:
                return await self.bot.send_message(**kwargs)
            except BadRequest as exc:
                text = str(exc).lower()
                if "entit" not in text and "parse" not in text:
                    raise
                kwargs.pop("parse_mode", None)
                kwargs["text"] = _html_to_plain(str(kwargs.get("text") or ""))
                return await self.bot.send_message(**kwargs)
        return await self.bot.send_message(**kwargs)

    async def send_typing(self, real_chat_id: str, thread_id: Optional[str] = None) -> None:
        if self.bot is None:
            return
        from telegram.constants import ChatAction  # type: ignore

        kwargs: dict[str, Any] = {"chat_id": int(real_chat_id), "action": ChatAction.TYPING}
        if thread_id:
            kwargs["message_thread_id"] = int(thread_id)
        await self.bot.send_chat_action(**kwargs)

    async def edit_text(
        self,
        real_chat_id: str,
        telegram_message_id: str,
        content: str,
        *,
        html: bool = True,
        reply_markup: Any = None,
    ) -> Any:
        if self.bot is None:
            raise RuntimeError("bot is not connected")
        from telegram.constants import ParseMode  # type: ignore
        from telegram.error import BadRequest  # type: ignore

        kwargs: dict[str, Any] = {
            "chat_id": int(real_chat_id),
            "message_id": int(telegram_message_id),
            "text": content,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if html:
            kwargs["parse_mode"] = ParseMode.HTML
            try:
                return await self.bot.edit_message_text(**kwargs)
            except BadRequest as exc:
                text = str(exc).lower()
                if "not modified" in text:
                    return None
                if "entit" not in text and "parse" not in text:
                    raise
                kwargs.pop("parse_mode", None)
                kwargs["text"] = _html_to_plain(str(kwargs.get("text") or ""))
                return await self.bot.edit_message_text(**kwargs)
        return await self.bot.edit_message_text(**kwargs)

    async def delete(self, real_chat_id: str, telegram_message_id: str) -> bool:
        if self.bot is None:
            return False
        try:
            await self.bot.delete_message(chat_id=int(real_chat_id), message_id=int(telegram_message_id))
            return True
        except Exception:
            return False

    async def send_media(
        self,
        kind: str,
        real_chat_id: str,
        *,
        file: Any,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        html: bool = True,
        reply_markup: Any = None,
        file_name: Optional[str] = None,
    ) -> Any:
        if self.bot is None:
            raise RuntimeError("bot is not connected")
        from telegram.constants import ParseMode  # type: ignore
        from telegram.error import BadRequest  # type: ignore

        method_name = {
            "photo": "send_photo",
            "document": "send_document",
            "audio": "send_audio",
            "voice": "send_voice",
            "video": "send_video",
            "animation": "send_animation",
        }.get(kind)
        if method_name is None:
            raise ValueError(f"unsupported media kind: {kind}")
        method = getattr(self.bot, method_name)
        kwargs: dict[str, Any] = {"chat_id": int(real_chat_id), kind: file}
        if caption:
            kwargs["caption"] = caption[:1024]
        if reply_to:
            kwargs["reply_to_message_id"] = int(reply_to)
        if thread_id:
            kwargs["message_thread_id"] = int(thread_id)
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if file_name and kind == "document":
            # Only meaningful for byte/file-object uploads; PTB derives the name
            # from a path or URL, so passing it there is harmless but useless.
            kwargs["filename"] = file_name
        if html and caption:
            kwargs["parse_mode"] = ParseMode.HTML
            try:
                return await method(**kwargs)
            except BadRequest as exc:
                if "entit" not in str(exc).lower() and "parse" not in str(exc).lower():
                    raise
                kwargs.pop("parse_mode", None)
                if "caption" in kwargs:
                    kwargs["caption"] = _html_to_plain(str(kwargs["caption"] or ""))
                return await method(**kwargs)
        return await method(**kwargs)

    # -- helpers -----------------------------------------------------------
    def _acquire_lock(self) -> tuple[bool, Any]:
        try:
            from gateway.status import acquire_scoped_lock

            return acquire_scoped_lock("telegram_multi", self.token)
        except Exception as exc:
            logger.warning("[multi-telegram:%s] scoped token lock unavailable: %s", self.bot_id, redact_error(exc, self.token))
            # Without the lock, still let the bot run; a 409 conflict is caught at polling.
            return True, None

    def _release_lock(self) -> None:
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("telegram_multi", self.token)
        except Exception:
            logger.debug("[multi-telegram:%s] scoped token lock release failed", self.bot_id)

    @staticmethod
    def _is_network(exc: BaseException) -> bool:
        try:
            from telegram.error import NetworkError, TimedOut  # type: ignore

            return isinstance(exc, (NetworkError, TimedOut))
        except Exception:
            return False

    @staticmethod
    def _classify_fatal(exc: BaseException) -> Optional[str]:
        try:
            from telegram.error import Conflict, Forbidden, InvalidToken  # type: ignore
        except Exception:
            return None
        try:
            if isinstance(exc, InvalidToken):
                return "invalid_token"
            if isinstance(exc, Forbidden):
                return "unauthorized"
            if isinstance(exc, Conflict):
                return "conflict"
        except Exception:
            return None
        text = str(exc).lower()
        if "invalid token shape" in text:
            return "invalid_token_format"
        if "invalid token" in text or ("not found" in text and "token" in text):
            return "invalid_token"
        if "conflict" in text or "terminated by other getupdates" in text:
            return "conflict"
        if "another hermes process" in text:
            return "locked"
        if "is not set" in text or "token_env" in text:
            return "missing_token"
        return None
