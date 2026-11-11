"""Logical conversation identity for multi- Telegram.

Hermes derives session keys from ``SessionSource.platform``, ``chat_type``,
``chat_id``, ``thread_id`` and ``user_id`` (see ``gateway/session.py``).  We need
two different behaviours from the same platform adapter:

* isolated: each bot's conversation is its own session;
* shared: bots assigned to one ``shared_group`` continue one real transcript.

The supported way to express that difference without touching Hermes routing is
to make ``chat_id`` carry the logical conversation scope.  The real Telegram
chat id stays available inside the synthetic value and is decoded before every
Telegram API call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

PREFIX = "tgmt"
BOT_MESSAGE_SEP = ":"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ID_RE = re.compile(r"^-?\d{1,24}$")


@dataclass(frozen=True)
class ParsedChatId:
    mode: Optional[str]
    scope: Optional[str]
    real_chat_id: str
    user_id: Optional[str]
    raw: str

    @property
    def bot_id(self) -> Optional[str]:
        return self.scope if self.mode == "i" else None

    @property
    def shared_group(self) -> Optional[str]:
        return self.scope if self.mode == "s" else None

    @property
    def is_synthetic(self) -> bool:
        return self.mode is not None


def encode_chat_id(*, mode: str, scope: str, real_chat_id: Any, user_id: Any) -> str:
    if mode not in {"i", "s"}:
        raise ValueError("mode must be 'i' or 's'")
    if not _SLUG_RE.fullmatch(scope or ""):
        raise ValueError(f"invalid scope: {scope!r}")
    real = str(real_chat_id or "").strip()
    user = str(user_id or "").strip()
    if not _ID_RE.fullmatch(real):
        raise ValueError(f"invalid real chat id: {real!r}")
    if not _ID_RE.fullmatch(user):
        # Channels/unknown senders: use 0.  Keep the scope unique per chat.
        user = "0"
    return f"{PREFIX}:{mode}:{scope}:{real}:{user}"


def logical_chat_id(bot: Any, real_chat_id: Any, user_id: Any) -> str:
    """Build the session-scoping chat id for one configured bot."""
    if getattr(bot, "session_mode", "isolated") == "shared":
        return encode_chat_id(
            mode="s",
            scope=bot.shared_group or bot.id,
            real_chat_id=real_chat_id,
            user_id=user_id,
        )
    return encode_chat_id(
        mode="i", scope=bot.id, real_chat_id=real_chat_id, user_id=user_id
    )


def parse_chat_id(chat_id: Any) -> ParsedChatId:
    raw = str(chat_id or "").strip()
    if raw.startswith(f"{PREFIX}:"):
        parts = raw.split(":")
        if len(parts) == 5:
            _, mode, scope, real, user = parts
            if mode in {"i", "s"}:
                return ParsedChatId(mode=mode, scope=scope, real_chat_id=real, user_id=user, raw=raw)
    # Legacy/raw Telegram id (manual config, home channel, or an operator target).
    return ParsedChatId(mode=None, scope=None, real_chat_id=raw, user_id=None, raw=raw)


def message_id_for(bot_id: str, telegram_message_id: Any) -> str:
    return f"{bot_id}{BOT_MESSAGE_SEP}{telegram_message_id}"


def parse_message_id(message_id: Any) -> tuple[Optional[str], str]:
    raw = str(message_id or "").strip()
    if BOT_MESSAGE_SEP in raw:
        bot_id, _, rest = raw.partition(BOT_MESSAGE_SEP)
        if _SLUG_RE.fullmatch(bot_id):
            return bot_id, rest
    return None, raw


def source_bot_id(source: Any) -> Optional[str]:
    """Best-effort bot identity from a live or restored SessionSource."""
    if source is None:
        return None
    direct = getattr(source, "_mt_bot_id", None)
    if direct:
        return str(direct)
    bot_id, _ = parse_message_id(getattr(source, "message_id", None))
    if bot_id:
        return bot_id
    parsed = parse_chat_id(getattr(source, "chat_id", None))
    return parsed.bot_id


def resolve_bot_id(
    *,
    chat_id: Any = None,
    reply_to: Any = None,
    metadata: Optional[dict[str, Any]] = None,
    context_bot_id: Optional[str] = None,
    shared_route_bot_id: Optional[str] = None,
    known_bot_ids: Optional[set[str]] = None,
) -> Optional[str]:
    """Resolve the transport bot for an outbound send.

    Order is deliberately explicit; there is no global "last active bot".  A
    caller that cannot prove a bot must fail closed rather than guess.
    """
    known = known_bot_ids or set()

    parsed = parse_chat_id(chat_id)
    if parsed.bot_id and (not known or parsed.bot_id in known):
        return parsed.bot_id

    reply_bot, _ = parse_message_id(reply_to)
    if reply_bot and (not known or reply_bot in known):
        return reply_bot

    meta = metadata or {}
    for key in ("multi_telegram_bot_id", "mt_bot_id"):
        value = meta.get(key)
        if value and (not known or str(value) in known):
            return str(value)

    if context_bot_id and (not known or context_bot_id in known):
        return context_bot_id

    # Shared-session chat ids intentionally carry no bot identity: every bot in
    # the group must resolve to the SAME Hermes session.  For proactive sends
    # (cron, background notifications) that have no event binding, use the
    # per-conversation receiving-bot record persisted by the adapter.  This is
    # scoped to one shared conversation, never a process-global "last bot".
    if parsed.shared_group and shared_route_bot_id and (
        not known or shared_route_bot_id in known
    ):
        return shared_route_bot_id
    return None


def real_chat_id(chat_id: Any) -> str:
    return parse_chat_id(chat_id).real_chat_id


def real_user_id(chat_id: Any, fallback: Any = None) -> Optional[str]:
    parsed = parse_chat_id(chat_id)
    if parsed.user_id not in (None, "", "0"):
        return parsed.user_id
    if fallback not in (None, ""):
        return str(fallback)
    return None
