from __future__ import annotations

import unittest

from .helper import ensure_platform, module

ident = module("session_identity")
config_mod = module("config")


class ChatIdTests(unittest.TestCase):
    def test_encode_parse_isolated(self):
        value = ident.encode_chat_id(mode="i", scope="alpha", real_chat_id=42, user_id=7)
        self.assertEqual(value, "tgmt:i:alpha:42:7")
        parsed = ident.parse_chat_id(value)
        self.assertEqual(parsed.mode, "i")
        self.assertEqual(parsed.bot_id, "alpha")
        self.assertEqual(parsed.real_chat_id, "42")
        self.assertEqual(parsed.user_id, "7")
        self.assertTrue(parsed.is_synthetic)

    def test_encode_parse_shared(self):
        value = ident.encode_chat_id(
            mode="s", scope="support", real_chat_id=-100, user_id=7
        )
        parsed = ident.parse_chat_id(value)
        self.assertEqual(parsed.shared_group, "support")
        self.assertIsNone(parsed.bot_id)
        self.assertEqual(parsed.real_chat_id, "-100")

    def test_raw_id(self):
        parsed = ident.parse_chat_id("-100123")
        self.assertFalse(parsed.is_synthetic)
        self.assertEqual(parsed.real_chat_id, "-100123")
        self.assertIsNone(parsed.bot_id)

    def test_missing_user_becomes_zero(self):
        value = ident.encode_chat_id(mode="i", scope="a", real_chat_id=42, user_id=None)
        self.assertEqual(value, "tgmt:i:a:42:0")

    def test_invalid_values(self):
        with self.assertRaises(ValueError):
            ident.encode_chat_id(mode="x", scope="a", real_chat_id=42, user_id=7)
        with self.assertRaises(ValueError):
            ident.encode_chat_id(mode="i", scope="bad scope", real_chat_id=42, user_id=7)
        with self.assertRaises(ValueError):
            ident.encode_chat_id(mode="i", scope="a", real_chat_id="abc", user_id=7)

    def test_logical_chat_id_per_mode(self):
        isolated = config_mod.BotConfig("a", "A", "TELEGRAM_MULTI_A_TOKEN")
        shared = config_mod.BotConfig(
            "b",
            "B",
            "TELEGRAM_MULTI_B_TOKEN",
            session_mode="shared",
            shared_group="team",
        )
        self.assertEqual(ident.logical_chat_id(isolated, 42, 7), "tgmt:i:a:42:7")
        self.assertEqual(ident.logical_chat_id(shared, 42, 7), "tgmt:s:team:42:7")

    def test_message_id_roundtrip(self):
        value = ident.message_id_for("alpha", 55)
        self.assertEqual(value, "alpha:55")
        self.assertEqual(ident.parse_message_id(value), ("alpha", "55"))
        self.assertEqual(ident.parse_message_id("55"), (None, "55"))
        self.assertEqual(ident.parse_message_id("Bad:55"), (None, "Bad:55"))


class ResolveBotTests(unittest.TestCase):
    def test_chat_id_wins(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:i:alpha:42:7",
                reply_to="beta:1",
                metadata={"multi_telegram_bot_id": "gamma"},
                known_bot_ids={"alpha", "beta", "gamma"},
            ),
            "alpha",
        )

    def test_reply_id_second(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:s:team:42:7",
                reply_to="beta:1",
                metadata={"multi_telegram_bot_id": "gamma"},
                known_bot_ids={"alpha", "beta", "gamma"},
            ),
            "beta",
        )

    def test_metadata_third(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:s:team:42:7",
                metadata={"multi_telegram_bot_id": "gamma"},
                known_bot_ids={"alpha", "beta", "gamma"},
            ),
            "gamma",
        )

    def test_context_fourth(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:s:team:42:7",
                context_bot_id="beta",
                known_bot_ids={"alpha", "beta"},
            ),
            "beta",
        )

    def test_shared_route_after_context(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:s:team:42:7",
                shared_route_bot_id="beta",
                known_bot_ids={"alpha", "beta"},
            ),
            "beta",
        )

    def test_shared_route_not_used_for_isolated_chat(self):
        self.assertEqual(
            ident.resolve_bot_id(
                chat_id="tgmt:i:alpha:42:7",
                shared_route_bot_id="beta",
                known_bot_ids={"alpha", "beta"},
            ),
            "alpha",
        )

    def test_unknown_bot_is_not_used(self):
        self.assertIsNone(
            ident.resolve_bot_id(
                chat_id="tgmt:i:removed:42:7",
                known_bot_ids={"alpha"},
            )
        )

    def test_fail_closed(self):
        self.assertIsNone(ident.resolve_bot_id(chat_id="tgmt:s:team:42:7"))

    def test_source_bot_id_from_message_prefix(self):
        class Source:
            _mt_bot_id = None
            message_id = "beta:77"
            chat_id = "tgmt:s:team:42:7"

        self.assertEqual(ident.source_bot_id(Source()), "beta")


