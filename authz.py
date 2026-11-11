"""Per-bot authorization, enforced before any message reaches the agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


class BotAuthorizer:
    def __init__(self, bot):
        self.bot = bot
        self.allowed_users = frozenset(str(x) for x in (getattr(bot, "allowed_users", ()) or ()))
        self.allowed_chats = frozenset(str(x) for x in (getattr(bot, "allowed_chats", ()) or ()))
        self.allow_all = bool(getattr(bot, "allow_all_users", False))
        self.allow_bots = bool(getattr(bot, "allow_bots", False))

    def authorize(
        self,
        *,
        chat_id: str,
        chat_type: str,
        user_id: Optional[str],
        is_bot: bool = False,
    ) -> Decision:
        chat = str(chat_id or "").strip()
        user = str(user_id or "").strip()
        if not user:
            return Decision(False, "missing sender id")
        if is_bot and not self.allow_bots:
            return Decision(False, "bot sender")
        if self.allow_all:
            return Decision(True)
        if not self.allowed_users and not self.allowed_chats:
            return Decision(False, "no allowlist configured")

        if chat_type == "dm":
            if user in self.allowed_users or chat in self.allowed_chats:
                return Decision(True)
            return Decision(False, "sender not on DM allowlist")

        chat_ok = not self.allowed_chats or chat in self.allowed_chats
        user_ok = not self.allowed_users or user in self.allowed_users
        if chat_ok and user_ok:
            return Decision(True)
        if not chat_ok:
            return Decision(False, "chat not on group allowlist")
        return Decision(False, "sender not on group allowlist")


def union_allows_dm(config, user_id: Optional[str]) -> bool:
    """Gateway-level DM gate.

    Hermes checks authorization per platform, not per bot.  The adapter's intake
    check is authoritative; this union only lets a user reach the intake of a bot
    that actually lists them.  ``allow_all_users`` on any bot intentionally makes
    the platform-wide gate permissive, after which each bot still applies its own
    policy.
    """
    user = str(user_id or "").strip()
    if not user:
        return False
    for bot in getattr(config, "enabled_bots", ()):
        if getattr(bot, "allow_all_users", False):
            return True
        if user in {str(x) for x in (getattr(bot, "allowed_users", ()) or ())}:
            return True
        # A DM chat id is the user id; a bot may authorize it through either
        # per-bot list.  Keep the gateway's platform-wide union consistent with
        # the adapter's DM rule.
        if user in {str(x) for x in (getattr(bot, "allowed_chats", ()) or ())}:
            return True
    return False


def allowed_user_union(config) -> set[str]:
    users: set[str] = set()
    for bot in getattr(config, "enabled_bots", ()):
        users.update(str(x) for x in (getattr(bot, "allowed_users", ()) or ()))
    return users


def matching_ids(ids: Iterable[str], candidate: Optional[str]) -> bool:
    return str(candidate or "") in {str(x) for x in ids}
