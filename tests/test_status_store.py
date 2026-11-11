from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .helper import module

status_mod = module("status_store")


class StatusStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_start_and_reports_bot(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            store = status_mod.StatusStore(path=path)
            await store.start()
            await store.set_bot("alpha", state="connected", username="alpha_bot", bot_user_id=1)
            await store.set_overall("connected")
            await store.flush()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["bots"]["alpha"]["state"], "connected")
            self.assertEqual(payload["state"], "connected")

    async def test_routes_survive_restart_but_bot_states_do_not(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            first = status_mod.StatusStore(path=path)
            await first.start()
            await first.set_bot("alpha", state="connected", username="alpha_bot")
            await first.set_route("tgmt:s:team:42:7", "beta")
            await first.flush()

            second = status_mod.StatusStore(path=path)
            await second.start()
            self.assertEqual(second.route("tgmt:s:team:42:7"), "beta")
            self.assertEqual(second.state["bots"], {})

    async def test_set_route_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            store = status_mod.StatusStore(path=Path(raw) / "status.json")
            await store.start()
            await store.set_route("c", "b")
            await store.set_route("c", "b")
            self.assertEqual(store.route("c"), "b")

    def test_route_returns_none_for_unknown(self):
        with tempfile.TemporaryDirectory() as raw:
            store = status_mod.StatusStore(path=Path(raw) / "status.json")
            self.assertIsNone(store.route("nope"))


class ReceiptTests(unittest.TestCase):
    def test_duplicate_update_ids_are_remembered(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "receipts.json"
            store = status_mod.UpdateReceiptStore("alpha", path=path)
            self.assertFalse(store.is_duplicate(10))
            self.assertTrue(store.is_duplicate(10))
            self.assertFalse(store.is_duplicate(11))

    def test_receipts_persist_across_instances(self):
        import asyncio

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "receipts.json"
            first = status_mod.UpdateReceiptStore("alpha", path=path)
            self.assertFalse(first.is_duplicate(10))
            asyncio.run(first.flush())

            second = status_mod.UpdateReceiptStore("alpha", path=path)
            self.assertTrue(second.is_duplicate(10))
            self.assertFalse(second.is_duplicate(11))


if __name__ == "__main__":
    unittest.main()
