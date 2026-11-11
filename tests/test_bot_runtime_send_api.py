from __future__ import annotations

import unittest
from types import SimpleNamespace

from .helper import bot_config, module

runtime_mod = module("bot_runtime")
bot_runtime = runtime_mod.BotRuntime


class FakeBot:
    """Minimal PTB Bot surface used by BotRuntime's outbound methods."""

    def __init__(self):
        self.calls = []
        self.fail_html_once = False
        self.not_modified = False

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    async def send_message(self, **kwargs):
        if self.fail_html_once and kwargs.get("parse_mode") is not None:
            self.fail_html_once = False
            from telegram.error import BadRequest

            self._record("send_message_failed", kwargs)
            raise BadRequest("Can't parse entities: bad markup")
        self._record("send_message", kwargs)
        return SimpleNamespace(message_id=101)

    async def edit_message_text(self, **kwargs):
        if self.not_modified:
            from telegram.error import BadRequest

            raise BadRequest("Message is not modified")
        self._record("edit_message_text", kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def send_photo(self, **kwargs):
        self._record("send_photo", kwargs)
        return SimpleNamespace(message_id=102)

    async def send_document(self, **kwargs):
        self._record("send_document", kwargs)
        return SimpleNamespace(message_id=103)

    async def send_voice(self, **kwargs):
        self._record("send_voice", kwargs)
        return SimpleNamespace(message_id=104)

    async def send_audio(self, **kwargs):
        self._record("send_audio", kwargs)
        return SimpleNamespace(message_id=105)

    async def send_chat_action(self, **kwargs):
        self._record("send_chat_action", kwargs)

    async def delete_message(self, **kwargs):
        self._record("delete_message", kwargs)

    async def get_file(self, file_id):
        data = b"hello"

        class Remote:
            async def download_as_bytearray(self):
                return bytearray(data)

        return Remote()


class SendApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rt = bot_runtime(object(), bot_config())
        self.bot = FakeBot()
        self.rt.bot = self.bot

    async def test_send_text_uses_reply_thread_and_preview_options(self):
        message = await self.rt.send_text(
            "42", "<b>hi</b>", reply_to="7", thread_id="3", html=True
        )
        self.assertEqual(message.message_id, 101)
        name, kwargs = self.bot.calls[-1]
        self.assertEqual(name, "send_message")
        self.assertEqual(kwargs["chat_id"], 42)
        self.assertEqual(kwargs["text"], "<b>hi</b>")
        self.assertEqual(kwargs["reply_to_message_id"], 7)
        self.assertEqual(kwargs["message_thread_id"], 3)
        self.assertTrue(kwargs["link_preview_options"].is_disabled)
        self.assertIsNotNone(kwargs["parse_mode"])

    async def test_html_rejection_retries_plain(self):
        self.bot.fail_html_once = True
        await self.rt.send_text("42", "bad <b>markup", html=True)
        self.assertEqual([c[0] for c in self.bot.calls], ["send_message_failed", "send_message"])
        self.assertNotIn("parse_mode", self.bot.calls[-1][1])
        self.assertEqual(self.bot.calls[-1][1]["text"], "bad markup")

    async def test_edit_not_modified_is_success(self):
        self.bot.not_modified = True
        result = await self.rt.edit_text("42", "9", "<b>x</b>", html=True)
        self.assertIsNone(result)

    async def test_edit_uses_chat_and_message_ids(self):
        await self.rt.edit_text("42", "9", "<b>x</b>", html=True)
        name, kwargs = self.bot.calls[-1]
        self.assertEqual(name, "edit_message_text")
        self.assertEqual(kwargs["chat_id"], 42)
        self.assertEqual(kwargs["message_id"], 9)
        self.assertEqual(kwargs["text"], "<b>x</b>")

    async def test_send_media_document_passes_filename(self):
        await self.rt.send_media(
            "document",
            "42",
            file="C:/tmp/a.pdf",
            caption="cap",
            reply_to="7",
            thread_id="3",
            file_name="a.pdf",
        )
        name, kwargs = self.bot.calls[-1]
        self.assertEqual(name, "send_document")
        self.assertEqual(kwargs["document"], "C:/tmp/a.pdf")
        self.assertEqual(kwargs["filename"], "a.pdf")
        self.assertEqual(kwargs["caption"], "cap")
        self.assertEqual(kwargs["reply_to_message_id"], 7)
        self.assertEqual(kwargs["message_thread_id"], 3)

    async def test_send_voice_and_typing_and_delete(self):
        await self.rt.send_media("voice", "42", file="C:/tmp/v.ogg")
        await self.rt.send_typing("42", thread_id="3")
        deleted = await self.rt.delete("42", "9")
        self.assertTrue(deleted)
        names = [c[0] for c in self.bot.calls]
        self.assertEqual(
            names, ["send_voice", "send_chat_action", "delete_message"]
        )
        self.assertEqual(self.bot.calls[1][1]["action"], "typing")
        self.assertEqual(
            self.bot.calls[2][1], {"chat_id": 42, "message_id": 9}
        )

    async def test_delete_failure_is_swallowed(self):
        async def fail(**kwargs):
            raise RuntimeError("gone")

        self.bot.delete_message = fail
        self.assertFalse(await self.rt.delete("42", "9"))


class InboundDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_message_forwards_to_adapter(self):
        seen = []

        class Adapter:
            async def _handle_update(self, runtime, update, context):
                seen.append((runtime, update, context))

        rt = bot_runtime(Adapter(), bot_config())
        update = SimpleNamespace(update_id=1)
        context = SimpleNamespace()
        await rt._on_message(update, context)
        self.assertEqual(seen, [(rt, update, context)])

    async def test_on_message_swallows_adapter_errors(self):
        class Adapter:
            async def _handle_update(self, runtime, update, context):
                raise RuntimeError("boom")

        rt = bot_runtime(Adapter(), bot_config())
        await rt._on_message(SimpleNamespace(update_id=1), SimpleNamespace())

    async def test_on_callback_answers_stray_taps(self):
        class Adapter:
            async def _handle_callback(self, runtime, update, context):
                self.called = True

        adapter = Adapter()
        rt = bot_runtime(adapter, bot_config())
        await rt._on_callback(SimpleNamespace(), SimpleNamespace())
        self.assertTrue(adapter.called)


if __name__ == "__main__":
    unittest.main()
