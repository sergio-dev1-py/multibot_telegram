# Windows guide

The plugin is designed for a local Windows Hermes install. It does not need
WSL, Docker, a Linux path layout, or a process supervisor beyond Hermes itself.

## Paths

Resolve the profile home instead of hard-coding it:

```powershell
$home = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
$plugin = Join-Path $home "plugins\multi-telegram"
$data   = Join-Path $home "plugin-data\multi-telegram"
```

- plugin install: `$home\plugins\multi-telegram`
- profile config: `$home\config.yaml`
- profile secrets: `$home\.env` (or your configured secret backend)
- status/receipts: `$data\status.json`, `$data\update_receipts_<bot>.json`

The plugin uses `pathlib`/`os.path` and creates directories through Hermes's
`plugin_data_dir`; it contains no `/tmp`, `/home`, `fcntl`, `signal.SIGTERM`
or WSL assumptions.

## Install

```powershell
$home = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
$plugin = Join-Path $home "plugins\multi-telegram"
New-Item -ItemType Directory -Force $plugin | Out-Null
Copy-Item .\* $plugin -Recurse -Force

hermes plugins validate $plugin --install-deps
hermes plugins doctor $plugin --ci
hermes plugins enable multi-telegram --no-allow-tool-override
```

If the copied directory is not named `multi-telegram`, use that directory name
as the plugin key when enabling.

## Tokens and PowerShell

Prefer editing the profile `.env` in a text editor over typing a token in a
shell. If you must set it in a PowerShell session, avoid `Write-Host` and
avoid saving the command in history:

```powershell
# Not recommended: leaves the token in PSReadLine history.
$env:TELEGRAM_MULTI_SOPORTE_TOKEN = "..."
```

The plugin never prints token values. Telegram's own error strings can contain
the token inside a URL; `redact_text` removes the whole `bot<id>:<secret>`
match from logs and CLI errors.

## Long polling and firewalls

PTB long polling makes outbound HTTPS requests to `api.telegram.org` (or your
`api_base_url`). No inbound port is opened, so a home Windows machine behind
NAT works without port forwarding.

If you use a proxy:

```yaml
extra:
  bots:
    - id: soporte
      token_env: TELEGRAM_MULTI_SOPORTE_TOKEN
      proxy: http://127.0.0.1:7890
```

`proxy` is per bot.

## Running the gateway

```powershell
hermes gateway restart
hermes multi-telegram status
```

Status is written by the plugin to `$data\status.json`. Hermes's gateway
status may also show the platform; the plugin status file is the per-bot view.

If the gateway runs as a Windows service or scheduled task, ensure the task's
environment includes `HERMES_HOME` and the profile `.env` is readable by that
account. A service running as `SYSTEM` will not see a user's `.env`.

## Firewall / antivirus

Some endpoint security products delay Python's outbound TLS handshakes or
inspect `python.exe` traffic. Symptoms are repeated `NetworkError`/`TimedOut`
states with the bot continuing to retry. Add an allow rule for the Hermes
Python executable if your policy permits it.

## Console encoding

The plugin logs and CLI output are UTF-8 text. On older Windows consoles,
set:

```powershell
chcp 65001
```

The status file itself is UTF-8 JSON with `ensure_ascii=False`.

## Removing the plugin

```powershell
hermes plugins disable multi-telegram
hermes plugins remove multi-telegram
Remove-Item -Recurse -Force (Join-Path $home "plugin-data\multi-telegram")
```

Deleting the plugin-data directory also deletes the per-conversation proactive
route map and update receipts. Hermes sessions are separate and remain until
removed through Hermes.
