"""BasePlatformAdapter implementation for multiple Telegram bot identities."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from datetime import datetime
from typing import Any, Optional

if __package__:
    from .authz import BotAuthorizer, union_allows_dm
    from .config import (
        ConfigError,
        MultiTelegramConfig,
        parse_config,
        redact_error,
        redact_text,
    )
    from .formatting import (
        MAX_TELEGRAM_LENGTH,
        plain_fallback,
        render_html,
        split_text,
        truncate_utf16,
        utf16_len,
    )
    from .session_identity import (
        logical_chat_id,
        message_id_for,
        parse_chat_id,
        parse_message_id,
        real_chat_id,
        resolve_bot_id,
        source_bot_id,
    )
    from .status_store import StatusStore, UpdateReceiptStore
else:  # standalone import (tests, ad-hoc tools)
    from authz import BotAuthorizer, union_allows_dm
    from config import (
        ConfigError,
        MultiTelegramConfig,
        parse_config,
        redact_error,
        redact_text,
    )
    from formatting import (
        MAX_TELEGRAM_LENGTH,
        plain_fallback,
        render_html,
        split_text,
        truncate_utf16,
        utf16_len,
    )
    from session_identity import (
        logical_chat_id,
        message_id_for,
        parse_chat_id,
        parse_message_id,
        real_chat_id,
        resolve_bot_id,
        source_bot_id,
    )
    from status_store import StatusStore, UpdateReceiptStore

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, classify_send_error
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)

# Module-level so a replacement adapter instance in the same process can follow
# the turn's transport binding.  It is a per-task ContextVar, never a global
# mutable "last bot" variable.
_BOUND_BOT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "multi_telegram_bound_bot_id", default=None
)

_WELCOME_DEFAULT = "Hi! I'm {name}, one of your Hermes Telegram endpoints."
# Cross-bot control commands that must reach Hermes's active-session bypass
# path instead of waiting for the session guard to clear. Kept local so the
# plugin does not depend on a private gateway helper.
_CROSS_BOT_CONTROL_COMMANDS = frozenset(
    {
        "stop", "new", "reset", "approve", "deny", "interrupt", "cancel",
        "model", "reasoning", "voice", "insights", "title", "resume", "retry",
        "undo", "compress", "usage", "reload-mcp", "sethome",
    }
)


class TelegramMultiAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_TELEGRAM_LENGTH
    splits_long_messages = True
    supports_async_delivery = True
    interactive_resume = True
    allow_from_covers_groups = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("telegram_multi"))
        if getattr(config, "token", None) or getattr(config, "api_key", None):
            logger.warning(
                "[multi-telegram] platform-level token/api_key is ignored; "
                "configure one token_env per bot"
            )
        self._config_error: Optional[str] = None
        try:
            self._mt = parse_config(getattr(config, "extra", {}) or {})
        except ConfigError as exc:
            self._mt = MultiTelegramConfig()
            self._config_error = str(exc)
            logger.error("[multi-telegram] invalid configuration: %s", exc)
        self._runtimes: dict[str, Any] = {}
        self._authorizers: dict[str, BotAuthorizer] = {
            bot.id: BotAuthorizer(bot) for bot in self._mt.bots
        }
        self._status = StatusStore()
        self._receipts: dict[str, UpdateReceiptStore] = {
            bot.id: UpdateReceiptStore(bot.id) for bot in self._mt.bots
        }
        self._warned_unrouted: set[str] = set()
        self._dm_policy = self._mt.dm_policy
        self._group_policy = self._mt.group_policy
        self._publish_gateway_allowlists(config)
        self._connected_once = False
        # Shared sessions may receive events from different bots while Hermes
        # is busy.  The base adapter's pending slot is keyed by session only and
        # merges photo bursts; that would let one bot's turn answer another
        # bot's message.  Defer cross-bot events instead, then run them as
        # separate turns once the session guard clears.
        self._active_bot_by_session: dict[str, str] = {}
        self._cross_bot_deferred: dict[str, list[MessageEvent]] = {}
        self._cross_bot_tasks: dict[str, asyncio.Task] = {}

    def _publish_gateway_allowlists(self, config: PlatformConfig) -> None:
        """Derive the platform-wide allowlist Hermes's outer gate reads.

        Hermes 0.23 authorizes a native event before dispatch. For a plugin
        platform it reads ``extra.allowed_users`` only when the platform entry
        declares ``allowed_users_env``. The plugin's authoritative policy is
        per bot and is enforced in ``_handle_update``; this union only lets a
        sender reach that intake. A chat-only allowlist is published through
        ``group_allowed_chats`` so group traffic is not blocked before the
        adapter can check it.
        """
        extra = getattr(config, "extra", None)
        if not isinstance(extra, dict):
            return
        users: set[str] = set()
        chats: set[str] = set()
        existing = extra.get("allowed_users")
        if isinstance(existing, (list, tuple, set)):
            users.update(str(item).strip() for item in existing if str(item).strip())
        elif isinstance(existing, str):
            users.update(part.strip() for part in existing.split(",") if part.strip())
        existing_chats = extra.get("group_allowed_chats")
        if isinstance(existing_chats, (list, tuple, set)):
            chats.update(
                str(item).strip() for item in existing_chats if str(item).strip()
            )
        elif isinstance(existing_chats, str):
            chats.update(
                part.strip() for part in existing_chats.split(",") if part.strip()
            )
        for bot in self._mt.enabled_bots:
            if bot.allow_all_users:
                users.add("*")
            users.update(bot.allowed_users)
            chats.update(bot.allowed_chats)
        if users:
            extra["allowed_users"] = sorted(users)
        if chats:
            extra["group_allowed_chats"] = sorted(chats)

    # -- BasePlatformAdapter contract -------------------------------------
    @property
    def message_len_fn(self):
        return utf16_len

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    @property
    def send_path_degraded(self) -> bool:
        return bool(self._runtimes) and not any(rt.connected for rt in self._runtimes.values())

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._config_error:
            await self._status.set_overall("invalid_config")
            logger.error("[multi-telegram] not connecting: %s", self._config_error)
            return False
        enabled = self._mt.enabled_bots
        if not enabled:
            await self._status.set_overall("no_enabled_bots")
            logger.error("[multi-telegram] no enabled bots configured")
            return False

        await self._status.start()
        await self._status.set_overall("connecting")
        for bot in enabled:
            await self._status.set_bot(bot.id, state="starting")
        try:
            from .bot_runtime import BotRuntime
        except ImportError:
            from bot_runtime import BotRuntime

        for bot in enabled:
            if bot.id not in self._runtimes:
                self._runtimes[bot.id] = BotRuntime(self, bot)
        for bot in enabled:
            rt = self._runtimes[bot.id]
            if is_reconnect:
                # A reconnect is not a cold boot: preserve Telegram's queued
                # outage-time updates instead of dropping them.
                rt._ever_started = True
            await rt.start()

        # Wait for each first connection attempt.  A slow/failed bot must not hold
        # the whole platform startup past the gateway's connect timeout.
        try:
            wait_timeout = float(
                getattr(self.config, "extra", {}).get("connect_wait_seconds", 18) or 18
            )
        except (TypeError, ValueError):
            wait_timeout = 18.0
        wait_timeout = max(6.0, min(60.0, wait_timeout))
        await asyncio.gather(
            *(rt.wait_ready(wait_timeout) for rt in self._runtimes.values() if rt.running),
            return_exceptions=True,
        )
        connected = [rt for rt in self._runtimes.values() if rt.connected]
        if connected:
            self._connected_once = True
            self._mark_connected()
            await self._status.set_overall("connected")
            logger.info(
                "[multi-telegram] %d/%d bot(s) connected: %s",
                len(connected),
                len(self._runtimes),
                ", ".join(rt.bot_id for rt in connected),
            )
            return True

        await self._status.set_overall("disconnected")
        self._mark_disconnected()
        logger.error(
            "[multi-telegram] no bot connected at startup; check tokens and network. "
            "The gateway can be restarted after fixing the configuration."
        )
        return False

    async def disconnect(self) -> None:
        deferred_tasks = list(self._cross_bot_tasks.values())
        for task in deferred_tasks:
            task.cancel()
        if deferred_tasks:
            await asyncio.gather(*deferred_tasks, return_exceptions=True)
        self._cross_bot_tasks.clear()
        self._cross_bot_deferred.clear()
        self._active_bot_by_session.clear()
        runtimes = list(self._runtimes.values())
        await asyncio.gather(*(rt.stop() for rt in runtimes), return_exceptions=True)
        for receipt in self._receipts.values():
            try:
                await receipt.flush()
            except Exception:
                pass
        await self._status.set_overall("disconnected")
        try:
            await self._status.flush()
        except Exception:
            pass
        self._mark_disconnected()
        logger.info("[multi-telegram] disconnected %d bot connection(s)", len(runtimes))

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        bot_id = self._resolve_bot(chat_id, reply_to, metadata)
        if not bot_id:
            self._log_unrouted(chat_id, "text send")
            return SendResult(
                success=False,
                error="No originating bot is bound to this conversation; refusing to guess",
                error_kind="unknown",
            )
        parsed = parse_chat_id(chat_id)
        runtime = self._runtimes.get(bot_id)
        if runtime is None or not runtime.connected:
            return SendResult(
                success=False,
                error=f"Telegram bot '{bot_id}' is not connected",
                error_kind="transient",
                retryable=True,
            )
        return await self._send_text_via(runtime, parsed.real_chat_id, content, reply_to, metadata)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        bot_id = self._resolve_bot(chat_id, None, metadata)
        if not bot_id:
            return
        runtime = self._runtimes.get(bot_id)
        if runtime is None or not runtime.connected:
            return
        parsed = parse_chat_id(chat_id)
        thread_id = self._thread_id(metadata)
        try:
            await runtime.send_typing(parsed.real_chat_id, thread_id)
        except Exception as exc:
            logger.debug("[multi-telegram:%s] typing failed: %s", bot_id, redact_error(exc, runtime.token))

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        bot_id, raw_message_id = parse_message_id(message_id)
        if not bot_id:
            bot_id = self._resolve_bot(chat_id, None, metadata)
        if not bot_id:
            self._log_unrouted(chat_id, "edit")
            return SendResult(success=False, error="No bot bound to edited message", error_kind="unknown")
        runtime = self._runtimes.get(bot_id)
        if runtime is None or not runtime.connected:
            return SendResult(success=False, error=f"Telegram bot '{bot_id}' is not connected", retryable=True)
        parsed = parse_chat_id(chat_id)
        html = render_html(content)
        try:
            if utf16_len(html) > MAX_TELEGRAM_LENGTH:
                html = truncate_utf16(plain_fallback(content), MAX_TELEGRAM_LENGTH)
            await runtime.edit_text(parsed.real_chat_id, raw_message_id, html, html=True)
            return SendResult(success=True, message_id=message_id_for(bot_id, raw_message_id))
        except Exception as exc:
            return self._failure(exc, runtime.token)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        bot_id, raw_message_id = parse_message_id(message_id)
        if not bot_id:
            bot_id = self._resolve_bot(chat_id, None, None)
        runtime = self._runtimes.get(bot_id or "")
        if runtime is None:
            return False
        return await runtime.delete(real_chat_id(chat_id), raw_message_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        parsed = parse_chat_id(chat_id)
        return {
            "name": parsed.real_chat_id,
            "type": "group" if parsed.real_chat_id.startswith("-") else "dm",
            "chat_id": parsed.real_chat_id,
        }

    # -- optional media contract ------------------------------------------
    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media("photo", chat_id, image_url, caption, reply_to, metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media("photo", chat_id, image_path, caption, reply_to, metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media(
            "document", chat_id, file_path, caption, reply_to, metadata,
            file_name=file_name,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        result = await self._send_media(
            "voice", chat_id, audio_path, caption, reply_to, metadata
        )
        if not result.success:
            # Telegram only treats OGG/Opus as a native voice bubble; a normal
            # audio send still reaches the user when the voice path is rejected.
            fallback = await self._send_media(
                "audio", chat_id, audio_path, caption, reply_to, metadata
            )
            if fallback.success:
                return fallback
        return result

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_media("video", chat_id, video_path, caption, reply_to, metadata)

    async def send_animation(
        self,
        chat_id: str,
        animation_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(
            "animation", chat_id, animation_path, caption, reply_to, metadata
        )

    # -- final delivery binding -------------------------------------------
    async def send_final_ledgered(
        self,
        event: MessageEvent,
        session_key: str,
        text_content: str,
        metadata: dict[str, Any],
        *,
        reply_to: Optional[str],
        is_ephemeral_response: bool = False,
    ) -> tuple[SendResult, BasePlatformAdapter]:
        bot_id = source_bot_id(event.source)
        token = _BOUND_BOT_ID.set(bot_id) if bot_id else None
        try:
            return await super().send_final_ledgered(
                event,
                session_key,
                text_content,
                metadata,
                reply_to=reply_to,
                is_ephemeral_response=is_ephemeral_response,
            )
        finally:
            if token is not None:
                _BOUND_BOT_ID.reset(token)

    async def _deliver_attachments(
        self, event: MessageEvent, extracted: Any, metadata: dict[str, Any], *,
        anything_sent: bool, record_delivery: Any,
    ) -> None:
        bot_id = source_bot_id(event.source)
        token = _BOUND_BOT_ID.set(bot_id) if bot_id else None
        try:
            await super()._deliver_attachments(
                event, extracted, metadata, anything_sent=anything_sent,
                record_delivery=record_delivery,
            )
        finally:
            if token is not None:
                _BOUND_BOT_ID.reset(token)

    async def _notify_turn_error(self, event: MessageEvent, e: BaseException) -> Optional[dict]:
        bot_id = source_bot_id(event.source)
        token = _BOUND_BOT_ID.set(bot_id) if bot_id else None
        try:
            return await super()._notify_turn_error(event, e)
        finally:
            if token is not None:
                _BOUND_BOT_ID.reset(token)

    async def on_processing_start(self, event: MessageEvent) -> None:
        bot_id = source_bot_id(event.source)
        if bot_id:
            _BOUND_BOT_ID.set(bot_id)
        try:
            await super().on_processing_start(event)
        except Exception:
            logger.debug("[multi-telegram] on_processing_start hook failed", exc_info=True)

    def status_metadata_for_turn(self, chat_id: str, anchor: str, metadata: Optional[dict]) -> dict:
        bot_id, _ = parse_message_id(anchor)
        if not bot_id:
            bot_id = _BOUND_BOT_ID.get()
        merged = dict(metadata or {})
        if bot_id:
            merged["multi_telegram_bot_id"] = bot_id
        return merged

    def toolsets_for_source(self, source: Any) -> Optional[list[str]]:
        bot_id = source_bot_id(source)
        bot = self._mt.bot(bot_id) if bot_id else None
        if bot is None or not bot.toolsets:
            return None
        return list(bot.toolsets)

    def _is_dm_allowed(self, user_id: str) -> bool:
        return union_allows_dm(self._mt, user_id)

    # -- cross-bot busy-session isolation --------------------------------
    def _start_session_processing(
        self, event: MessageEvent, session_key: str, *, interrupt_event: Any = None
    ) -> bool:
        bot_id = source_bot_id(event.source)
        if bot_id:
            self._active_bot_by_session[session_key] = bot_id
        return super()._start_session_processing(
            event, session_key, interrupt_event=interrupt_event
        )

    async def _handle_message_while_active(
        self, event: MessageEvent, session_key: str
    ) -> None:
        bot_id = source_bot_id(event.source)
        active_bot = self._active_bot_by_session.get(session_key)
        command = event.get_command()
        bypass = bool(command and command in _CROSS_BOT_CONTROL_COMMANDS)
        if (
            bot_id
            and active_bot
            and bot_id != active_bot
            and not bypass
        ):
            queue = self._cross_bot_deferred.setdefault(session_key, [])
            queue.append(event)
            event._gateway_accepted = True
            task = self._cross_bot_tasks.get(session_key)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._drain_cross_bot(session_key),
                    name=f"multi-telegram-cross-bot-{session_key}",
                )
                self._cross_bot_tasks[session_key] = task
            return
        await super()._handle_message_while_active(event, session_key)

    async def _drain_cross_bot(self, session_key: str) -> None:
        try:
            while True:
                queue = self._cross_bot_deferred.get(session_key)
                if not queue:
                    return
                while session_key in self._active_sessions:
                    await asyncio.sleep(0.1)
                queue = self._cross_bot_deferred.get(session_key)
                if not queue:
                    return
                event = queue.pop(0)
                token = None
                bot_id = source_bot_id(event.source)
                if bot_id:
                    token = _BOUND_BOT_ID.set(bot_id)
                try:
                    await self.handle_message(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "[multi-telegram] deferred cross-bot turn failed: %s",
                        redact_error(exc),
                    )
                finally:
                    if token is not None:
                        _BOUND_BOT_ID.reset(token)
        finally:
            if not self._cross_bot_deferred.get(session_key):
                self._cross_bot_deferred.pop(session_key, None)
            self._cross_bot_tasks.pop(session_key, None)

    def _can_merge_text_debounce_events(self, existing: Any, event: Any) -> bool:
        """Do not merge rapid text from two different bots in one shared session."""
        existing_bot = source_bot_id(getattr(existing, "source", None))
        event_bot = source_bot_id(getattr(event, "source", None))
        if existing_bot and event_bot and existing_bot != event_bot:
            return False
        return super()._can_merge_text_debounce_events(existing, event)

    # -- inbound -----------------------------------------------------------
    async def _handle_update(self, runtime: Any, update: Any, context: Any) -> None:
        update_id = getattr(update, "update_id", None)
        if update_id is not None:
            receipts = self._receipts.get(runtime.bot_id)
            if receipts is not None and receipts.is_duplicate(update_id):
                logger.debug("[multi-telegram:%s] duplicate update %s dropped", runtime.bot_id, update_id)
                return
        message = getattr(update, "message", None)
        if message is None:
            if getattr(update, "edited_message", None) is not None:
                logger.debug("[multi-telegram:%s] edited message ignored", runtime.bot_id)
                return
            if getattr(update, "callback_query", None) is not None:
                await self._handle_callback(runtime, update, context)
                return
            return

        chat = getattr(message, "chat", None)
        from_user = getattr(message, "from_user", None)
        if chat is None or from_user is None:
            # Channel posts / anonymous channel senders are not a supported 1:1
            # or group conversation in v1.
            logger.debug("[multi-telegram:%s] message without user/chat ignored", runtime.bot_id)
            return

        chat_type = self._chat_type(chat)
        if chat_type == "channel":
            logger.debug("[multi-telegram:%s] channel post ignored", runtime.bot_id)
            return
        real_chat = str(chat.id)
        user_id = str(from_user.id)
        user_name = self._display_name(from_user)
        is_bot = bool(getattr(from_user, "is_bot", False))

        authorizer = self._authorizers.get(runtime.bot_id)
        decision = authorizer.authorize(
            chat_id=real_chat, chat_type=chat_type, user_id=user_id, is_bot=is_bot
        ) if authorizer else None
        if decision is None or not decision.allowed:
            logger.debug(
                "[multi-telegram:%s] rejected %s/%s: %s",
                runtime.bot_id,
                chat_type,
                real_chat,
                decision.reason if decision else "no authorizer",
            )
            return

        if chat_type != "dm" and not self._is_addressed(runtime, message):
            return

        bot = self._mt.bot(runtime.bot_id)
        if bot is None:
            return
        generic_command = self._command_name(message)
        command = self._command_name(message, runtime.state.username)
        if generic_command and not command:
            # Explicitly addressed to another bot; not ours to execute.
            return
        logical_chat = logical_chat_id(bot, real_chat, user_id)
        if bot.session_mode == "shared":
            # Persist the receiving-bot binding for THIS conversation.  It is
            # used only by proactive sends with no event/reply binding; every
            # reply to a live turn is bound explicitly below.
            try:
                await self._status.set_route(logical_chat, runtime.bot_id)
            except Exception:
                logger.debug(
                    "[multi-telegram:%s] could not persist shared route",
                    runtime.bot_id,
                    exc_info=True,
                )

        command = self._command_name(message, runtime.state.username)
        if command == "start":
            await self._send_welcome(
                runtime, real_chat, self._thread_id_from_message(message)
            )
            return

        media = await self._prepare_media(runtime, message)
        if media.blocked:
            if media.note:
                try:
                    await runtime.send_text(
                        real_chat,
                        render_html(media.note),
                        thread_id=self._thread_id_from_message(message),
                        html=True,
                    )
                except Exception:
                    pass
            return

        text = self._message_text(message)
        if not text and not media.paths:
            return
        if not text and media.note:
            text = media.note
        elif media.note and text:
            text = f"{text}\n\n{media.note}"

        thread_id = self._thread_id_from_message(message)
        chat_name = self._chat_name(chat, from_user, chat_type)
        source = self.build_source(
            chat_id=logical_chat,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_id,
            message_id=message_id_for(runtime.bot_id, message.message_id),
            scope_id=real_chat,
            is_bot=is_bot,
        )
        # Live-turn provenance; not persisted, but available to toolsets_for_source
        # and to diagnostics while this event is processed.
        source._mt_bot_id = runtime.bot_id
        source._mt_real_chat_id = real_chat

        reply = getattr(message, "reply_to_message", None)
        event = MessageEvent(
            text=text,
            message_type=self._message_type(message, text),
            user_id=user_id,
            user_name=user_name,
            source=source,
            raw_message=message,
            message_id=message_id_for(runtime.bot_id, message.message_id),
            reply_to_message_id=str(reply.message_id) if reply is not None else None,
            reply_to_text=self._message_text(reply)[:1000] if reply is not None else None,
            reply_to_author_id=str(reply.from_user.id) if reply is not None and reply.from_user else None,
            reply_to_author_name=self._display_name(reply.from_user) if reply is not None and reply.from_user else None,
            reply_to_is_own_message=bool(
                reply is not None
                and reply.from_user is not None
                and runtime.state.bot_user_id is not None
                and int(reply.from_user.id) == int(runtime.state.bot_user_id)
            ),
            media_urls=media.paths,
            media_types=media.types,
            media_filenames=media.filenames,
            platform_update_id=update_id,
            # Keep metadata empty. Hermes 0.23 native admission accepts only
            # the closed WhatsApp metadata contract and refuses any other key
            # with "invalid_params". Routing is already carried by the
            # synthetic chat id, the "<bot>:<message>" id prefix, the
            # turn-local ContextVar and the persisted per-conversation route.
            channel_prompt=self._channel_prompt(runtime),
            auto_skill=list(bot.skills) if bot.skills else None,
            reply_expected=True,
        )
        date = getattr(message, "date", None)
        if isinstance(date, datetime):
            event.timestamp = date
        token = _BOUND_BOT_ID.set(runtime.bot_id)
        try:
            await self.handle_message(event)
        finally:
            _BOUND_BOT_ID.reset(token)

    async def _handle_callback(self, runtime: Any, update: Any, context: Any) -> None:
        """Answer callback taps with a clear fallback.

        Native inline keyboards for clarify/approvals are intentionally not
        claimed in this release; Hermes's text fallbacks remain available.
        """
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        try:
            await query.answer(text="Interactive buttons are not enabled in this release; reply with text.")
        except Exception:
            logger.debug("[multi-telegram:%s] callback answer failed", runtime.bot_id)

    # -- outbound helpers --------------------------------------------------
    def _resolve_bot(self, chat_id: Any, reply_to: Any, metadata: Optional[dict]) -> Optional[str]:
        parsed = parse_chat_id(chat_id)
        shared_route: Optional[str] = None
        if parsed.shared_group:
            candidate = self._status.route(parsed.raw)
            candidate_bot = self._mt.bot(candidate) if candidate else None
            if (
                candidate_bot is not None
                and candidate_bot.enabled
                and candidate_bot.session_mode == "shared"
                and candidate_bot.shared_group == parsed.shared_group
            ):
                shared_route = candidate_bot.id
        bot_id = resolve_bot_id(
            chat_id=chat_id,
            reply_to=reply_to,
            metadata=metadata,
            context_bot_id=_BOUND_BOT_ID.get(),
            shared_route_bot_id=shared_route,
            known_bot_ids=set(self._mt.by_id),
        )
        if bot_id:
            return bot_id
        if not parsed.is_synthetic and self._mt.default_bot:
            # Raw Telegram ids are operator/cron targets with no conversation
            # scope from which a bot could be proven.  Use the explicitly
            # configured default rather than a hidden "last active" value.
            return self._mt.default_bot
        return None

    def _log_unrouted(self, chat_id: Any, operation: str) -> None:
        key = f"{operation}:{chat_id}"
        if key in self._warned_unrouted:
            return
        self._warned_unrouted.add(key)
        logger.warning(
            "[multi-telegram] %s for %s has no provable originating bot; "
            "refusing to guess (final replies are event-bound and unaffected)",
            operation,
            chat_id,
        )

    async def _send_text_via(
        self,
        runtime: Any,
        real_chat: str,
        content: str,
        reply_to: Optional[str],
        metadata: Optional[dict],
    ) -> SendResult:
        _, raw_reply = parse_message_id(reply_to)
        thread_id = self._thread_id(metadata)
        mode = str(getattr(self.config, "reply_to_mode", "first") or "first").lower()
        chunks = split_text(content, MAX_TELEGRAM_LENGTH)
        if not chunks:
            return SendResult(success=True)
        sent: list[str] = []
        for index, chunk in enumerate(chunks):
            html = render_html(chunk)
            use_html = True
            if utf16_len(html) > MAX_TELEGRAM_LENGTH:
                html = plain_fallback(chunk)
                use_html = False
            this_reply = None
            if raw_reply and mode != "off" and (mode == "all" or index == 0):
                this_reply = raw_reply
            try:
                message = await runtime.send_text(
                    real_chat,
                    html,
                    reply_to=this_reply,
                    thread_id=thread_id,
                    html=use_html,
                )
            except Exception as exc:
                return self._failure(exc, runtime.token, sent)
            raw_id = getattr(message, "message_id", None)
            if raw_id is not None:
                sent.append(message_id_for(runtime.bot_id, raw_id))
        return SendResult(
            success=True,
            message_id=sent[-1] if sent else None,
            continuation_message_ids=tuple(sent[:-1]),
        )

    async def _send_media(
        self,
        kind: str,
        chat_id: str,
        file: Any,
        caption: Optional[str],
        reply_to: Optional[str],
        metadata: Optional[dict],
        *,
        file_name: Optional[str] = None,
    ) -> SendResult:
        bot_id = self._resolve_bot(chat_id, reply_to, metadata)
        if not bot_id:
            self._log_unrouted(chat_id, f"{kind} send")
            return SendResult(success=False, error="No bot bound to media target", error_kind="unknown")
        runtime = self._runtimes.get(bot_id)
        if runtime is None or not runtime.connected:
            return SendResult(success=False, error=f"Telegram bot '{bot_id}' is not connected", retryable=True)
        parsed = parse_chat_id(chat_id)
        _, raw_reply = parse_message_id(reply_to)
        thread_id = self._thread_id(metadata)
        html_caption = None
        use_html = False
        if caption:
            plain_caption = truncate_utf16(caption, 1024)
            candidate = render_html(plain_caption)
            if utf16_len(candidate) > 1024:
                html_caption = truncate_utf16(plain_fallback(plain_caption), 1024)
            else:
                html_caption = candidate
                use_html = True
        try:
            message = await runtime.send_media(
                kind,
                parsed.real_chat_id,
                file=file,
                caption=html_caption,
                reply_to=raw_reply,
                thread_id=thread_id,
                html=use_html,
                file_name=file_name,
            )
            raw_id = getattr(message, "message_id", None)
            return SendResult(
                success=True,
                message_id=message_id_for(bot_id, raw_id) if raw_id is not None else None,
            )
        except Exception as exc:
            return self._failure(exc, runtime.token)

    @staticmethod
    def _failure(exc: BaseException, token: str, sent: Optional[list[str]] = None) -> SendResult:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None and not isinstance(retry_after, (int, float)):
            try:
                retry_after = retry_after.total_seconds()
            except Exception:
                retry_after = None
        kind = classify_send_error(exc, str(exc))
        return SendResult(
            success=False,
            error=redact_error(exc, token),
            retryable=kind == "transient",
            retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
            error_kind=kind,
            raw_response={"partial_message_ids": tuple(sent or ())} if sent else None,
        )

    @staticmethod
    def _thread_id(metadata: Optional[dict]) -> Optional[str]:
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("thread_id")
        return str(value) if value not in (None, "") else None

    # -- inbound helpers ---------------------------------------------------
    @staticmethod
    def _chat_type(chat: Any) -> str:
        value = getattr(getattr(chat, "type", None), "value", getattr(chat, "type", None))
        value = str(value or "").lower()
        if value in {"private", "dm"}:
            return "dm"
        if value in {"group", "supergroup"}:
            return "group"
        if value == "channel":
            return "channel"
        return value or "group"

    @staticmethod
    def _display_name(user: Any) -> str:
        if user is None:
            return ""
        name = getattr(user, "full_name", None) or " ".join(
            part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
        )
        return str(name or getattr(user, "username", None) or getattr(user, "id", "") or "")

    @staticmethod
    def _chat_name(chat: Any, user: Any, chat_type: str) -> str:
        if chat_type == "dm":
            return TelegramMultiAdapter._display_name(user) or str(getattr(chat, "id", ""))
        return str(getattr(chat, "title", None) or getattr(chat, "username", None) or getattr(chat, "id", ""))

    @staticmethod
    def _thread_id_from_message(message: Any) -> Optional[str]:
        value = getattr(message, "message_thread_id", None)
        if value in (None, ""):
            return None
        return str(value)

    @staticmethod
    def _message_text(message: Any) -> str:
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")

    @staticmethod
    def _command_target(message: Any) -> Optional[str]:
        text = str(getattr(message, "text", None) or "").strip()
        if not text.startswith("/"):
            return None
        token = text.split(maxsplit=1)[0][1:]
        _, sep, target = token.partition("@")
        return target.lower() if sep and target else None

    @staticmethod
    def _command_name(message: Any, username: Optional[str] = None) -> Optional[str]:
        text = str(getattr(message, "text", None) or "").strip()
        if not text.startswith("/"):
            return None
        token = text.split(maxsplit=1)[0][1:]
        command, sep, target = token.partition("@")
        if sep and username:
            # A command explicitly addressed to another bot must not be executed
            # (or count as a mention) here.
            if target.lower() != str(username).lower().lstrip("@"):
                return None
        command = command.lower()
        return command or None

    def _message_type(self, message: Any, text: str) -> MessageType:
        if text.strip().startswith("/"):
            return MessageType.COMMAND
        if getattr(message, "photo", None):
            return MessageType.PHOTO
        if getattr(message, "voice", None):
            return MessageType.VOICE
        if getattr(message, "audio", None):
            return MessageType.AUDIO
        if getattr(message, "document", None):
            return MessageType.DOCUMENT
        if getattr(message, "video", None):
            return MessageType.VIDEO
        if getattr(message, "animation", None):
            return MessageType.VIDEO
        return MessageType.TEXT

    def _is_addressed(self, runtime: Any, message: Any) -> bool:
        bot = self._mt.bot(runtime.bot_id)
        if bot is None or not bot.group_require_mention:
            return True
        username = (runtime.state.username or "").lower()
        target = self._command_target(message)
        if target is not None:
            # Explicit ``/command@bot``: only the named bot acts.
            return bool(username) and target == username
        if self._command_name(message, username):
            # A bare group command is ambiguous when several bots share the
            # group; accept it only when it is a reply to this bot.
            return self._is_reply_to_bot(runtime, message)
        if username:
            text = self._message_text(message)
            if re.search(rf"(?<![\w])@{re.escape(username)}(?![\w])", text, re.IGNORECASE):
                return True
        return self._is_reply_to_bot(runtime, message)

    @staticmethod
    def _is_reply_to_bot(runtime: Any, message: Any) -> bool:
        reply = getattr(message, "reply_to_message", None)
        if reply is None or reply.from_user is None or runtime.state.bot_user_id is None:
            return False
        try:
            return int(reply.from_user.id) == int(runtime.state.bot_user_id)
        except (TypeError, ValueError):
            return False

    async def _send_welcome(
        self, runtime: Any, real_chat: str, thread_id: Optional[str] = None
    ) -> None:
        bot = self._mt.bot(runtime.bot_id)
        if bot is None:
            return
        text = bot.welcome_message or _WELCOME_DEFAULT.format(name=bot.display_name)
        try:
            for chunk in split_text(text, MAX_TELEGRAM_LENGTH):
                await runtime.send_text(
                    real_chat,
                    render_html(chunk),
                    thread_id=thread_id,
                    html=True,
                )
        except Exception as exc:
            logger.warning("[multi-telegram:%s] welcome send failed: %s", runtime.bot_id, redact_error(exc, runtime.token))

    # -- media -------------------------------------------------------------
    async def _prepare_media(self, runtime: Any, message: Any) -> "MediaBundle":
        bot = self._mt.bot(runtime.bot_id)
        cap = (bot.max_attachment_mb if bot else 20) * 1024 * 1024
        candidates: list[tuple[str, Any, str, str, str]] = []
        photo = getattr(message, "photo", None)
        if photo:
            largest = photo[-1]
            candidates.append(("photo", largest, ".jpg", "image/jpeg", "photo.jpg"))
        for kind, attr, default_ext in (
            ("document", "document", ""),
            ("voice", "voice", ".ogg"),
            ("audio", "audio", ""),
            ("video", "video", ".mp4"),
            ("animation", "animation", ".mp4"),
        ):
            media = getattr(message, attr, None)
            if media is not None:
                mime = str(getattr(media, "mime_type", None) or "")
                name = str(getattr(media, "file_name", None) or "")
                ext = os.path.splitext(name)[1] if name else default_ext
                candidates.append((kind, media, ext, mime, name))
                break

        if not candidates:
            return MediaBundle()
        kind, media, ext, mime, name = candidates[0]
        size = getattr(media, "file_size", None)
        if isinstance(size, int) and size > cap:
            return MediaBundle(
                blocked=True,
                note=(
                    f"That {kind} is larger than the configured {cap // (1024 * 1024)} MB limit. "
                    "Please send a smaller file."
                ),
            )
        try:
            from gateway.platforms.base import (
                cache_audio_from_bytes_async,
                cache_document_from_bytes_async,
                cache_image_from_bytes_async,
                cache_video_from_bytes_async,
            )

            remote = await runtime.bot.get_file(media.file_id)
            data = bytes(await remote.download_as_bytearray())
            if kind == "photo":
                path = await cache_image_from_bytes_async(data, ext or ".jpg")
                mime = mime or "image/jpeg"
            elif kind in {"voice", "audio"}:
                path = await cache_audio_from_bytes_async(data, ext or ".ogg")
                mime = mime or ("audio/ogg" if kind == "voice" else "audio/mpeg")
            elif kind in {"video", "animation"}:
                path = await cache_video_from_bytes_async(data, ext or ".mp4")
                mime = mime or "video/mp4"
            else:
                path = await cache_document_from_bytes_async(data, name or "document")
                mime = mime or "application/octet-stream"
            return MediaBundle(
                paths=[path],
                types=[mime],
                filenames=[name or os.path.basename(path)],
                note=self._media_note(kind),
            )
        except Exception as exc:
            logger.warning(
                "[multi-telegram:%s] could not download %s attachment: %s",
                runtime.bot_id,
                kind,
                redact_error(exc, runtime.token),
            )
            return MediaBundle(note=f"[The user sent a {kind} that could not be downloaded.]")

    @staticmethod
    def _media_note(kind: str) -> str:
        return {
            "photo": "[The user sent a photo; the cached file is attached.]",
            "voice": "[The user sent a voice message; the cached audio is attached.]",
            "audio": "[The user sent an audio file; the cached file is attached.]",
            "document": "[The user sent a document; the cached file is attached.]",
            "video": "[The user sent a video; the cached file is attached.]",
            "animation": "[The user sent an animation; the cached file is attached.]",
        }.get(kind, "[The user sent an attachment.]")

    # -- prompt ------------------------------------------------------------
    def _channel_prompt(self, runtime: Any) -> str:
        bot = self._mt.bot(runtime.bot_id)
        if bot is None:
            return ""
        username = runtime.state.username
        lines = [
            f'You are replying through the "{bot.display_name}" Telegram bot'
            + (f" (@{username})" if username else "")
            + ".",
            "This is a communication identity for the same Hermes Agent profile; "
            "it is not a separate AI agent or a separate model configuration.",
        ]
        if self._mt.shared_context:
            lines.append(
                "Shared context for every bot in this deployment:\n"
                + self._mt.shared_context
            )
        if bot.purpose:
            lines.append(f"Bot purpose: {bot.purpose}")
        if bot.instructions:
            lines.append("Bot-specific operating instructions:\n" + bot.instructions)
        if bot.session_mode == "shared":
            lines.append(
                "This bot is in a shared-session group: the transcript is shared with "
                "the other bots assigned to the same group for this external chat."
            )
        return "\n".join(lines)[:12000]

    # -- status ------------------------------------------------------------
    async def _runtime_state_changed(self, runtime: Any) -> None:
        try:
            await self._status.set_bot(
                runtime.bot_id,
                state=runtime.state.state,
                username=runtime.state.username,
                bot_user_id=runtime.state.bot_user_id,
                error_code=runtime.state.error_code,
                error=redact_text(runtime.state.error or "", runtime.token)[:500] or None,
            )
        except Exception:
            logger.debug("[multi-telegram] status write failed", exc_info=True)

        connected = [rt for rt in self._runtimes.values() if rt.connected]
        live = [rt for rt in self._runtimes.values() if rt.running]
        if connected:
            if not self._running:
                self._mark_connected()
            await self._safe_overall("connected")
        elif live:
            if self._running:
                self._mark_degraded()
            await self._safe_overall("degraded")
        else:
            self._mark_disconnected()
            await self._safe_overall("disconnected")

    async def _safe_overall(self, state: str) -> None:
        try:
            await self._status.set_overall(state)
        except Exception:
            logger.debug("[multi-telegram] overall status write failed", exc_info=True)


class MediaBundle:
    __slots__ = ("paths", "types", "filenames", "note", "blocked")

    def __init__(
        self,
        paths: Optional[list[str]] = None,
        types: Optional[list[str]] = None,
        filenames: Optional[list[str]] = None,
        note: str = "",
        blocked: bool = False,
    ):
        self.paths = paths or []
        self.types = types or []
        self.filenames = filenames or []
        self.note = note
        self.blocked = blocked
