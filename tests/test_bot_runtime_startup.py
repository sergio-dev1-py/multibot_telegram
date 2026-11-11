from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .helper import bot_config, module

runtime_mod = module("bot_runtime")
bot_runtime = runtime_mod.BotRuntime


class FakeBot:
    def __init__(self):
        self.calls = []

    async def get_me(self):
        return SimpleNamespace(username="startup_bot", id=4242)

    async def delete_webhook(self, **kwargs):
        self.calls.append(("delete_webhook", kwargs))
        return True

    async def set_my_commands(self, commands):
        self.calls.append(("set_my_commands", {"commands": commands}))
        return True


class FakeUpdater:
    def __init__(self):
        self.running = False
        self.calls = []

    async def start_polling(self, **kwargs):
        self.calls.append(kwargs)
        self.running = True

    async def stop(self):
        self.running = False


class FakeApplication:
    def __init__(self):
        self.bot = FakeBot()
        self.updater = FakeUpdater()
        self.calls = []
        self.handlers = []
        self.error_handlers = []
        self.initialized = False
        self.started = False

    async def initialize(self):
        self.calls.append("initialize")
        self.initialized = True

    async def start(self):
        self.calls.append("start")
        self.started = True

    def add_handler(self, handler, group=0):
        self.handlers.append(handler)

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)

    async def stop(self):
        self.calls.append("stop")
        self.started = False

    async def shutdown(self):
        self.calls.append("shutdown")
        self.initialized = False


class FakeBuilder:
    instances = []

    def __init__(self):
        self.calls = []
        self.app = FakeApplication()
        FakeBuilder.instances.append(self)

    def token(self, token):
        self.calls.append(("token", token))
        return self

    def concurrent_updates(self, value):
        self.calls.append(("concurrent_updates", value))
        return self

    def request(self, value):
        self.calls.append(("request", value))
        return self

    def get_updates_request(self, value):
        self.calls.append(("get_updates_request", value))
        return self

    def base_url(self, value):
        self.calls.append(("base_url", value))
        return self

    def base_file_url(self, value):
        self.calls.append(("base_file_url", value))
        return self

    def build(self):
        self.calls.append(("build", None))
        return self.app


class FakeRequest:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeRequest.instances.append(self)


class FakeAdapter:
    def __init__(self):
        self.states = []

    async def _runtime_state_changed(self, runtime):
        self.states.append(
            (
                runtime.bot_id,
                runtime.state.state,
                runtime.state.error_code,
                runtime.state.error,
            )
        )

    async def _handle_callback(self, runtime, update, context):
        return None


class StartupWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeBuilder.instances.clear()
        FakeRequest.instances.clear()
        self.token = "123456:" + "a" * 35
        self.adapter = FakeAdapter()
        self.rt = bot_runtime(self.adapter, bot_config())

    def patches(self):
        return (
            patch.object(runtime_mod, "resolve_token", return_value=self.token),
            patch.object(self.rt, "_acquire_lock", return_value=(True, None)),
            patch.object(self.rt, "_release_lock", return_value=None),
            patch("telegram.ext.ApplicationBuilder", FakeBuilder),
            patch("telegram.request.HTTPXRequest", FakeRequest),
        )

    async def test_first_start_wires_ptb_and_drops_pending(self):
        p1, p2, p3, p4, p5 = self.patches()
        with p1, p2, p3, p4, p5:
            await self.rt._start_once()
        self.assertEqual(self.rt.state.state, "connected")
        self.assertEqual(self.rt.state.username, "startup_bot")
        self.assertEqual(self.rt.state.bot_user_id, 4242)
        self.assertTrue(self.rt._ever_started)

        builder = FakeBuilder.instances[-1]
        names = [name for name, _ in builder.calls]
        self.assertIn("token", names)
        self.assertIn("concurrent_updates", names)
        self.assertIn("request", names)
        self.assertIn("get_updates_request", names)
        self.assertEqual(len(FakeRequest.instances), 2)

        app = builder.app
        self.assertIn("initialize", app.calls)
        self.assertIn("start", app.calls)
        self.assertEqual(len(app.handlers), 2)  # message + callback
        self.assertEqual(len(app.error_handlers), 1)
        webhook = [c for c in app.bot.calls if c[0] == "delete_webhook"][-1][1]
        self.assertTrue(webhook["drop_pending_updates"])
        polling = app.updater.calls[-1]
        self.assertTrue(polling["drop_pending_updates"])
        self.assertEqual(polling["allowed_updates"], ["message", "callback_query"])

        await self.rt._teardown()

    async def test_reconnect_preserves_pending_updates(self):
        self.rt._ever_started = True
        p1, p2, p3, p4, p5 = self.patches()
        with p1, p2, p3, p4, p5:
            await self.rt._start_once()
        app = FakeBuilder.instances[-1].app
        webhook = [c for c in app.bot.calls if c[0] == "delete_webhook"][-1][1]
        self.assertFalse(webhook["drop_pending_updates"])
        polling = app.updater.calls[-1]
        self.assertFalse(polling["drop_pending_updates"])
        await self.rt._teardown()

    async def test_base_urls_are_applied(self):
        cfg = bot_config(
            api_base_url="http://127.0.0.1:8081",
            file_base_url="http://127.0.0.1:8081/file",
        )
        rt = bot_runtime(self.adapter, cfg)
        p1, p2, p3, p4, p5 = (
            patch.object(runtime_mod, "resolve_token", return_value=self.token),
            patch.object(rt, "_acquire_lock", return_value=(True, None)),
            patch.object(rt, "_release_lock", return_value=None),
            patch("telegram.ext.ApplicationBuilder", FakeBuilder),
            patch("telegram.request.HTTPXRequest", FakeRequest),
        )
        with p1, p2, p3, p4, p5:
            await rt._start_once()
        calls = FakeBuilder.instances[-1].calls
        self.assertIn(("base_url", "http://127.0.0.1:8081"), calls)
        self.assertIn(("base_file_url", "http://127.0.0.1:8081/file"), calls)
        await rt._teardown()

    async def test_token_is_not_logged_on_startup_error(self):
        # A failing get_me must produce a redacted state error, not the token.
        async def fail():
            raise RuntimeError(f"bad login for {self.token}")

        rt = bot_runtime(self.adapter, bot_config())
        rt._start_once = fail
        rt.token = self.token
        await rt.start()
        await rt.wait_ready(2)
        error = self.adapter.states[-1][3]
        self.assertNotIn(self.token, error)
        await rt.stop()


if __name__ == "__main__":
    unittest.main()
