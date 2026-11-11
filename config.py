"""Configuration parsing and validation for Hermes Multi-Telegram.

The plugin is configured under ``gateway.platforms.telegram_multi`` in
``config.yaml``.  Every key below is intentionally read from
``PlatformConfig.extra``; Hermes promotes unknown platform-block keys into that
dict automatically.

Tokens are never stored in this file.  A bot names an environment variable via
``token_env`` and the value is resolved through the active Hermes profile's
secret scope.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
NUMERIC_ID_RE = re.compile(r"^-?\d{1,24}$")
TOKEN_SHAPE_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
# Broad enough to catch a token embedded in an exception message or URL, but
# specific enough not to eat ordinary timestamps/ports.  The whole match is
# replaced, so no fragment of the credential survives in logs.
TOKEN_REDACT_RE = re.compile(r"(?<![A-Za-z0-9_])(?:bot)?\d{6,}:[A-Za-z0-9_-]{25,}")

SESSION_MODES = {"isolated", "shared"}


class ConfigError(ValueError):
    """A configuration error safe to show to the operator (never contains a token)."""


@dataclass(frozen=True)
class BotConfig:
    id: str
    display_name: str
    token_env: str
    enabled: bool = True
    session_mode: str = "isolated"
    shared_group: Optional[str] = None
    allowed_users: tuple[str, ...] = ()
    allowed_chats: tuple[str, ...] = ()
    allow_all_users: bool = False
    allow_bots: bool = False
    group_require_mention: bool = True
    purpose: str = ""
    instructions: str = ""
    welcome_message: str = ""
    toolsets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    proxy: Optional[str] = None
    api_base_url: Optional[str] = None
    file_base_url: Optional[str] = None
    drop_pending_updates: bool = True
    request_timeout: float = 30.0
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_attachment_mb: int = 20

    @property
    def policy_scope(self) -> str:
        """The scope component used in synthetic chat IDs."""
        return self.shared_group if self.session_mode == "shared" else self.id

    @property
    def token_env_name(self) -> str:
        return self.token_env

    def public_dict(self) -> dict[str, Any]:
        """Status-safe representation.  No token and no token value."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "token_env": self.token_env,
            "enabled": self.enabled,
            "session_mode": self.session_mode,
            "shared_group": self.shared_group,
            "allowed_users": list(self.allowed_users),
            "allowed_chats": list(self.allowed_chats),
            "allow_all_users": self.allow_all_users,
            "allow_bots": self.allow_bots,
            "group_require_mention": self.group_require_mention,
            "toolsets": list(self.toolsets),
            "skills": list(self.skills),
        }


@dataclass
class MultiTelegramConfig:
    bots: list[BotConfig] = field(default_factory=list)
    default_bot: Optional[str] = None
    # Optional free-form context injected into every bot's ephemeral prompt.
    # Leave empty for a generic deployment; use it for a company/project brief,
    # not for secrets or long documents (put those in the Hermes workspace).
    shared_context: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def by_id(self) -> dict[str, BotConfig]:
        return {bot.id: bot for bot in self.bots}

    @property
    def enabled_bots(self) -> list[BotConfig]:
        return [bot for bot in self.bots if bot.enabled]

    @property
    def dm_policy(self) -> str:
        return "allowlist" if self.enabled_bots else "disabled"

    @property
    def group_policy(self) -> str:
        return "allowlist" if self.enabled_bots else "disabled"

    @property
    def union_allowed_users(self) -> set[str]:
        users: set[str] = set()
        for bot in self.enabled_bots:
            users.update(bot.allowed_users)
        return users

    @property
    def any_allow_all(self) -> bool:
        return any(bot.allow_all_users for bot in self.enabled_bots)

    def bot(self, bot_id: str) -> Optional[BotConfig]:
        return self.by_id.get((bot_id or "").strip().lower())

    def public_dict(self) -> dict[str, Any]:
        return {
            "default_bot": self.default_bot,
            "shared_context": self.shared_context,
            "bots": [bot.public_dict() for bot in self.bots],
            "warnings": list(self.warnings),
        }


