"""Test support: load the plugin exactly like Hermes' directory loader does."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "multi_telegram_plugin_under_test"


def _load_plugin() -> object:
    if PKG in sys.modules:
        return sys.modules[PKG]
    init_file = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        PKG, str(init_file), submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def module(name: str):
    return importlib.import_module(f"{PKG}.{name}")


def ensure_platform():
    from gateway.config import Platform

    try:
        return Platform("telegram_multi")
    except ValueError:
        return Platform._add_pseudo_member("telegram_multi")


from types import SimpleNamespace  # noqa: E402
from typing import Any, Optional  # noqa: E402


class FakeRuntime:
    """A PTB-free stand-in for BotRuntime with the methods the adapter uses."""

    def __init__(
        self,
        bot_id: str,
        *,
        username: Optional[str] = None,
        bot_user_id: int = 1000,
        connected: bool = True,
    ):
        self.bot_id = bot_id
        self.token = "123456:" + "b" * 35
        self.state = SimpleNamespace(
            state="connected" if connected else "disconnected",
            username=username or f"{bot_id}_bot",
            bot_user_id=bot_user_id,
            error_code=None,
            error=None,
        )
        self.connected = connected
        self.running = True
        self.sent: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.typing: list[dict[str, Any]] = []
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_text(
        self,
        real_chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        html: bool = True,
        disable_preview: bool = True,
    ):
        self.sent.append(
            {
                "chat_id": real_chat_id,
                "content": content,
                "reply_to": reply_to,
                "thread_id": thread_id,
                "html": html,
            }
        )
        return SimpleNamespace(message_id=self._id())

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
    ):
        if kind == "voice" and getattr(self, "fail_voice", False):
            raise RuntimeError("voice rejected")
        self.media.append(
            {
                "kind": kind,
                "chat_id": real_chat_id,
                "file": file,
                "caption": caption,
                "reply_to": reply_to,
                "thread_id": thread_id,
                "html": html,
                "file_name": file_name,
            }
        )
        return SimpleNamespace(message_id=self._id())

    async def send_typing(self, real_chat_id: str, thread_id: Optional[str] = None):
        self.typing.append({"chat_id": real_chat_id, "thread_id": thread_id})

    async def edit_text(self, real_chat_id: str, message_id: str, content: str, **kwargs):
        self.edits.append(
            {"chat_id": real_chat_id, "message_id": message_id, "content": content, **kwargs}
        )
        return SimpleNamespace(message_id=int(message_id))

    async def delete(self, real_chat_id: str, message_id: str) -> bool:
        self.deleted.append({"chat_id": real_chat_id, "message_id": message_id})
        return True

    async def stop(self) -> None:
        self.connected = False
        self.running = False


def make_adapter(tmp_path: Path, extra: dict[str, Any]):
    """Build an adapter with status/receipt state redirected into *tmp_path*."""
    from unittest.mock import patch

    from gateway.config import PlatformConfig

    ensure_platform()
    adapter_mod = module("adapter")
    status_mod = module("status_store")
    with patch.object(status_mod, "_data_dir", return_value=tmp_path):
        adapter = adapter_mod.TelegramMultiAdapter(PlatformConfig(enabled=True, extra=extra))
    return adapter


def install_capture(adapter) -> list:
    from types import MethodType

    events: list = []

    async def capture(self, event):
        self.events.append(event)

    adapter.events = events
    adapter.handle_message = MethodType(capture, adapter)
    return events


def make_message(
    *,
    message_id: int = 10,
    chat_id: object = 42,
    chat_type: str = "private",
    user_id: int = 7,
    text: str = "hello",
    reply_to: Optional[Any] = None,
    username: str = "alice",
):
    from datetime import datetime

    chat = SimpleNamespace(
        id=chat_id,
        type=chat_type,
        title="Test Group",
        username="test_group",
        full_name="Alice",
    )
    user = SimpleNamespace(
        id=user_id,
        is_bot=False,
        username=username,
        full_name="Alice",
        first_name="Alice",
        last_name=None,
    )
    return SimpleNamespace(
        message_id=message_id,
        chat=chat,
        from_user=user,
        text=text,
        caption=None,
        date=datetime.now(),
        reply_to_message=reply_to,
        message_thread_id=None,
        entities=None,
        photo=None,
        voice=None,
        audio=None,
        document=None,
        video=None,
        animation=None,
        sticker=None,
        location=None,
    )


def make_update(*, message=None, update_id: int = 1, callback_query=None):
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        callback_query=callback_query,
    )


def bot_config(**overrides):
    values = {
        "id": "alpha",
        "display_name": "Alpha",
        "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
        "enabled": True,
        "session_mode": "isolated",
        "allowed_users": ("7",),
        "allowed_chats": (),
        "allow_all_users": False,
        "allow_bots": False,
        "group_require_mention": True,
        "purpose": "",
        "instructions": "",
        "welcome_message": "",
        "toolsets": (),
        "skills": (),
        "proxy": None,
        "api_base_url": None,
        "file_base_url": None,
        "drop_pending_updates": True,
        "request_timeout": 30.0,
        "connect_timeout": 10.0,
        "read_timeout": 30.0,
        "max_attachment_mb": 20,
    }
    values.update(overrides)
    return module("config").BotConfig(**values)
