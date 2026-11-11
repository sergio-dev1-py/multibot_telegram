"""Simulated demo: no Telegram token and no network access required.

Run with the Hermes Python environment from the repository root:

    python scripts/demo.py

It shows:
* the synthetic chat ids for isolated vs shared bots;
* that shared bots resolve to the same conversation scope while users, chats
  and topics remain separate;
* how outbound routing is proved from chat ids / reply ids;
* why there is no global "last active bot".

The real Hermes session key is derived from this synthetic chat id by Hermes;
tests/test_session_identity.py verifies the exact keys, including shared-bot
equality, with the real gateway function.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "multi_telegram_demo"


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        PKG, str(ROOT / "__init__.py"), submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()
config_mod = importlib.import_module(f"{PKG}.config")
ident = importlib.import_module(f"{PKG}.session_identity")


def main() -> int:
    cfg = config_mod.parse_config(
        {
            "default_bot": "soporte",
            "bots": [
                {
                    "id": "soporte",
                    "display_name": "NovaDesk Support",
                    "token_env": "TELEGRAM_MULTI_SOPORTE_TOKEN",
                    "allowed_users": [42],
                },
                {
                    "id": "ventas",
                    "display_name": "NovaDesk Sales",
                    "token_env": "TELEGRAM_MULTI_VENTAS_TOKEN",
                    "allowed_users": [42],
                    "session_mode": "shared",
                    "shared_group": "novadesk",
                },
                {
                    "id": "tecnico",
                    "display_name": "NovaDesk Technical",
                    "token_env": "TELEGRAM_MULTI_TECNICO_TOKEN",
                    "allowed_users": [42],
                    "session_mode": "shared",
                    "shared_group": "novadesk",
                },
            ],
        }
    )

    print("Simulated multi-bot demo (no network, no tokens)\n")
    print("1) Synthetic conversation scope for group -100123, user 42:")
    scopes = {}
    for bot in cfg.bots:
        chat_id = ident.logical_chat_id(bot, -100123, 42)
        scopes[bot.id] = chat_id
        print(f"   {bot.id:<12} {chat_id}")
    print()
    print("2) Shared transcript scope check:")
    print(f"   ventas == tecnico ? {scopes['ventas'] == scopes['tecnico']}")
    print(f"   soporte == ventas ? {scopes['soporte'] == scopes['ventas']}")
    print()

    shared = ident.logical_chat_id(cfg.bot("ventas"), -100123, 42)
    print("3) Outbound routing proof (order, no global last bot):")
    print(
        "   isolated chat id ->",
        ident.resolve_bot_id(chat_id="tgmt:i:soporte:-100123:42"),
    )
    print(
        "   reply prefix     ->",
        ident.resolve_bot_id(chat_id=shared, reply_to="tecnico:55"),
    )
    print(
        "   explicit metadata ->",
        ident.resolve_bot_id(
            chat_id=shared, metadata={"multi_telegram_bot_id": "ventas"}
        ),
    )
    print(
        "   shared route only ->",
        ident.resolve_bot_id(chat_id=shared, shared_route_bot_id="ventas"),
    )
    print(
        "   nothing provable  ->",
        ident.resolve_bot_id(chat_id=shared),
    )
    print()
    print("4) A different user is always a different conversation scope:")
    other = ident.logical_chat_id(cfg.bot("ventas"), -100123, 99)
    print(f"   user 42: {scopes['ventas']}")
    print(f"   user 99: {other}")
    print()
    print("5) The exact Hermes session keys are asserted in the automated tests;")
    print("   shared bots produce the same key, different users do not.")
    print()
    print("No reply was sent and no token was read. For a live smoke test:")
    print(
        "   hermes multi-telegram validate --network\n"
        "   hermes multi-telegram test --bot soporte --chat <numeric-id>"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
