# Security

## Trust model

`multi-telegram` is third-party code running in-process with Hermes. Installing
and enabling it grants the same trust as any other Hermes plugin: it can read
the profile's configuration and, through Hermes, invoke tools. Review the
source and pin a commit before installing from a catalog.

The plugin does not modify Hermes core, does not monkey-patch gateway classes,
and does not replace the native Telegram adapter. It registers one platform
adapter through the public plugin API.

## Token handling

- Tokens are read from the active profile's scoped secret store via
  `get_scoped_secret(<token_env>)`.
- Tokens are never written to `config.yaml`, `status.json`, update receipts,
  CLI output, or source code.
- Logs and errors pass through `redact_text`/`redact_error`, which replaces the
  whole `bot<digits>:<secret>` match with `<redacted-token>` and also replaces
  any exact token value the runtime knows about.
- A logging filter on the `telegram`/`httpx`/`httpcore` loggers and on the
  configured root handlers redacts token-shaped strings emitted by the SDK
  itself. Tracebacks from arbitrary external code are outside that filter; the
  plugin's own caught exceptions are redacted before they are logged.
- The status file and CLI report only the environment-variable name and a
  boolean "present" flag.
- Token shape is checked before network calls; malformed values produce an
  operator-safe error.
- One token per bot is enforced. Two bots with the same value under different
  variable names are reported by `hermes multi-telegram validate`.
- A scoped lock keyed by `telegram_multi` + token prevents two instances in
  this plugin from polling the same token. The native Telegram adapter uses a
  different lock scope, so do not configure the same token in both adapters;
  Telegram will return `409 Conflict` and one poller will stop.

A Telegram bot token grants full control of that bot (send messages, read
updates, change commands). Treat it like a password. If a token leaks, revoke
it with BotFather immediately, replace the profile secret, and restart the
gateway.

## Per-bot authorization

Authorization happens at adapter intake, before `handle_message` and before
the agent sees the text:

- DMs: `allow_all_users`, or sender in `allowed_users`, or chat in
  `allowed_chats`;
- groups: chat allowlist and user allowlist are both enforced when set;
- empty configuration denies everyone;
- bot authors are denied unless `allow_bots: true`;
- missing sender ids are denied;
- group mention policy is applied after authorization.

`allow_all_users` is intentionally loud: it logs a warning at parse time and is
visible in status. Treat that bot as public. Do not combine
`allow_all_users: true` with a shared-session group containing private context.

### Platform-wide gate caveat

Hermes's own gateway authorization is per platform, not per bot. For DMs it
sees the union of the enabled bots' allowlists. The adapter's exact per-bot
check runs first and remains authoritative. See
[configuration.md](configuration.md#platform-wide-dm-gate-caveat).

## Conversation isolation

The synthetic chat id includes the sender id even in shared mode:

```text
tgmt:s:<shared_group>:<real_chat_id>:<user_id>
```

Therefore:

- unrelated users never share a session, even if
  `group_sessions_per_user` is false;
- different chats and forum topics remain separate;
- only bots explicitly assigned to the same `shared_group` share a transcript.

Shared mode is not a privacy boundary between the bots in that group: every
bot in the group can continue the same transcript, which is the point.

## Outbound routing and fail-closed behavior

No process-global "last bot" is used. Resolution requires proof:

1. isolated synthetic chat id;
2. `<bot_id>:` reply/message id;
3. explicit metadata;
4. turn-local context;
5. shared per-conversation route (proactive sends only);
6. explicit `default_bot` for raw chat ids.

Otherwise the send fails with an error. Ambiguous shared-chat sends are not
guessed onto a random bot.

## Inbound attachments

- The adapter downloads only when the sender passed authorization.
- File size is checked against `max_attachment_mb` before download.
- Bytes are written through Hermes's cache helpers; file names are sanitized
  by those helpers.
- Download failures produce a text notice to the agent, not a crash.
- Telegram itself fetches URL-based outbound images; the plugin does not open
  arbitrary local URLs for inbound media.

## Webhooks and polling

Only long polling is implemented. Starting a bot removes any existing webhook
for that token on first start so polling can receive updates. Do not run a
separate webhook consumer for the same bot token.

## Plugin isolation

Platform adapters require `plugins.isolation: in_process`; Hermes refuses
`ctx.register_platform` in the isolated host. Do not weaken that check or run
this plugin in a pooled/multiplexed profile you do not trust.

## Operator responsibilities

- Keep the profile `.env` and secret store permissions tight.
- Use numeric Telegram ids from a trusted source.
- Review `purpose`/`instructions`; they become part of the model prompt.
- Keep `group_require_mention: true` in busy groups.
- Do not enable `allow_all_users` on a bot connected to sensitive tools.
- Monitor `hermes multi-telegram status` and the gateway log for `failed` or
  `retrying` states.
- Revoke and rotate tokens on staff departure or suspected exposure.
