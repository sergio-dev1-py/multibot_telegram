from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from .helper import bot_config, module

runtime_mod = module("bot_runtime")
bot_runtime = runtime_mod.BotRuntime


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


class RuntimeClassificationTests(unittest.TestCase):
    def test_fatal_classes(self):
        from telegram.error import Conflict, Forbidden, InvalidToken

        self.assertEqual(
            bot_runtime._classify_fatal(InvalidToken("bad")), "invalid_token"
        )
        self.assertEqual(
            bot_runtime._classify_fatal(Forbidden("no")), "unauthorized"
        )
        self.assertEqual(bot_runtime._classify_fatal(Conflict("409")), "conflict")
        self.assertEqual(
            bot_runtime._classify_fatal(RuntimeError("invalid token shape")),
            "invalid_token_format",
        )
        self.assertEqual(
            bot_runtime._classify_fatal(
                RuntimeError("TELEGRAM_MULTI_A_TOKEN is not set")
            ),
            "missing_token",
        )
        self.assertEqual(
            bot_runtime._classify_fatal(
                RuntimeError("another Hermes process/profile is using this Telegram token")
            ),
            "locked",
        )
        self.assertIsNone(bot_runtime._classify_fatal(RuntimeError("anything else")))

    def test_network_classes(self):
        from telegram.error import NetworkError, TimedOut

        self.assertTrue(bot_runtime._is_network(NetworkError("down")))
        self.assertTrue(bot_runtime._is_network(TimedOut("slow")))
        self.assertFalse(bot_runtime._is_network(RuntimeError("down")))

    def test_ptb_log_filter_redacts_token(self):
        import logging

        token = "123456:" + "a" * 35
        record = logging.LogRecord(
            "telegram",
            logging.INFO,
            __file__,
            1,
            "request https://api.telegram.org/bot%s/getMe",
            (token,),
            None,
        )
        record.getMessage()
        runtime_mod._TokenRedactingFilter().filter(record)
        rendered = record.getMessage()
        self.assertNotIn(token, rendered)
        self.assertNotIn("123456:", rendered)
        self.assertIn("<redacted-token>", rendered)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_token_is_permanent(self):
        from telegram.error import InvalidToken

        adapter = FakeAdapter()
        rt = bot_runtime(adapter, bot_config())

        async def fail():
            raise InvalidToken("bad token")

        rt._start_once = fail
        await rt.start()
        state = await rt.wait_ready(2)
        self.assertEqual(state, "failed")
        self.assertFalse(rt.running)
        self.assertEqual(adapter.states[-1][1], "failed")
        self.assertEqual(adapter.states[-1][2], "invalid_token")
        await rt.stop()

    async def test_missing_token_is_permanent(self):
        adapter = FakeAdapter()
        rt = bot_runtime(adapter, bot_config())
        with patch.object(runtime_mod, "resolve_token", return_value=""):
            await rt.start()
            state = await rt.wait_ready(2)
            self.assertEqual(state, "failed")
            self.assertEqual(adapter.states[-1][2], "missing_token")
            self.assertIn("is not set", adapter.states[-1][3] or "")
            await rt.stop()

    async def test_network_error_retries_and_stops_promptly(self):
        from telegram.error import NetworkError

        adapter = FakeAdapter()
        rt = bot_runtime(adapter, bot_config())

        async def fail():
            raise NetworkError("network down")

        rt._start_once = fail
        await rt.start()
        state = await rt.wait_ready(2)
        self.assertEqual(state, "retrying")
        self.assertTrue(rt.running)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await rt.stop()
        self.assertLess(loop.time() - started, 2.0)

    async def test_error_text_is_redacted_in_state(self):
        from telegram.error import NetworkError

        adapter = FakeAdapter()
        rt = bot_runtime(adapter, bot_config())
        token = "123456:" + "a" * 35

        async def fail():
            raise NetworkError(f"could not connect with {token}")

        rt._start_once = fail
        rt.token = token
        await rt.start()
        await rt.wait_ready(2)
        self.assertEqual(adapter.states[-1][1], "retrying")
        error = adapter.states[-1][3] or ""
        self.assertNotIn(token, error)
        self.assertNotIn("123456:", error)
        self.assertIn("<redacted-token>", error)
        await rt.stop()

    async def test_stop_before_start_is_safe(self):
        adapter = FakeAdapter()
        rt = bot_runtime(adapter, bot_config())
        await rt.stop()
        self.assertFalse(rt.running)


if __name__ == "__main__":
    unittest.main()
