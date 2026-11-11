"""``hermes multi-telegram ...`` CLI.

Token-safety rules for this module:
* never print, format, or log a token or a token fragment;
* report only the environment-variable *name* and whether a value is present;
* redact PTB error text before printing it (PTB errors can echo a URL with the
  token embedded).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .config import (
    ConfigError,
    MultiTelegramConfig,
    load_platform_config,
    redact_error,
    redact_text,
    resolve_token,
    token_shape_ok,
    validate_bot_config,
)


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="multi_telegram_command", required=False)

    p_status = subs.add_parser("status", help="Show per-bot connection and token-presence state")
    p_status.add_argument("--json", action="store_true", help="Machine-readable output")

    p_validate = subs.add_parser(
        "validate", help="Validate the telegram_multi configuration (and optionally the tokens)"
    )
    p_validate.add_argument(
        "--no-tokens",
        action="store_true",
        help="Validate structure only; do not require configured tokens",
    )
    p_validate.add_argument(
        "--network",
        action="store_true",
        help="Call Telegram getMe for every enabled bot (live network; token-safe output)",
    )
    p_validate.add_argument("--json", action="store_true", help="Machine-readable output")

    p_test = subs.add_parser("test", help="Live getMe, and optionally send a test message")
    p_test.add_argument("--bot", default=None, help="Bot id (defaults to default_bot or the only enabled bot)")
    p_test.add_argument("--chat", default=None, help="Numeric Telegram chat id to send to")
    p_test.add_argument("--thread", default=None, help="Optional forum topic / message thread id")
    p_test.add_argument("--text", default="Hermes multi-bot Telegram test message.", help="Message text")
    p_test.add_argument(
        "--no-send",
        action="store_true",
        help="Only call getMe; do not send a message (no --chat required)",
    )
    p_test.add_argument("--json", action="store_true", help="Machine-readable output")

    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "multi_telegram_command", None) or "status"
    handlers = {
        "status": _cmd_status,
        "validate": _cmd_validate,
        "test": _cmd_test,
    }
    handler = handlers.get(command)
    if handler is None:
        print(f"Unknown multi-telegram subcommand: {command}", file=sys.stderr)
        return 2
    return handler(args)


# -- configuration ---------------------------------------------------------


def _load_config() -> tuple[Optional[MultiTelegramConfig], Optional[Any], list[str]]:
    try:
        _gateway, pconfig = load_platform_config()
    except ConfigError as exc:
        return None, None, [str(exc)]
    try:
        cfg = _config_from_extra(getattr(pconfig, "extra", {}) or {})
    except ConfigError as exc:
        return None, pconfig, [str(exc)]
    return cfg, pconfig, []


def _config_from_extra(extra: dict[str, Any]) -> MultiTelegramConfig:
    from .config import parse_config

    return parse_config(extra)


def _status_path() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("multi-telegram") / "status.json"
    except Exception:
        return Path.cwd() / ".multi-telegram-data" / "status.json"


def _read_status() -> dict[str, Any]:
    path = _status_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _token_present(bot: Any) -> bool:
    return bool(resolve_token(bot))


def _bot_public_row(bot: Any, runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": bot.id,
        "display_name": bot.display_name,
        "enabled": bot.enabled,
        "session_mode": bot.session_mode,
        "shared_group": bot.shared_group,
        "token_env": bot.token_env,
        "token_present": _token_present(bot) if bot.enabled else False,
        "allowed_users": list(bot.allowed_users),
        "allowed_chats": list(bot.allowed_chats),
        "allow_all_users": bot.allow_all_users,
        "group_require_mention": bot.group_require_mention,
        "state": runtime.get("state", "not_started"),
        "username": runtime.get("username"),
        "error_code": runtime.get("error_code"),
        "error": redact_text(runtime.get("error") or "") or None,
        "updated_at": runtime.get("updated_at"),
    }


# -- status ----------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    cfg, _pconfig, problems = _load_config()
    status = _read_status()
    if cfg is None:
        payload = {"ok": False, "problems": problems, "status": status}
        return _emit(payload, json_mode=args.json, text=_format_problems(problems))

    runtimes = status.get("bots") if isinstance(status.get("bots"), dict) else {}
    rows = [_bot_public_row(bot, runtimes.get(bot.id, {}) or {}) for bot in cfg.bots]
    payload = {
        "ok": not problems,
        "overall": status.get("state", "not_started"),
        "pid": status.get("pid"),
        "updated_at": status.get("updated_at"),
        "problems": problems,
        "warnings": list(getattr(cfg, "warnings", ()) or ()),
        "bots": rows,
    }
    if args.json:
        return _emit(payload, json_mode=True)
    lines = [f"Overall: {payload['overall']}"]
    if status.get("pid"):
        lines.append(f"Gateway pid: {status['pid']}")
    lines.append("")
    for row in rows:
        token = "set" if row["token_present"] else "MISSING"
        if not row["enabled"]:
            token = "disabled"
        lines.append(
            f"{row['id']}  [{row['state']}]  @{row['username'] or '-'}  "
            f"token={token}  ({row['token_env']})"
        )
        mode = row["session_mode"]
        if mode == "shared":
            mode += f" (group={row['shared_group']})"
        lines.append(
            f"  display={row['display_name']}  mode={mode}  "
            f"users={len(row['allowed_users'])}  chats={len(row['allowed_chats'])}  "
            f"allow_all={row['allow_all_users']}  require_mention={row['group_require_mention']}"
        )
        if row.get("error_code") or row.get("error"):
            lines.append(f"  error: {row.get('error_code') or ''} {row.get('error') or ''}".rstrip())
    if cfg.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in cfg.warnings)
    if problems:
        lines.append("")
        lines.extend(_format_problems(problems))
    print("\n".join(lines))
    return 0 if not problems else 1


# -- validation ------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        _gateway, pconfig = load_platform_config()
    except ConfigError as exc:
        return _emit(
            {"ok": False, "problems": [str(exc)]},
            json_mode=args.json,
            text=_format_problems([str(exc)]),
        )

    cfg, problems = validate_bot_config(
        getattr(pconfig, "extra", {}) or {},
        require_tokens=not args.no_tokens,
        check_shape=True,
    )
    rows = []
    for bot in cfg.bots:
        present = _token_present(bot) if bot.enabled else False
        rows.append(
            {
                "id": bot.id,
                "enabled": bot.enabled,
                "session_mode": bot.session_mode,
                "shared_group": bot.shared_group,
                "token_env": bot.token_env,
                "token_present": present,
                "token_shape_ok": token_shape_ok(resolve_token(bot)) if present else None,
            }
        )

    network: list[dict[str, Any]] = []
    if args.network:
        enabled = cfg.enabled_bots
        network = asyncio.run(_network_check(enabled))
        problems.extend(item["problem"] for item in network if item.get("problem"))

    payload = {
        "ok": not problems,
        "bots": rows,
        "problems": problems,
        "warnings": list(getattr(cfg, "warnings", ()) or ()),
        "network": network,
    }
    if args.json:
        return _emit(payload, json_mode=True)
    lines = ["Configuration is valid." if not problems else "Configuration has problems:"]
    if problems:
        lines.extend(_format_problems(problems))
    lines.append("")
    for row in rows:
        token = "set" if row["token_present"] else ("disabled" if not row["enabled"] else "MISSING")
        lines.append(
            f"{row['id']}: enabled={row['enabled']} token={token} "
            f"env={row['token_env']} mode={row['session_mode']}"
        )
    for item in network:
        if item.get("ok"):
            lines.append(f"  network: {item['bot_id']} -> @{item.get('username') or '?'} (id {item.get('id')})")
        else:
            lines.append(f"  network: {item['bot_id']} FAILED: {item.get('problem')}")
    if payload["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in payload["warnings"])
    print("\n".join(lines))
    return 0 if not problems else 1


async def _network_check(bots: list[Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for bot in bots:
        token = resolve_token(bot)
        if not token:
            results.append({"bot_id": bot.id, "ok": False, "problem": "token is not set"})
            continue
        try:
            username, bot_id = await _get_me(token, bot)
        except Exception as exc:
            results.append(
                {
                    "bot_id": bot.id,
                    "ok": False,
                    "problem": redact_error(exc, token),
                }
            )
        else:
            results.append(
                {"bot_id": bot.id, "ok": True, "username": username, "id": bot_id}
            )
    return results


# -- live test -------------------------------------------------------------


def _select_bot(cfg: MultiTelegramConfig, requested: Optional[str]) -> Any:
    if requested:
        bot = cfg.bot(requested)
        if bot is None:
            raise ConfigError(f"unknown bot id: {requested}")
        return bot
    if cfg.default_bot:
        bot = cfg.bot(cfg.default_bot)
        if bot is not None:
            return bot
    enabled = cfg.enabled_bots
    if not enabled:
        raise ConfigError("no enabled bots")
    if len(enabled) == 1:
        return enabled[0]
    raise ConfigError("multiple bots are enabled; pass --bot <id>")


def _cmd_test(args: argparse.Namespace) -> int:
    try:
        _gateway, pconfig = load_platform_config()
        cfg = _config_from_extra(getattr(pconfig, "extra", {}) or {})
    except ConfigError as exc:
        return _emit({"ok": False, "problem": str(exc)}, json_mode=args.json, text=str(exc))

    try:
        bot = _select_bot(cfg, args.bot)
    except ConfigError as exc:
        return _emit({"ok": False, "problem": str(exc)}, json_mode=args.json, text=str(exc))
    if not bot.enabled:
        return _emit(
            {"ok": False, "problem": f"bot {bot.id!r} is disabled"},
            json_mode=args.json,
            text=f"bot {bot.id!r} is disabled",
        )
    token = resolve_token(bot)
    if not token:
        problem = f"{bot.token_env} is not set in the active Hermes profile"
        return _emit({"ok": False, "problem": problem}, json_mode=args.json, text=problem)
    if not token_shape_ok(token):
        problem = f"{bot.token_env} does not look like a Telegram bot token"
        return _emit({"ok": False, "problem": problem}, json_mode=args.json, text=problem)
    if not args.no_send and not args.chat:
        problem = "--chat is required unless --no-send is used"
        return _emit({"ok": False, "problem": problem}, json_mode=args.json, text=problem)

    try:
        result = asyncio.run(_live_test(token, bot, args))
    except Exception as exc:
        problem = redact_error(exc, token)
        return _emit({"ok": False, "problem": problem}, json_mode=args.json, text=problem)

    payload = {"ok": True, "bot_id": bot.id, **result}
    if args.json:
        return _emit(payload, json_mode=True)
    lines = [f"{bot.id}: getMe OK -> @{result.get('username') or '?'} (id {result.get('id')})"]
    if result.get("message_id") is not None:
        lines.append(f"sent test message to chat {result.get('chat_id')} (message_id={result['message_id']})")
    print("\n".join(lines))
    return 0


# -- PTB helpers (lazy) ----------------------------------------------------


def _request_for(bot: Any) -> Any:
    from telegram.request import HTTPXRequest  # type: ignore

    return HTTPXRequest(
        connect_timeout=bot.connect_timeout,
        read_timeout=bot.read_timeout,
        write_timeout=bot.request_timeout,
        pool_timeout=bot.request_timeout,
        proxy=bot.proxy,
    )


async def _get_me(token: str, bot_config: Any) -> tuple[str, Optional[int]]:
    from telegram import Bot  # type: ignore

    try:
        from .bot_runtime import _install_log_redaction

        _install_log_redaction()
    except Exception:
        pass
    bot = Bot(token=token, request=_request_for(bot_config))
    await bot.initialize()
    try:
        me = await bot.get_me()
        return str(getattr(me, "username", "") or ""), getattr(me, "id", None)
    finally:
        try:
            await bot.shutdown()
        except Exception:
            pass


async def _live_test(token: str, bot_config: Any, args: argparse.Namespace) -> dict[str, Any]:
    from telegram import Bot  # type: ignore

    try:
        from .bot_runtime import _install_log_redaction

        _install_log_redaction()
    except Exception:
        pass
    bot = Bot(token=token, request=_request_for(bot_config))
    await bot.initialize()
    result: dict[str, Any] = {}
    try:
        me = await bot.get_me()
        result["username"] = str(getattr(me, "username", "") or "")
        result["id"] = getattr(me, "id", None)
        if args.no_send:
            return result
        kwargs: dict[str, Any] = {
            "chat_id": int(str(args.chat).strip()),
            "text": str(args.text or "")[:4096],
        }
        if args.thread not in (None, ""):
            kwargs["message_thread_id"] = int(str(args.thread).strip())
        message = await bot.send_message(**kwargs)
        result["chat_id"] = kwargs["chat_id"]
        result["message_id"] = getattr(message, "message_id", None)
        return result
    finally:
        try:
            await bot.shutdown()
        except Exception:
            pass


# -- output ----------------------------------------------------------------


def _format_problems(problems: list[str]) -> str:
    if not problems:
        return ""
    return "\n".join(f"  - {problem}" for problem in problems)


def _emit(payload: dict[str, Any], *, json_mode: bool, text: str = "") -> int:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text:
        print(text)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", False) else 1