class SessionKeyTests(unittest.TestCase):
    def setUp(self):
        ensure_platform()

    def source(self, platform, chat_id, *, user="7", thread=None, message_id=None):
        from gateway.session import SessionSource

        return SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type="group",
            user_id=user,
            thread_id=thread,
            message_id=message_id,
        )

    def test_shared_bots_get_the_same_session_key(self):
        from gateway.config import Platform
        from gateway.session import build_session_key

        platform = Platform("telegram_multi")
        b = config_mod.BotConfig(
            "b", "B", "TELEGRAM_MULTI_B_TOKEN", session_mode="shared", shared_group="team"
        )
        c = config_mod.BotConfig(
            "c", "C", "TELEGRAM_MULTI_C_TOKEN", session_mode="shared", shared_group="team"
        )
        sb = self.source(platform, ident.logical_chat_id(b, -100, 7), message_id="b:1")
        sc = self.source(platform, ident.logical_chat_id(c, -100, 7), message_id="c:2")
        self.assertEqual(build_session_key(sb), build_session_key(sc))

    def test_isolated_bots_get_different_session_keys(self):
        from gateway.config import Platform
        from gateway.session import build_session_key

        platform = Platform("telegram_multi")
        a = config_mod.BotConfig("a", "A", "TELEGRAM_MULTI_A_TOKEN")
        b = config_mod.BotConfig("b", "B", "TELEGRAM_MULTI_B_TOKEN")
        sa = self.source(platform, ident.logical_chat_id(a, -100, 7))
        sb = self.source(platform, ident.logical_chat_id(b, -100, 7))
        self.assertNotEqual(build_session_key(sa), build_session_key(sb))

    def test_shared_users_remain_isolated_even_without_per_user_setting(self):
        from gateway.config import Platform
        from gateway.session import build_session_key

        platform = Platform("telegram_multi")
        b = config_mod.BotConfig(
            "b", "B", "TELEGRAM_MULTI_B_TOKEN", session_mode="shared", shared_group="team"
        )
        s7 = self.source(platform, ident.logical_chat_id(b, -100, 7), user="7")
        s8 = self.source(platform, ident.logical_chat_id(b, -100, 8), user="8")
        self.assertNotEqual(
            build_session_key(s7, group_sessions_per_user=False),
            build_session_key(s8, group_sessions_per_user=False),
        )

    def test_topics_remain_isolated(self):
        from gateway.config import Platform
        from gateway.session import build_session_key

        platform = Platform("telegram_multi")
        b = config_mod.BotConfig(
            "b", "B", "TELEGRAM_MULTI_B_TOKEN", session_mode="shared", shared_group="team"
        )
        chat = ident.logical_chat_id(b, -100, 7)
        self.assertNotEqual(
            build_session_key(self.source(platform, chat, thread="1")),
            build_session_key(self.source(platform, chat, thread="2")),
        )

    def test_restored_source_still_names_its_bot(self):
        from gateway.config import Platform
        from gateway.session import SessionSource

        platform = Platform("telegram_multi")
        source = SessionSource(
            platform=platform,
            chat_id="tgmt:s:team:-100:7",
            chat_type="group",
            user_id="7",
            message_id="beta:77",
        )
        restored = SessionSource.from_dict(source.to_dict())
        self.assertEqual(ident.source_bot_id(restored), "beta")


if __name__ == "__main__":
    unittest.main()
