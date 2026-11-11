from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .helper import (
    FakeRuntime,
    install_capture,
    make_adapter,
    make_message,
    make_update,
    module,
)

adapter_mod = module("adapter")
ident = module("session_identity")

EXTRA = {
    "bots": [
        {
            "id": "alpha",
            "display_name": "Alpha",
            "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
            "allowed_users": [7],
        },
        {
            "id": "beta",
            "display_name": "Beta",
            "token_env": "TELEGRAM_MULTI_BETA_TOKEN",
            "allowed_users": [7],
            "session_mode": "shared",
            "shared_group": "team",
            "purpose": "Answer billing questions.",
            "instructions": "Be concise.",
            "skills": ["billing"],
            "toolsets": ["terminal"],
        },
        {
            "id": "gamma",
            "display_name": "Gamma",
            "token_env": "TELEGRAM_MULTI_GAMMA_TOKEN",
            "allowed_users": [7],
            "session_mode": "shared",
            "shared_group": "team",
        },
    ]
}


def build(tmp):
    adapter = make_adapter(tmp, EXTRA)
    runtimes = {
        "alpha": FakeRuntime("alpha", username="alpha_bot", bot_user_id=1001),
        "beta": FakeRuntime("beta", username="beta_bot", bot_user_id=1002),
        "gamma": FakeRuntime("gamma", username="gamma_bot", bot_user_id=1003),
    }
    adapter._runtimes = runtimes
    return adapter, runtimes


class InboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.adapter, self.runtimes = build(self.tmp)
        self.events = install_capture(self.adapter)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_dm_isolated_chat_id(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"], make_update(message=make_message()), None
        )
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.source.chat_id, "tgmt:i:alpha:42:7")
        self.assertEqual(event.source.message_id, "alpha:10")
        self.assertEqual(event.message_id, "alpha:10")
        self.assertEqual(event.user_id, "7")
        self.assertEqual(event.source.scope_id, "42")
        # Hermes 0.23's native admission rejects any adapter metadata key
        # outside its closed WhatsApp contract. The plugin must not stamp
        # custom metadata on inbound events.
        self.assertEqual(event.metadata, {})
        from gateway.session_envelope import _native_metadata

        self.assertEqual(_native_metadata(event), {})
        # Hermes's outer gate reads the derived platform-wide union; the
        # adapter still enforces the exact per-bot list in _handle_update.
        self.assertEqual(self.adapter.config.extra["allowed_users"], ["7"])

    async def test_gateway_allowlists_are_derived_from_all_bots(self):
        extra = {
            "bots": [
                {
                    "id": "alpha",
                    "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
                    "allowed_users": [7],
                },
                {
                    "id": "beta",
                    "token_env": "TELEGRAM_MULTI_BETA_TOKEN",
                    "allowed_chats": [-100123],
                },
                {
                    "id": "gamma",
                    "token_env": "TELEGRAM_MULTI_GAMMA_TOKEN",
                    "allow_all_users": True,
                },
            ]
        }
        adapter = make_adapter(self.tmp, extra)
        self.assertEqual(adapter.config.extra["allowed_users"], ["*", "7"])
        self.assertEqual(adapter.config.extra["group_allowed_chats"], ["-100123"])

    async def test_real_ptb_message_shape(self):
        """Feed a real PTB Update object, not only SimpleNamespace fakes."""
        import time

        from telegram import Update

        update = Update.de_json(
            {
                "update_id": 501,
                "message": {
                    "message_id": 11,
                    "date": int(time.time()),
                    "chat": {"id": 42, "type": "private", "first_name": "Alice"},
                    "from": {
                        "id": 7,
                        "is_bot": False,
                        "first_name": "Alice",
                        "username": "alice",
                    },
                    "text": "hello from PTB",
                },
            },
            None,
        )
        await self.adapter._handle_update(
            self.runtimes["alpha"], update, None
        )
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.text, "hello from PTB")
        self.assertEqual(event.source.chat_id, "tgmt:i:alpha:42:7")
        self.assertEqual(event.source.chat_type, "dm")

    async def test_bot_sender_can_pass_and_is_marked_in_source(self):
        adapter = make_adapter(
            self.tmp,
            {
                "bots": [
                    {
                        "id": "alpha",
                        "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
                        "allowed_users": [7],
                        "allow_bots": True,
                    }
                ]
            },
        )
        adapter._runtimes = {"alpha": FakeRuntime("alpha")}
        install_capture(adapter)
        message = make_message()
        message.from_user.is_bot = True
        await adapter._handle_update(
            adapter._runtimes["alpha"], make_update(message=message), None
        )
        self.assertEqual(len(adapter.events), 1)
        self.assertTrue(adapter.events[0].source.is_bot)

    async def test_shared_context_is_injected_into_every_bot_prompt(self):
        extra = dict(EXTRA)
        extra["shared_context"] = "Example deployment brief: plans Alpha, Beta and Gamma."
        adapter = make_adapter(self.tmp, extra)
        adapter._runtimes = {"alpha": FakeRuntime("alpha", username="alpha_bot")}
        events = install_capture(adapter)
        await adapter._handle_update(
            adapter._runtimes["alpha"],
            make_update(message=make_message()),
            None,
        )
        self.assertEqual(len(events), 1)
        prompt = events[0].channel_prompt
        self.assertIn("Example deployment brief", prompt)
        self.assertIn("communication identity", prompt)

    async def test_shared_context_is_absent_by_default(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"], make_update(message=make_message()), None
        )
        self.assertNotIn("Shared context for every bot", self.events[0].channel_prompt)

    async def test_shared_bots_get_identical_source_chat_id(self):
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=make_message()), None
        )
        await self.adapter._handle_update(
            self.runtimes["gamma"], make_update(message=make_message()), None
        )
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[0].source.chat_id, self.events[1].source.chat_id)
        self.assertEqual(self.events[0].source.chat_id, "tgmt:s:team:42:7")
        self.assertNotEqual(self.events[0].message_id, self.events[1].message_id)

    async def test_unauthorized_user_is_dropped_before_agent(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"],
            make_update(message=make_message(user_id=99)),
            None,
        )
        self.assertEqual(self.events, [])

    async def test_no_allowlist_denies(self):
        adapter = make_adapter(
            self.tmp,
            {
                "bots": [
                    {
                        "id": "solo",
                        "token_env": "TELEGRAM_MULTI_SOLO_TOKEN",
                    }
                ]
            },
        )
        adapter._runtimes = {"solo": FakeRuntime("solo")}
        adapter.events = []
        install_capture(adapter)
        await adapter._handle_update(
            adapter._runtimes["solo"], make_update(message=make_message()), None
        )
        self.assertEqual(adapter.events, [])

    async def test_group_requires_mention(self):
        message = make_message(chat_id=-100, chat_type="group", text="hello")
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=message), None
        )
        self.assertEqual(self.events, [])

        message = make_message(chat_id=-100, chat_type="group", text="hello @beta_bot")
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=message, update_id=2), None
        )
        self.assertEqual(len(self.events), 1)

    async def test_group_reply_to_bot_counts_as_addressed(self):
        reply = SimpleNamespace(
            message_id=9,
            from_user=SimpleNamespace(id=1002),
            text="previous",
            caption=None,
        )
        message = make_message(
            chat_id=-100, chat_type="group", text="follow up", reply_to=reply
        )
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=message), None
        )
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0].reply_to_is_own_message)

    async def test_group_require_mention_false_forwards_all(self):
        adapter = make_adapter(
            self.tmp,
            {
                "bots": [
                    {
                        "id": "alpha",
                        "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
                        "allowed_users": [7],
                        "group_require_mention": False,
                    }
                ]
            },
        )
        adapter._runtimes = {"alpha": FakeRuntime("alpha")}
        install_capture(adapter)
        await adapter._handle_update(
            adapter._runtimes["alpha"],
            make_update(
                message=make_message(chat_id=-100, chat_type="group", text="hello")
            ),
            None,
        )
        self.assertEqual(len(adapter.events), 1)

    async def test_command_start_sends_welcome_without_agent_turn(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"],
            make_update(message=make_message(text="/start")),
            None,
        )
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.runtimes["alpha"].sent), 1)
        self.assertIn("Alpha", self.runtimes["alpha"].sent[0]["content"])

    async def test_start_addressed_to_another_bot_is_ignored(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"],
            make_update(message=make_message(text="/start@other_bot")),
            None,
        )
        self.assertEqual(self.events, [])
        self.assertEqual(self.runtimes["alpha"].sent, [])

    async def test_start_addressed_to_this_bot_sends_welcome(self):
        await self.adapter._handle_update(
            self.runtimes["alpha"],
            make_update(message=make_message(text="/start@alpha_bot")),
            None,
        )
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.runtimes["alpha"].sent), 1)

    async def test_duplicate_update_id_is_processed_once(self):
        update = make_update(message=make_message(), update_id=44)
        await self.adapter._handle_update(self.runtimes["alpha"], update, None)
        await self.adapter._handle_update(self.runtimes["alpha"], update, None)
        self.assertEqual(len(self.events), 1)

    async def test_callback_query_is_answered_with_text_fallback(self):
        class Query:
            def __init__(self):
                self.answers = []

            async def answer(self, text=None, **kwargs):
                self.answers.append(text)

        query = Query()
        await self.adapter._handle_update(
            self.runtimes["alpha"],
            make_update(callback_query=query, update_id=9),
            None,
        )
        self.assertEqual(len(query.answers), 1)
        self.assertIn("reply with text", query.answers[0])

    async def test_channel_prompt_identity_and_shared_note(self):
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=make_message()), None
        )
        prompt = self.events[0].channel_prompt
        self.assertIn("Beta", prompt)
        self.assertIn("@beta_bot", prompt)
        self.assertIn("billing", prompt)
        self.assertIn("shared-session group", prompt)
        self.assertIn("Answer billing questions.", prompt)
        self.assertIn("Be concise.", prompt)

    async def test_toolsets_and_auto_skill_per_bot(self):
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=make_message()), None
        )
        event = self.events[0]
        self.assertEqual(self.adapter.toolsets_for_source(event.source), ["terminal"])
        self.assertEqual(event.auto_skill, ["billing"])
        self.assertIsNone(self.adapter.toolsets_for_source(None))

    async def test_oversized_media_is_rejected_before_download(self):
        photo = [
            SimpleNamespace(
                file_id="f1", file_size=999 * 1024 * 1024, mime_type="image/jpeg"
            )
        ]
        message = make_message()
        message.photo = photo
        await self.adapter._handle_update(
            self.runtimes["alpha"], make_update(message=message), None
        )
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.runtimes["alpha"].sent), 1)
        self.assertIn("larger than", self.runtimes["alpha"].sent[0]["content"])

    async def test_document_is_downloaded_and_attached_to_event(self):
        document = SimpleNamespace(
            file_id="doc-1",
            file_size=5,
            file_name="notes.txt",
            mime_type="text/plain",
        )
        message = make_message(text="", user_id=7)
        message.caption = "see attached"
        message.document = document

        class Remote:
            async def download_as_bytearray(self):
                return bytearray(b"hello")

        async def get_file(file_id):
            self.assertEqual(file_id, "doc-1")
            return Remote()

        self.runtimes["alpha"].bot = SimpleNamespace(get_file=get_file)
        cached = str(self.tmp / "notes.txt")
        with patch(
            "gateway.platforms.base.cache_document_from_bytes_async",
            new=AsyncMock(return_value=cached),
        ):
            await self.adapter._handle_update(
                self.runtimes["alpha"], make_update(message=message), None
            )
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.media_urls, [cached])
        self.assertEqual(event.media_types, ["text/plain"])
        self.assertEqual(event.media_filenames, ["notes.txt"])
        self.assertIn("see attached", event.text)
        self.assertIn("cached file is attached", event.text)


class OutboundRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.adapter, self.runtimes = build(self.tmp)
        self.events = install_capture(self.adapter)
        self.shared = "tgmt:s:team:42:7"
        self.iso_alpha = "tgmt:i:alpha:42:7"

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_isolated_chat_id_routes_without_any_context(self):
        result = await self.adapter.send(self.iso_alpha, "hi")
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["alpha"].sent), 1)
        self.assertEqual(self.runtimes["beta"].sent, [])
        self.assertEqual(self.runtimes["alpha"].sent[0]["content"], "hi")

    async def test_unbound_shared_chat_fails_closed(self):
        result = await self.adapter.send(self.shared, "hi")
        self.assertFalse(result.success)
        self.assertIn("refusing to guess", result.error)
        self.assertEqual(self.runtimes["beta"].sent, [])

    async def test_shared_route_fallback_is_per_conversation(self):
        await self.adapter._handle_update(
            self.runtimes["beta"], make_update(message=make_message()), None
        )
        result = await self.adapter.send(self.shared, "after restart")
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["beta"].sent), 1)
        self.assertEqual(self.runtimes["gamma"].sent, [])

    async def test_reply_prefix_routes_to_the_sending_bot(self):
        result = await self.adapter.send(self.shared, "hi", reply_to="gamma:77")
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["gamma"].sent), 1)
        self.assertEqual(self.runtimes["beta"].sent, [])

    async def test_explicit_metadata_routes(self):
        result = await self.adapter.send(
            self.shared, "hi", metadata={"multi_telegram_bot_id": "gamma"}
        )
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["gamma"].sent), 1)

    async def test_text_debounce_never_merges_different_bots(self):
        await self.adapter._handle_update(
            self.runtimes["beta"],
            make_update(message=make_message(), update_id=1),
            None,
        )
        await self.adapter._handle_update(
            self.runtimes["gamma"],
            make_update(message=make_message(), update_id=2),
            None,
        )
        event_b, event_g = self.events
        self.assertFalse(self.adapter._can_merge_text_debounce_events(event_b, event_g))
        self.assertTrue(self.adapter._can_merge_text_debounce_events(event_b, event_b))

    async def test_cross_bot_busy_event_is_deferred_not_merged(self):
        await self.adapter._handle_update(
            self.runtimes["beta"],
            make_update(message=make_message(), update_id=1),
            None,
        )
        await self.adapter._handle_update(
            self.runtimes["gamma"],
            make_update(message=make_message(), update_id=2),
            None,
        )
        event_b, event_g = self.events
        key = self.adapter._event_session_key(event_b)
        self.assertEqual(key, self.adapter._event_session_key(event_g))
        guard = asyncio.Event()
        self.adapter._active_sessions[key] = guard
        self.adapter._active_bot_by_session[key] = "beta"
        try:
            await self.adapter._handle_message_while_active(event_g, key)
            self.assertTrue(event_g._gateway_accepted)
            self.assertEqual(self.adapter._cross_bot_deferred[key], [event_g])
            self.assertNotIn(key, self.adapter._pending_messages)
        finally:
            task = self.adapter._cross_bot_tasks.get(key)
            if task is not None:
                task.cancel()
            self.adapter._cross_bot_tasks.pop(key, None)
            self.adapter._cross_bot_deferred.pop(key, None)
            self.adapter._active_sessions.pop(key, None)
            self.adapter._active_bot_by_session.pop(key, None)

    async def test_concurrent_sends_keep_their_own_bot(self):
        from gateway.platforms.base import SendResult

        async def fake_final(
            self,
            event,
            session_key,
            text_content,
            metadata,
            *,
            reply_to,
            is_ephemeral_response=False,
        ):
            await asyncio.sleep(0)
            return (
                await self.send(
                    event.source.chat_id,
                    text_content,
                    reply_to=reply_to,
                    metadata=metadata,
                ),
                self,
            )

        await self.adapter._handle_update(
            self.runtimes["beta"],
            make_update(message=make_message(), update_id=1),
            None,
        )
        await self.adapter._handle_update(
            self.runtimes["gamma"],
            make_update(message=make_message(), update_id=2),
            None,
        )
        event_b, event_g = self.events
        with patch.object(
            adapter_mod.BasePlatformAdapter, "send_final_ledgered", fake_final
        ):
            results = await asyncio.gather(
                self.adapter.send_final_ledgered(
                    event_b, "s1", "from beta", {}, reply_to=None
                ),
                self.adapter.send_final_ledgered(
                    event_g, "s1", "from gamma", {}, reply_to=None
                ),
            )
        self.assertTrue(all(result.success for result, _ in results))
        self.assertEqual([m["content"] for m in self.runtimes["beta"].sent], ["from beta"])
        self.assertEqual([m["content"] for m in self.runtimes["gamma"].sent], ["from gamma"])

    async def test_edit_uses_prefixed_message_id(self):
        result = await self.adapter.edit_message(self.shared, "beta:55", "**new**")
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["beta"].edits), 1)
        edit = self.runtimes["beta"].edits[0]
        self.assertEqual(edit["message_id"], "55")
        self.assertIn("<b>new</b>", edit["content"])

    async def test_delete_uses_prefixed_message_id(self):
        self.assertTrue(await self.adapter.delete_message(self.shared, "gamma:66"))
        self.assertEqual(
            self.runtimes["gamma"].deleted,
            [{"chat_id": "42", "message_id": "66"}],
        )

    async def test_typing_uses_context_or_route(self):
        token = adapter_mod._BOUND_BOT_ID.set("gamma")
        try:
            await self.adapter.send_typing(self.shared, {"thread_id": "3"})
        finally:
            adapter_mod._BOUND_BOT_ID.reset(token)
        self.assertEqual(
            self.runtimes["gamma"].typing,
            [{"chat_id": "42", "thread_id": "3"}],
        )

    async def test_media_document_has_prefixed_reply(self):
        result = await self.adapter.send_document(
            self.shared,
            "C:/tmp/file.pdf",
            "a caption",
            file_name="file.pdf",
            reply_to="beta:12",
            metadata=None,
        )
        self.assertTrue(result.success)
        sent = self.runtimes["beta"].media
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "document")
        self.assertEqual(sent[0]["reply_to"], "12")
        self.assertEqual(sent[0]["file_name"], "file.pdf")

    async def test_voice_falls_back_to_audio(self):
        self.runtimes["beta"].fail_voice = True
        result = await self.adapter.send_voice(
            self.shared,
            "C:/tmp/voice.ogg",
            "hello",
            reply_to="beta:12",
        )
        self.assertTrue(result.success)
        self.assertEqual([m["kind"] for m in self.runtimes["beta"].media], ["audio"])

    async def test_url_image_is_sent_as_photo(self):
        result = await self.adapter.send_image(
            self.shared,
            "https://example.com/cat.png",
            "cat",
            reply_to="beta:12",
        )
        self.assertTrue(result.success)
        self.assertEqual(len(self.runtimes["beta"].media), 1)
        self.assertEqual(self.runtimes["beta"].media[0]["kind"], "photo")
        self.assertEqual(
            self.runtimes["beta"].media[0]["file"], "https://example.com/cat.png"
        )

    async def test_disconnected_bot_reports_retryable(self):
        self.runtimes["gamma"].connected = False
        result = await self.adapter.send(
            self.shared, "hi", metadata={"multi_telegram_bot_id": "gamma"}
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    async def test_raw_chat_uses_explicit_default_bot_only(self):
        adapter = make_adapter(
            self.tmp,
            {
                "default_bot": "alpha",
                "bots": [
                    {
                        "id": "alpha",
                        "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
                        "allowed_users": [7],
                    },
                    {
                        "id": "beta",
                        "token_env": "TELEGRAM_MULTI_BETA_TOKEN",
                        "allowed_users": [7],
                        "session_mode": "shared",
                        "shared_group": "team",
                    },
                ],
            },
        )
        rt = FakeRuntime("alpha", username="alpha_bot")
        adapter._runtimes = {"alpha": rt}
        result = await adapter.send("12345", "hi")
        self.assertTrue(result.success)
        self.assertEqual(len(rt.sent), 1)

    async def test_failure_redacts_token_and_carries_retry_after(self):
        token = "123456:" + "a" * 35
        exc = RuntimeError(f"send failed {token}")
        exc.retry_after = 2.5
        result = self.adapter._failure(exc, token)
        self.assertFalse(result.success)
        self.assertNotIn(token, result.error)
        self.assertNotIn("123456:", result.error)
        self.assertEqual(result.retry_after, 2.5)

    async def test_status_metadata_carries_bot(self):
        metadata = self.adapter.status_metadata_for_turn(
            self.shared, "beta:5", {"thread_id": "1"}
        )
        self.assertEqual(metadata["multi_telegram_bot_id"], "beta")
        self.assertEqual(metadata["thread_id"], "1")

    async def test_chat_info(self):
        info = await self.adapter.get_chat_info(self.iso_alpha)
        self.assertEqual(info["chat_id"], "42")
        self.assertEqual(info["type"], "dm")
        info = await self.adapter.get_chat_info("tgmt:s:team:-100:7")
        self.assertEqual(info["chat_id"], "-100")
        self.assertEqual(info["type"], "group")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_config_connect_fails_without_network(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            adapter = make_adapter(Path(raw), {"bots": []})
            self.assertTrue(adapter._config_error)
            self.assertFalse(await adapter.connect())
            await adapter.disconnect()

    async def test_disconnect_is_safe_without_connect(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            adapter, _runtimes = build(Path(raw))
            await adapter.disconnect()
            self.assertFalse(adapter._running)


if __name__ == "__main__":
    unittest.main()
