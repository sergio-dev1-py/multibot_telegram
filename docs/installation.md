# Installation

## 1. Choose the profile

The plugin belongs to one Hermes profile. That profile owns the agent, model,
memory and tool configuration. The bots are aliases for that one profile.

The `HERMES_HOME` environment variable selects the profile home. Do not
hard-code `~/.hermes` in scripts; resolve it:

```powershell
$home = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
$dest = Join-Path $home "plugins\multi-telegram"
```

## 2. Install the plugin directory

Copy the contents of this repository into `$dest` so that `plugin.yaml` sits
directly beside `__init__.py`:

```powershell
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item .\* $dest -Recurse -Force
```

The install directory name is the plugin key. Installing it as
`multi-telegram` matches the manifest name and keeps `hermes plugins enable
multi-telegram` unambiguous. If you install under a different directory name
(for example, by using this repository directory directly as
`multi_Telegram_Hermes`), use that directory name in `plugins.enabled` and in
`hermes plugins enable`.

## 3. Install Python dependencies

Preferred (catalog-CI path):

```powershell
hermes plugins validate $dest --install-deps
```

The plugin declares `python-telegram-bot[httpx,socks]==22.8` in both `pyproject.toml`
and `plugin.yaml`. At runtime, if PTB is still missing and the plugin is
enabled, the adapter's `ensure_deps_fn` asks Hermes's package manager to
install the host's `telegram` extra. The passive status probe never installs.

Verify the import and registration:

```powershell
hermes plugins doctor $dest --ci
```

A dependency warning while the plugin is not installed/enabled is expected.
Once it is installed and enabled, `hermes pm repair` reconciles the
environment, then restart Hermes.

## 4. Enable the plugin

User-installed plugins are third-party code and are opt-in:

```powershell
hermes plugins enable multi-telegram --no-allow-tool-override
```

`--no-allow-tool-override` is correct: this plugin registers no tools and must
not replace built-in tools.

If your Hermes build uses `plugins.enabled` in YAML instead of the command,
add the plugin key:

```yaml
plugins:
  enabled:
    - multi-telegram
  isolation: in_process
```

`plugins.isolation: in_process` is mandatory. Hermes refuses
`ctx.register_platform` in the isolated `host` process because platform
adapters must receive native SDK objects on the gateway event loop.

## 5. Store tokens

Create one environment variable per bot in the active profile's secret scope.
The usual file is the profile `.env`:

```dotenv
TELEGRAM_MULTI_SOPORTE_TOKEN=replace-with-botfather-token
TELEGRAM_MULTI_VENTAS_TOKEN=replace-with-another-botfather-token
TELEGRAM_MULTI_TECNICO_TOKEN=replace-with-a-third-botfather-token
```

Rules:

- one token per bot; two bots cannot share one token;
- do not reuse `TELEGRAM_BOT_TOKEN` (that is the native Telegram adapter's
  token, and two pollers on one token conflict);
- do not put tokens in `config.yaml`;
- do not paste tokens into chat, issues, screenshots or logs.

If your profile uses a secret manager, store the same variable names there.
`get_scoped_secret` reads the active profile's scope first.

## 6. Configure `config.yaml`

Add `gateway.platforms.telegram_multi`. Keep bot-specific values under
`extra:` (Hermes promotes unknown platform-block keys into `extra`, but the
explicit form is clearer):

```yaml
gateway:
  platforms:
    telegram_multi:
      enabled: true
      reply_to_mode: first
      extra:
        default_bot: soporte
        connect_wait_seconds: 18
        # Optional. Empty by default; the NovaDesk values below are fictional.
        shared_context: |
          NovaDesk is a fictional helpdesk SaaS used for this example.
          Support hours are 09:00-18:00 UTC-5.
        bots:
          - id: soporte
            display_name: "NovaDesk Support"
            token_env: TELEGRAM_MULTI_SOPORTE_TOKEN
            allowed_users: [123456789]
            session_mode: isolated
            purpose: "First-line support and FAQ."
          - id: ventas
            display_name: "NovaDesk Sales"
            token_env: TELEGRAM_MULTI_VENTAS_TOKEN
            allowed_users: [123456789]
            session_mode: shared
            shared_group: novadesk
            purpose: "Commercial qualification and plans."
          - id: tecnico
            display_name: "NovaDesk Technical"
            token_env: TELEGRAM_MULTI_TECNICO_TOKEN
            allowed_users: [123456789]
            session_mode: shared
            shared_group: novadesk
            purpose: "Technical implementation and troubleshooting."
```

Start from [`examples/config.multi-bot.yaml`](../examples/config.multi-bot.yaml).

## 7. Start and verify

```powershell
hermes gateway restart
hermes multi-telegram status
hermes multi-telegram validate --network
```

`status` reads the plugin status file and shows each bot's connection state,
username, token presence (not the value) and policy summary. `validate
--network` calls Telegram `getMe` for every enabled bot; it is a live network
action but sends no chat message.

To send a real test message:

```powershell
hermes multi-telegram test --bot soporte --chat 123456789 --text "hello from Hermes"
```

The `--chat` value is a numeric Telegram chat id (a DM user id or a negative
group/supergroup id), not a username.

## 8. Upgrade / uninstall

The plugin keeps only non-secret state in
`%HERMES_HOME%\plugin-data\multi-telegram\` (`status.json` and per-bot update
receipts). Removing the plugin does not remove the Hermes sessions it created;
use Hermes's normal session management for those.

```powershell
hermes plugins disable multi-telegram
hermes plugins remove multi-telegram
```

Removing the plugin stops all bot polling. Tokens remain in the profile's
secret store until you delete the variables yourself.
