"""Registration, passive dependency probe, and platform status predicates."""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from .config import ConfigError, parse_config, resolve_token

logger = logging.getLogger(__name__)

PLATFORM_NAME = "telegram_multi"
PLUGIN_KEY = "multi-telegram"
INSTALL_HINT = (
    "Install the plugin's Python dependencies with "
    "`hermes plugins validate <plugin-dir> --install-deps` (python-telegram-bot 22.8)."
)


def check_requirements() -> bool:
    """PASSIVE registry probe: is python-telegram-bot importable right now?

    This must never install anything: Hermes calls it from status displays and
    the config enablement pass.
    """
    try:
        return importlib.util.find_spec("telegram") is not None
    except (ImportError, ValueError):
        return False


def ensure_dependencies() -> bool:
    """ACTIVE lazy installer called by ``create_adapter`` when PTB is absent.

    Hermes ships a ``telegram`` extra, so the plugin reuses the host's package
    manager instead of running an ad-hoc pip install.
    """
    if check_requirements():
        return True
    try:
        from pm import ensure_import

        ensure_import("telegram")
    except Exception:
        logger.warning(
            "multi-telegram: python-telegram-bot is not installed and the lazy "
            "install failed; run `hermes plugins validate <plugin-dir> --install-deps`",
            exc_info=True,
        )
        return False
    return check_requirements()


def adapter_factory(config: Any) -> Any:
    """Build the adapter lazily so importing this plugin does not import PTB."""
    from .adapter import TelegramMultiAdapter

    return TelegramMultiAdapter(config)


def _config_from_platform_config(pconfig: Any):
    extra = getattr(pconfig, "extra", None)
    return parse_config(extra if isinstance(extra, dict) else {})


def is_connected(pconfig: Any) -> bool:
    """Configuration-level connectedness: at least one enabled bot has a token.

    This is deterministic and read-only.  The real connection state is reported
    by the adapter/status file; Hermes uses this predicate to decide whether the
    platform is configured at all.
    """
    try:
        cfg = _config_from_platform_config(pconfig)
    except ConfigError:
        return False
    for bot in cfg.enabled_bots:
        if resolve_token(bot):
            return True
    return False


def register(ctx: Any) -> None:
    """Register the single ``telegram_multi`` platform and its CLI command."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Telegram (multi-bot)",
        adapter_factory=adapter_factory,
        check_fn=check_requirements,
        ensure_deps_fn=ensure_dependencies,
        is_connected=is_connected,
        install_hint=INSTALL_HINT,
        max_message_length=4096,
        emoji="✈️",
        # Hermes reads extra.allowed_users for a plugin platform only when the
        # registry entry declares this env name. The adapter publishes the
        # per-bot union into that key; an operator may also set this env var.
        allowed_users_env="TELEGRAM_MULTI_ALLOWED_USERS",
        allow_update_command=True,
        pii_safe=False,
        display_tier="high",
        display_overrides={"tool_progress": "off", "busy_ack_detail": False},
    )
    try:
        from .cli import dispatch, register_cli

        ctx.register_cli_command(
            name="multi-telegram",
            help="Validate and inspect the multi-bot Telegram platform",
            setup_fn=register_cli,
            handler_fn=dispatch,
            description=(
                "Inspect per-bot connection state, validate the telegram_multi "
                "configuration, and optionally perform a live getMe/send test. "
                "Tokens are read from the active Hermes profile and never printed."
            ),
        )
    except Exception:
        logger.warning(
            "multi-telegram: could not register the `hermes multi-telegram` CLI command",
            exc_info=True,
        )
