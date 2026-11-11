from __future__ import annotations

import unittest
from unittest.mock import patch

from .helper import module

config_mod = module("config")

TOKEN_A = "111111:" + "a" * 35
TOKEN_B = "222222:" + "b" * 35


class ConfigParsingTests(unittest.TestCase):
    def base(self, **overrides):
        bot = {
            "id": "alpha",
            "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
            "allowed_users": [7],
            "display_name": "Alpha",
        }
        bot.update(overrides)
        return {"bots": [bot]}

    def test_minimal_config(self):
        cfg = config_mod.parse_config(self.base())
        self.assertEqual(len(cfg.bots), 1)
        bot = cfg.bots[0]
        self.assertEqual(bot.id, "alpha")
        self.assertEqual(bot.allowed_users, ("7",))
        self.assertEqual(bot.session_mode, "isolated")
        self.assertEqual(cfg.dm_policy, "allowlist")
        self.assertEqual(cfg.group_policy, "allowlist")

    def test_shared_mode_requires_group(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.parse_config(self.base(session_mode="shared"))
        cfg = config_mod.parse_config(
            self.base(session_mode="shared", shared_group="support")
        )
        self.assertEqual(cfg.bots[0].policy_scope, "support")

    def test_shared_context_is_optional_and_generic(self):
        cfg = config_mod.parse_config(self.base())
        self.assertEqual(cfg.shared_context, "")

        cfg = config_mod.parse_config(
            {
                **self.base(),
                "shared_context": "  Project Atlas: internal documentation.  ",
            }
        )
        self.assertEqual(
            cfg.shared_context, "Project Atlas: internal documentation."
        )

    def test_duplicate_bot_ids(self):
        extra = {
            "bots": [
                {"id": "a", "token_env": "TELEGRAM_MULTI_A_TOKEN", "allow_all_users": True},
                {"id": "a", "token_env": "TELEGRAM_MULTI_B_TOKEN", "allow_all_users": True},
            ]
        }
        with self.assertRaisesRegex(config_mod.ConfigError, "duplicate bot id"):
            config_mod.parse_config(extra)

    def test_duplicate_token_env(self):
        extra = {
            "bots": [
                {"id": "a", "token_env": "TELEGRAM_MULTI_A_TOKEN", "allow_all_users": True},
                {"id": "b", "token_env": "TELEGRAM_MULTI_A_TOKEN", "allow_all_users": True},
            ]
        }
        with self.assertRaisesRegex(config_mod.ConfigError, "same token_env"):
            config_mod.parse_config(extra)

    def test_native_telegram_token_env_refused(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.parse_config(
                {
                    "bots": [
                        {
                            "id": "a",
                            "token_env": "TELEGRAM_BOT_TOKEN",
                            "allow_all_users": True,
                        }
                    ]
                }
            )

    def test_invalid_slug(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.parse_config(
                {"bots": [{"id": "Bad Slug", "token_env": "TELEGRAM_MULTI_A_TOKEN"}]}
            )

    def test_numeric_allowlists_only(self):
        with self.assertRaisesRegex(config_mod.ConfigError, "numeric Telegram IDs"):
            config_mod.parse_config(self.base(allowed_users=["@alice"]))
        with self.assertRaisesRegex(config_mod.ConfigError, "numeric Telegram IDs"):
            config_mod.parse_config(self.base(allowed_chats=["-100abc"]))

    def test_no_allowlist_warns_but_parses(self):
        cfg = config_mod.parse_config(
            {
                "bots": [
                    {
                        "id": "a",
                        "token_env": "TELEGRAM_MULTI_A_TOKEN",
                        "allowed_users": [],
                    }
                ]
            }
        )
        self.assertFalse(
            module("authz")
            .BotAuthorizer(cfg.bots[0])
            .authorize(chat_id="1", chat_type="dm", user_id="7")
            .allowed
        )
        self.assertTrue(any("all inbound messages will be denied" in w for w in cfg.warnings))

    def test_public_dict_never_contains_token_values(self):
        with patch.object(config_mod, "resolve_token", return_value=TOKEN_A):
            cfg, problems = config_mod.validate_bot_config(
                self.base(), require_tokens=True, check_shape=True
            )
        self.assertEqual(problems, [])
        blob = repr(cfg.public_dict())
        self.assertNotIn(TOKEN_A, blob)
        self.assertNotIn("111111:", blob)
        self.assertIn("TELEGRAM_MULTI_ALPHA_TOKEN", blob)

    def test_token_shape(self):
        self.assertTrue(config_mod.token_shape_ok(TOKEN_A))
        self.assertFalse(config_mod.token_shape_ok("not-a-token"))
        self.assertFalse(config_mod.token_shape_ok("111111:short"))

    def test_duplicate_token_values_reported(self):
        bots = [
            {
                "id": "a",
                "token_env": "TELEGRAM_MULTI_A_TOKEN",
                "allow_all_users": True,
            },
            {
                "id": "b",
                "token_env": "TELEGRAM_MULTI_B_TOKEN",
                "allow_all_users": True,
            },
        ]
        with patch.object(config_mod, "resolve_token", return_value=TOKEN_A):
            _cfg, problems = config_mod.validate_bot_config(
                {"bots": bots}, require_tokens=True, check_shape=True
            )
        self.assertTrue(any("identical" in problem for problem in problems), problems)

    def test_missing_token_reported_without_value(self):
        with patch.object(config_mod, "resolve_token", return_value=""):
            _cfg, problems = config_mod.validate_bot_config(
                self.base(), require_tokens=True, check_shape=True
            )
        self.assertTrue(any("is not set" in problem for problem in problems))
        self.assertFalse(any("111111:" in problem for problem in problems))


class RedactionTests(unittest.TestCase):
    def test_redacts_whole_token(self):
        text = f"request failed for bot {TOKEN_A} while polling"
        redacted = config_mod.redact_text(text, TOKEN_A)
        self.assertNotIn(TOKEN_A, redacted)
        self.assertNotIn("111111:", redacted)
        self.assertIn("<redacted-token>", redacted)

    def test_redacts_token_not_previously_known(self):
        text = f"https://api.telegram.org/bot{TOKEN_B}/getMe failed"
        redacted = config_mod.redact_text(text)
        self.assertNotIn(TOKEN_B, redacted)
        self.assertNotIn("222222:", redacted)
        self.assertIn("<redacted-token>", redacted)

    def test_does_not_destroy_timestamps(self):
        text = "2026-11-08 12:34:56 connected"
        self.assertEqual(config_mod.redact_text(text), text)

    def test_redact_error(self):
        exc = RuntimeError(f"bad token {TOKEN_A}")
        value = config_mod.redact_error(exc, TOKEN_A)
        self.assertNotIn(TOKEN_A, value)
        self.assertIn("RuntimeError", value)


if __name__ == "__main__":
    unittest.main()
