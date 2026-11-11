# Troubleshooting

## The platform does not appear / config is silently ignored

- The plugin must be installed and **enabled**. User plugins are opt-in.
- If `plugins.isolation: host`, Hermes refuses `register_platform`. Set
  `plugins.isolation: in_process`.
- The gateway must discover plugins before loading `config.yaml`. Restart the
  gateway after installing/enabling; do not hot-edit a running process.
- The config block must be `gateway.platforms.telegram_multi` (or
  `platforms.telegram_multi`); a random top-level `telegram_multi:` block also
  works in some builds, but the nested form is the supported one in docs.
- If you installed the directory under another name, `plugins.enabled` must
  use that directory key, not necessarily `multi-telegram`.

## `hermes multi-telegram` is an unknown command

- The plugin is not enabled/discovered. Run `hermes plugins list --no-bundled`
  and `hermes plugins enable multi-telegram`.
- Shell completion/old process: start a new `hermes` invocation.

## Token is reported missing

- `resolve_token` reads the **active profile's** secret scope. A token in
  another profile or another user account is invisible.
- If the gateway runs as a service, its account may not see the user's
  `.env` or secret backend.
- Check the variable name exactly (`token_env`), including uppercase.
- Run `hermes multi-telegram validate`; it reports the variable name but never
  the value.

## Token looks right but the bot says `invalid_token`

- Re-copy the token from BotFather; trailing whitespace and partial copies are
  common.
- Revoked tokens fail with 401. Generate a new one.
- A token from a deleted bot cannot connect.

## `409 Conflict: terminated by other getUpdates`

Another process is polling the same bot token:

- the native Hermes Telegram adapter configured with the same token;
- another Hermes profile/gateway;
- a leftover `getUpdates` script, webhook consumer, or bot framework.

Only one poller per token is allowed. Stop the other consumer or use a
different bot. The plugin's scoped lock catches another instance of this
plugin, but it cannot see a different program's poller.

## One bot is `failed`, the others are `connected`

This is by design. Permanent per-bot failures (invalid token, unauthorized,
conflict, bad shape, missing env, lock held) stop only that runtime. Fix the
bot's config/token and restart the gateway, or use
`hermes multi-telegram status` to confirm which bot is at fault.

## Bots keep `retrying`

- No outbound HTTPS to `api.telegram.org`; check proxy/firewall.
- Wrong `proxy` URL or proxy down.
- System clock far off (TLS failures).
- Endpoint security software interfering with Python TLS.
- `api_base_url` points at a local Bot API server that is not running.

The supervisor backs off up to 60s. It does not spin tightly.

## Replies come from the wrong bot

This should not happen for live turns. If it does:

- verify the final message id is prefixed (`<bot_id>:<tg_id>`) in the session
  origin/status;
- check that you are not sending through a custom integration that passes a
  raw chat id and expects a global last-bot fallback (there isn't one);
- for proactive sends into a shared conversation, confirm the per-conversation
  route exists or set `default_bot`;
- check the gateway log for "no provable originating bot; refusing to guess".

Ambiguous sends fail closed rather than guessing. Provide explicit metadata or
use an isolated chat id.

## Shared bots do not share the transcript

- Both bots must have `session_mode: shared`.
- Both must use the same `shared_group` slug (case-insensitive parse).
- The external chat id, user id and forum topic id must match.
- A shared group is not global: a different user is a different session by
  design.
- Verify the synthetic chat ids are equal in the gateway log/session source.

## Shared bots share too much

- Check `shared_group`: every bot in it shares transcripts for the same
  external chat/user/topic.
- Different users should never share; if they appear to, inspect the synthetic
  `chat_id` and confirm the user id is present. Do not manually strip the
  suffix.
- `allow_all_users: true` admits more people into the same bot/session space;
  it does not merge different user ids, but it does expose the bot's tools.

## `/start` works but normal messages do nothing

- The sender is not authorized. Check `allowed_users` / `allowed_chats` and
  numeric ids.
- In a group, `group_require_mention` is on: mention `@botusername`, use a
  command addressed to this bot (`/cmd@botusername`), or reply to the bot. A
  bare `/cmd` is ignored in a group unless it is a reply to the bot.
- The message is an edit, channel post, sticker, location or contact; these
  are ignored in this release.
- The bot token belongs to a bot that has no privacy-mode access to group
  messages. As a bot admin, disable BotFather privacy mode or make the bot an
  admin, then re-add/re-check.

## Media is missing

- `max_attachment_mb` is 20 by default. Larger files get a text notice.
- Telegram Bot API has its own upload/download limits (20MB download through
  `getFile` for many bots).
- The media message may have been an album; each item arrives as a separate
  update and is processed separately.
- Check the gateway log for `could not download ... attachment` (redacted).

## Cron / notifications deliver nothing

This release does not register a `standalone_sender_fn`, so out-of-process
cron delivery cannot send through `telegram_multi` without a live gateway.
In-process notifications and replies to a session origin work through the
adapter. For out-of-process cron, use the native Telegram adapter or run the
delivery through a live gateway session. This is a known limitation, not a
token problem.

## Inline buttons/clarify do not work

This release does not install inline keyboards. A tapped callback from some
other source gets the text fallback "Interactive buttons are not enabled in
this release; reply with text." Use Hermes's text-based clarify/approval
fallbacks. Do not expect native Telegram buttons in this plugin version.

## Status file is stale or missing

- `status.json` lives under
  `%HERMES_HOME%\plugin-data\multi-telegram\`.
- If the gateway has never connected, `hermes multi-telegram status` shows
  `not_started`.
- A status file from a previous process is reset on the next `connect()`;
  per-conversation routes are preserved.
- Deleting the file is safe; proactive shared sends may fail closed until a
  bot receives a message in that conversation again (or `default_bot` is set).

## Update receipts / duplicate processing

The plugin persists per-bot update id receipts for 24 hours. If Telegram
redelivers a batch after a crash/restart, the receipt store drops duplicates.
If you intentionally replay updates from a backup, clear
`plugin-data/multi-telegram/update_receipts_<bot_id>.json` while the gateway is
stopped.

## Logs contain `Conflict`, `NetworkError` or `Retrying`, but no token

That is expected redaction. Tokens and `bot<id>:<secret>` strings are replaced
with `<redacted-token>`. If you need to diagnose the token itself, verify it
locally with `hermes multi-telegram validate --network`; never paste it into
chat.

## Validation passes but the real Telegram smoke test fails

The automated suite uses simulated Telegram updates and a simulated PTB bot
surface. A live failure can still come from:

- network/proxy/TLS on the gateway host;
- Telegram BotFather privacy mode;
- a token revoked after validation;
- a second poller;
- a chat id that does not exist / the bot is not a member.

Run:

```powershell
hermes multi-telegram validate --network
hermes multi-telegram test --bot <id> --chat <id> --text "smoke test"
```

and read the redacted error in the console/gateway log.
