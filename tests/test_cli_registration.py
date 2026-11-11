from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .helper import module

cli = module("cli")
registration = module("registration")
config_mod = module("config")

TOKEN = "123456:" + "a" * 35

EXTRA = {
    "bots": [
        {
            "id": "alpha",
            "display_name": "Alpha",
            "token_env": "TELEGRAM_MULTI_ALPHA_TOKEN",
            "allowed_users": [7],
        }
    ]
}


def _pconfig(extra):
    return SimpleNamespace(extra=extra)


class CliStatusTests(unittest.TestCase):
    def test_status_json_never_prints_token(self):
        status = {
            "state": "connected",
            "pid": 123,
            "bots": {
                "alpha": {
                    "state": "connected",
                    "username": "alpha_bot",
                    "updated_at": "now",
                }
            },
        }
        args = SimpleNamespace(json=True, multi_telegram_command="status")
        out = io.StringIO()
        with patch.object(cli, "load_platform_config", return_value=(None, _pconfig(EXTRA))), patch.object(
            cli, "resolve_token", return_value=TOKEN
        ), patch.object(cli, "_read_status", return_value=status), contextlib.redirect_stdout(out):
            code = cli.dispatch(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["bots"][0]["token_present"])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn("123456:", out.getvalue())
        self.assertIn("TELEGRAM_MULTI_ALPHA_TOKEN", out.getvalue())

    def test_status_text_hides_token(self):
        args = SimpleNamespace(json=False, multi_telegram_command="status")
        out = io.StringIO()
        with patch.object(cli, "load_platform_config", return_value=(None, _pconfig(EXTRA))), patch.object(
            cli, "resolve_token", return_value=TOKEN
        ), patch.object(cli, "_read_status", return_value={}), contextlib.redirect_stdout(out):
            code = cli.dispatch(args)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("123456:", text)
        self.assertIn("token=set", text)

    def test_status_invalid_config_is_nonzero_and_safe(self):
        args = SimpleNamespace(json=True, multi_telegram_command="status")
        out = io.StringIO()
        with patch.object(
            cli, "load_platform_config", side_effect=config_mod.ConfigError("bad config")
        ), contextlib.redirect_stdout(out):
            code = cli.dispatch(args)
        self.assertEqual(code, 1)
        self.assertIn("bad config", out.getvalue())


class CliValidateTests(unittest.TestCase):
    def test_validate_no_tokens_ok(self):
        args = SimpleNamespace(
            json=True, multi_telegram_command="validate", no_tokens=True, network=False
        )
        out = io.StringIO()
        with patch.object(cli, "load_platform_config", return_value=(None, _pconfig(EXTRA))), patch.object(
            cli, "resolve_token", return_value=TOKEN
        ), patch.object(
            config_mod, "resolve_token", return_value=TOKEN
        ), contextlib.redirect_stdout(out):
            code = cli.dispatch(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertNotIn(TOKEN, out.getvalue())

    def test_validate_missing_token_fails(self):
        args = SimpleNamespace(
            json=True, multi_telegram_command="validate", no_tokens=False, network=False
        )
        out = io.StringIO()
        with patch.object(cli, "load_platform_config", return_value=(None, _pconfig(EXTRA))), patch.object(
            cli, "resolve_token", return_value=""
        ), patch.object(
            config_mod, "resolve_token", return_value=""
        ), contextlib.redirect_stdout(out):
            code = cli.dispatch(args)
        self.assertEqual(code, 1)
        self.assertIn("is not set", out.getvalue())


class RegistrationTests(unittest.TestCase):
    def test_register_records_platform_and_cli(self):
        class Ctx:
            def __init__(self):
                self.calls = []

            def register_platform(self, **kwargs):
                self.calls.append(("platform", kwargs))

            def register_cli_command(self, **kwargs):
                self.calls.append(("cli", kwargs))

        ctx = Ctx()
        registration.register(ctx)
        kinds = [call[0] for call in ctx.calls]
        self.assertIn("platform", kinds)
        self.assertIn("cli", kinds)
        platform_kwargs = next(call[1] for call in ctx.calls if call[0] == "platform")
        self.assertEqual(platform_kwargs["name"], "telegram_multi")
        self.assertEqual(platform_kwargs["label"], "Telegram (multi-bot)")
        self.assertTrue(callable(platform_kwargs["adapter_factory"]))
        self.assertTrue(callable(platform_kwargs["check_fn"]))
        self.assertTrue(callable(platform_kwargs["is_connected"]))
        self.assertEqual(
            platform_kwargs["allowed_users_env"], "TELEGRAM_MULTI_ALLOWED_USERS"
        )
        cli_kwargs = next(call[1] for call in ctx.calls if call[0] == "cli")
        self.assertEqual(cli_kwargs["name"], "multi-telegram")

    def test_passive_dependency_probe(self):
        self.assertIsInstance(registration.check_requirements(), bool)

    def test_is_connected_needs_a_token(self):
        with patch.object(registration, "resolve_token", return_value=""):
            self.assertFalse(registration.is_connected(_pconfig(EXTRA)))
        with patch.object(registration, "resolve_token", return_value=TOKEN):
            self.assertTrue(registration.is_connected(_pconfig(EXTRA)))
        with patch.object(registration, "resolve_token", return_value=TOKEN):
            self.assertFalse(registration.is_connected(_pconfig({"bots": []})))


if __name__ == "__main__":
    unittest.main()
