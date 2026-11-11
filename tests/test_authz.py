from __future__ import annotations

import unittest

from .helper import bot_config, module

authz = module("authz")


class BotAuthorizerTests(unittest.TestCase):
    def test_allowlisted_dm(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=("7", "8")))
        self.assertTrue(a.authorize(chat_id="7", chat_type="dm", user_id="7").allowed)
        self.assertFalse(a.authorize(chat_id="9", chat_type="dm", user_id="9").allowed)

    def test_bot_sender_denied_by_default(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=("7",)))
        decision = a.authorize(chat_id="7", chat_type="dm", user_id="7", is_bot=True)
        self.assertFalse(decision.allowed)
        self.assertIn("bot sender", decision.reason)

    def test_bot_sender_can_be_allowed(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=("7",), allow_bots=True))
        self.assertTrue(
            a.authorize(chat_id="7", chat_type="dm", user_id="7", is_bot=True).allowed
        )

    def test_allow_all(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=(), allow_all_users=True))
        self.assertTrue(a.authorize(chat_id="1", chat_type="dm", user_id="999").allowed)

    def test_group_requires_both_lists_when_both_set(self):
        a = authz.BotAuthorizer(
            bot_config(allowed_users=("7",), allowed_chats=("-100",))
        )
        self.assertTrue(
            a.authorize(chat_id="-100", chat_type="group", user_id="7").allowed
        )
        self.assertFalse(
            a.authorize(chat_id="-200", chat_type="group", user_id="7").allowed
        )
        self.assertFalse(
            a.authorize(chat_id="-100", chat_type="group", user_id="8").allowed
        )

    def test_group_user_only_list_allows_in_any_chat(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=("7",), allowed_chats=()))
        self.assertTrue(
            a.authorize(chat_id="-999", chat_type="group", user_id="7").allowed
        )
        self.assertFalse(
            a.authorize(chat_id="-999", chat_type="group", user_id="8").allowed
        )

    def test_group_chat_only_list_allows_any_member(self):
        a = authz.BotAuthorizer(bot_config(allowed_users=(), allowed_chats=("-100",)))
        self.assertTrue(
            a.authorize(chat_id="-100", chat_type="group", user_id="123456").allowed
        )

    def test_missing_user_denied(self):
        a = authz.BotAuthorizer(bot_config(allow_all_users=True))
        self.assertFalse(a.authorize(chat_id="1", chat_type="dm", user_id=None).allowed)

    def test_union_dm_policy(self):
        cfg = module("config").parse_config(
            {
                "bots": [
                    {"id": "a", "token_env": "TELEGRAM_MULTI_A_TOKEN", "allowed_users": [7]},
                    {"id": "b", "token_env": "TELEGRAM_MULTI_B_TOKEN", "allowed_users": [8]},
                ]
            }
        )
        self.assertTrue(authz.union_allows_dm(cfg, "7"))
        self.assertTrue(authz.union_allows_dm(cfg, "8"))
        self.assertFalse(authz.union_allows_dm(cfg, "9"))
        self.assertEqual(authz.allowed_user_union(cfg), {"7", "8"})

    def test_union_honours_allow_all(self):
        cfg = module("config").parse_config(
            {
                "bots": [
                    {"id": "a", "token_env": "TELEGRAM_MULTI_A_TOKEN", "allow_all_users": True}
                ]
            }
        )
        self.assertTrue(authz.union_allows_dm(cfg, "999"))

    def test_union_honours_dm_chat_allowlist(self):
        cfg = module("config").parse_config(
            {
                "bots": [
                    {
                        "id": "a",
                        "token_env": "TELEGRAM_MULTI_A_TOKEN",
                        "allowed_chats": [7],
                    }
                ]
            }
        )
        self.assertTrue(authz.union_allows_dm(cfg, "7"))
        self.assertFalse(authz.union_allows_dm(cfg, "8"))


if __name__ == "__main__":
    unittest.main()