def redact_text(text: Any, *secrets: str) -> str:
    """Remove Telegram tokens (and known exact secrets) from logs/errors."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<redacted-token>")
    return TOKEN_REDACT_RE.sub("<redacted-token>", value)


def redact_error(exc: BaseException, *secrets: str) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}", *secrets)[:500]


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on", "y"}:
            return True
        if v in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def _coerce_float(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not (low <= result <= high):
        return default
    return result


def _coerce_int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result))


def _str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # A single value is accepted; comma-separated is accepted for convenience.
        parts = value.split(",") if "," in value else [value]
    elif isinstance(value, Iterable):
        parts = list(value)
    else:
        return ()
    cleaned: list[str] = []
    for item in parts:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _string(value: Any, default: str = "", *, cap: int = 8192) -> str:
    if value is None:
        return default
    return str(value).strip()[:cap]


def _slug(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not ID_RE.fullmatch(text):
        raise ConfigError(
            f"{field_name} must be a lowercase slug matching "
            "[a-z0-9][a-z0-9_-]{0,31}"
        )
    return text


def _numeric_ids(value: Any, field_name: str) -> tuple[str, ...]:
    items = _str_list(value)
    bad = [item for item in items if not NUMERIC_ID_RE.fullmatch(item)]
    if bad:
        raise ConfigError(
            f"{field_name} must contain numeric Telegram IDs only "
            f"(username allowlists are not supported); {len(bad)} invalid entr"
            f"{'y' if len(bad) == 1 else 'ies'}"
        )
    return items


def _validate_url(value: Optional[str], field_name: str) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{field_name} must be an http(s) URL")
    return text


def _validate_proxy(value: Optional[str], field_name: str) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.netloc:
        raise ConfigError(
            f"{field_name} must be an http(s) or socks5 URL (e.g. http://127.0.0.1:7890)"
        )
    return text


def _parse_bot(raw: Any, index: int) -> BotConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"bots[{index}] must be a mapping")
    bot_id = _slug(raw.get("id"), f"bots[{index}].id")
    prefix = f"bots[{bot_id}]"
    token_env = str(raw.get("token_env") or raw.get("token_var") or "").strip()
    if not ENV_RE.fullmatch(token_env):
        raise ConfigError(
            f"{prefix}.token_env must be an uppercase environment-variable name "
            f"(e.g. TELEGRAM_MULTI_{bot_id.upper()}_TOKEN)"
        )
    if token_env in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_MULTI_BOT_TOKEN"}:
        raise ConfigError(
            f"{prefix}.token_env must not reuse {token_env}; that token belongs to "
            "the native Hermes Telegram adapter and two pollers would conflict"
        )

    mode = _string(raw.get("session_mode"), "isolated").lower()
    if mode not in SESSION_MODES:
        raise ConfigError(f"{prefix}.session_mode must be one of {sorted(SESSION_MODES)}")
    shared_group_raw = raw.get("shared_group")
    shared_group = _slug(shared_group_raw, f"{prefix}.shared_group") if shared_group_raw else None
    if mode == "shared" and not shared_group:
        raise ConfigError(f"{prefix}.shared_group is required when session_mode is 'shared'")

    bot = BotConfig(
        id=bot_id,
        display_name=_string(raw.get("display_name") or raw.get("name"), bot_id, cap=128),
        token_env=token_env,
        enabled=_coerce_bool(raw.get("enabled"), True),
        session_mode=mode,
        shared_group=shared_group,
        allowed_users=_numeric_ids(raw.get("allowed_users") or raw.get("allowed_user_ids"), f"{prefix}.allowed_users"),
        allowed_chats=_numeric_ids(raw.get("allowed_chats") or raw.get("allowed_groups"), f"{prefix}.allowed_chats"),
        allow_all_users=_coerce_bool(raw.get("allow_all_users"), False),
        allow_bots=_coerce_bool(raw.get("allow_bots"), False),
        group_require_mention=_coerce_bool(raw.get("group_require_mention", raw.get("require_mention")), True),
        purpose=_string(raw.get("purpose"), cap=4000),
        instructions=_string(raw.get("instructions"), cap=8000),
        welcome_message=_string(raw.get("welcome_message"), cap=4096),
        toolsets=_str_list(raw.get("toolsets")),
        skills=_str_list(raw.get("skills")),
        proxy=_validate_proxy(raw.get("proxy"), f"{prefix}.proxy"),
        api_base_url=_validate_url(raw.get("api_base_url"), f"{prefix}.api_base_url"),
        file_base_url=_validate_url(raw.get("file_base_url"), f"{prefix}.file_base_url"),
        drop_pending_updates=_coerce_bool(raw.get("drop_pending_updates"), True),
        request_timeout=_coerce_float(raw.get("request_timeout"), 30.0, low=1.0, high=300.0),
        connect_timeout=_coerce_float(raw.get("connect_timeout"), 10.0, low=1.0, high=60.0),
        read_timeout=_coerce_float(raw.get("read_timeout"), 30.0, low=1.0, high=300.0),
        max_attachment_mb=_coerce_int(raw.get("max_attachment_mb"), 20, low=1, high=2000),
    )
    if bot.allow_all_users and not (bot.allowed_users or bot.allowed_chats):
        logger.warning(
            "[multi-telegram:%s] allow_all_users is enabled; every Telegram user who can "
            "reach this bot will be admitted to the shared Hermes agent",
            bot_id,
        )
    if mode == "isolated" and shared_group:
        logger.warning("[multi-telegram:%s] shared_group is ignored in isolated mode", bot_id)
    if not bot.allowed_users and not bot.allowed_chats and not bot.allow_all_users:
        logger.warning(
            "[multi-telegram:%s] no allowed_users, allowed_chats, or allow_all_users configured; "
            "the bot will deny all inbound messages",
            bot_id,
        )
    return bot


def parse_config(extra: Any) -> MultiTelegramConfig:
    """Parse ``PlatformConfig.extra`` into a validated config.

    Raises :class:`ConfigError` for structural problems.  Token resolution is a
    separate step so status/validation can distinguish missing secrets from
    invalid configuration.
    """
    extra = extra if isinstance(extra, dict) else {}
    raw_bots = extra.get("bots")
    if raw_bots is None:
        raise ConfigError("telegram_multi.bots is required")
    if not isinstance(raw_bots, list):
        raise ConfigError("telegram_multi.bots must be a list")
    if not raw_bots:
        raise ConfigError("telegram_multi.bots must contain at least one bot")

    default_bot_raw = extra.get("default_bot")
    default_bot = _slug(default_bot_raw, "default_bot") if default_bot_raw else None
    shared_context = _string(extra.get("shared_context"), cap=6000)

    bots: list[BotConfig] = []
    seen_ids: set[str] = set()
    seen_env: dict[str, str] = {}
    for i, raw in enumerate(raw_bots):
        bot = _parse_bot(raw, i)
        if bot.id in seen_ids:
            raise ConfigError(f"duplicate bot id: {bot.id}")
        if bot.token_env in seen_env:
            raise ConfigError(
                f"bots {seen_env[bot.token_env]!r} and {bot.id!r} use the same token_env "
                f"{bot.token_env}; each bot needs its own token"
            )
        seen_ids.add(bot.id)
        seen_env[bot.token_env] = bot.id
        bots.append(bot)

    if default_bot and default_bot not in seen_ids:
        raise ConfigError(f"default_bot {default_bot!r} does not match any bot id")
    if default_bot:
        chosen = next(bot for bot in bots if bot.id == default_bot)
        if not chosen.enabled:
            raise ConfigError(f"default_bot {default_bot!r} is disabled")

    warnings: list[str] = []
    for bot in bots:
        if not bot.enabled:
            continue
        if bot.allow_all_users:
            warnings.append(
                f"bots[{bot.id}]: allow_all_users is enabled; treat that bot as public"
            )
        elif not bot.allowed_users and not bot.allowed_chats:
            warnings.append(
                f"bots[{bot.id}]: no allowed_users/allowed_chats and allow_all_users is false; "
                "all inbound messages will be denied"
            )
    return MultiTelegramConfig(
        bots=bots,
        default_bot=default_bot,
        shared_context=shared_context,
        warnings=warnings,
    )


def resolve_token(bot: BotConfig) -> str:
    """Resolve one bot token from the active Hermes profile secret scope."""
    try:
        from gateway.platforms._shared import get_scoped_secret
    except Exception:
        return ""
    try:
        value = get_scoped_secret(bot.token_env, "")
    except Exception:
        return ""
    token = str(value or "").strip()
    return token


def token_shape_ok(token: str) -> bool:
    return bool(TOKEN_SHAPE_RE.fullmatch(token or ""))


def validate_bot_config(
    extra: Any,
    *,
    require_tokens: bool = True,
    check_shape: bool = True,
) -> tuple[MultiTelegramConfig, list[str]]:
    """Return ``(config, problems)``.  Problems are operator-safe strings."""
    problems: list[str] = []
    try:
        config = parse_config(extra)
    except ConfigError as exc:
        return MultiTelegramConfig(), [str(exc)]

    seen_tokens: dict[str, str] = {}
    for bot in config.bots:
        if not bot.enabled:
            continue
        token = resolve_token(bot)
        if not token:
            if require_tokens:
                problems.append(f"bots[{bot.id}]: {bot.token_env} is not set in the profile secret scope")
            continue
        if check_shape and not token_shape_ok(token):
            problems.append(f"bots[{bot.id}]: {bot.token_env} does not look like a Telegram bot token")
        if token in seen_tokens:
            problems.append(
                f"bots[{bot.id}]: token for {bot.token_env} is identical to {seen_tokens[token]}; "
                "two pollers cannot share one token"
            )
        else:
            seen_tokens[token] = bot.id
    if not any(bot.enabled for bot in config.bots):
        problems.append("no enabled bots")
    return config, problems


def load_platform_config() -> tuple[Any, Any]:
    """Load the gateway config and return ``(gateway_config, PlatformConfig)``.

    Raises ``ConfigError`` when the platform is not configured.  This is used by
    the CLI and by ``PlatformEntry.validate_config``.
    """
    try:
        from gateway.config import Platform, load_gateway_config
    except Exception as exc:  # pragma: no cover - only outside Hermes
        raise ConfigError(f"Hermes gateway modules are unavailable: {exc}") from exc

    config = load_gateway_config()
    platform = Platform("telegram_multi")
    pconfig = (getattr(config, "platforms", {}) or {}).get(platform)
    if pconfig is None:
        raise ConfigError(
            "telegram_multi is not configured under gateway.platforms in config.yaml"
        )
    return config, pconfig
