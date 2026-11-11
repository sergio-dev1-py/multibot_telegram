# Configuration reference

All keys live under `gateway.platforms.telegram_multi.extra` (a top-level
`platforms.telegram_multi.extra` block is equivalent; Hermes merges both).
The plugin contains no default bot ids, names, company data or roles. Every
bot and every piece of context is operator configuration. The values in
`examples/` are fictional demonstrations, not plugin defaults.

## Platform-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `bots` | list | required | Ordered list of bot definitions. At least one, no upper limit. |
| `default_bot` | slug | none | Explicit fallback for raw Telegram chat ids with no conversation-scoped bot (operator/cron sends). Proactive sends in shared conversations use the persisted per-conversation route first. |
| `shared_context` | string | `""` | Optional free-form context injected into every bot's ephemeral prompt. Leave empty for a generic deployment; use it for a short company/project brief. Long documents belong in the Hermes workspace or skills. |
| `connect_wait_seconds` | float | `18` | How long the platform waits for the first connection attempt of each bot. Must stay below the gateway's platform connect timeout. |
| `group_sessions_per_user` | bool | Hermes default | Passed through by the base adapter. The plugin's synthetic chat id already contains the user, so shared users stay isolated even when this is `false`. |
| `thread_sessions_per_user` | bool | Hermes default | Same note as above; a forum topic is always part of the synthetic session key. |

## Bot keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | Stable bot id; also the isolated session scope. `[a-z0-9][a-z0-9_-]{0,31}`. |
| `display_name` | string | `id` | Human name used in prompts, welcome text and status. This is a plugin label, not the bot name/username shown in Telegram; set that in BotFather. |
| `token_env` | UPPER_SNAKE | required | Environment variable holding the token in the profile secret scope. Must be unique and must not be `TELEGRAM_BOT_TOKEN` / `TELEGRAM_MULTI_BOT_TOKEN`. |
| `enabled` | bool | `true` | A disabled bot is not polled and is not authorized. |
| `session_mode` | `isolated` or `shared` | `isolated` | Session scoping strategy. |
| `shared_group` | slug | none | Required for `shared`; bots with the same slug share a transcript for the same external chat/user/topic. |
| `allowed_users` | list of numeric ids | `[]` | Telegram user ids allowed to talk to this bot. Usernames are rejected. |
| `allowed_chats` | list of numeric ids | `[]` | Telegram chat/group ids allowed for this bot. |
| `allow_all_users` | bool | `false` | Admits any sender reaching the bot. Strongly discouraged; status exposes it and the log warns. |
| `allow_bots` | bool | `false` | Admits messages whose author is a bot. Leave false unless you deliberately bridge bots; two bots in one shared group can otherwise ping-pong, and Hermes's loop guard is only a backstop. |
| `group_require_mention` | bool | `true` | In groups, require an `@bot` mention, a command addressed to this bot (`/cmd@bot`), or a reply to this bot. A bare `/cmd` is ambiguous when several bots share a group and is ignored unless it is a reply to this bot. Set false to forward every group message. |
| `purpose` | string | `""` | Short purpose appended to the per-turn ephemeral channel prompt. |
| `instructions` | string | `""` | Per-bot operating instructions appended to the ephemeral prompt. These are not persisted to the transcript. |
| `welcome_message` | string | built-in | Sent for `/start`. Rendered as Telegram HTML and sent directly, without an agent turn. |
| `toolsets` | list of strings | `[]` | Optional per-bot toolset override (Hermes `toolsets_for_source`). Leave empty to share the platform/profile default. |
| `skills` | list of strings | `[]` | Optional per-bot auto-loaded skills (Hermes `auto_skill`). Leave empty to share the default behavior. |
| `proxy` | `http(s)://` or `socks5://` | none | Per-bot HTTP/SOCKS proxy for the PTB requests. |
| `api_base_url` | `http(s)://...` | Telegram default | Per-bot Bot API base URL (for a local Bot API server). |
| `file_base_url` | `http(s)://...` | Telegram default | Per-bot Bot API **file** base URL for a local Bot API server (needed for media downloads/sends). |
| `drop_pending_updates` | bool | `true` | Drop queued updates on the **first** start of a process. Reconnects never drop the outage queue. |
| `request_timeout` | float | `30` | PTB write/pool timeout. |
| `connect_timeout` | float | `10` | PTB connect timeout. |
| `read_timeout` | float | `30` | PTB read timeout (polling uses at least 30s). |
| `max_attachment_mb` | int | `20` | Maximum inbound attachment accepted for download. A larger file gets a text notice, not an agent turn. |

## Roles and shared context

`shared_context` is optional and shared by every bot. `purpose` and
`instructions` are per-bot additions to the ephemeral channel prompt. They are
not persisted into the transcript, and they do not enforce a hard security
boundary. For hard separation of models, memories or secret-bearing tools, use
separate Hermes profiles instead of separate bots in one profile.

Transcript isolation is not memory isolation. `MEMORY.md`, `USER.md`,
`session_search` and tools are profile-wide. A bot can write a fact to memory
and another bot can read it even when their sessions are isolated. The
NovaDesk example sets `toolsets: [web, vision, todo]` on every bot to keep the
demo's transcript-only handoff honest. Widen that list if your deployment
needs more tools.

The plugin's defaults are deliberately empty:

- no bot ids or display names;
- no roles or company data;
- no shared context.

Examples under `examples/` use the fictional NovaDesk deployment only to show
the shape of a real configuration.

## Authorization semantics

Authorization runs in `adapter._handle_update` **before** `handle_message`, so
rejected messages never reach the gateway/agent.

For DMs:

- `allow_all_users: true` admits every non-bot sender;
- otherwise the sender id must be in `allowed_users`, or the DM chat id must
  be in `allowed_chats`;
- an empty configuration (`no allowed_users`, `no allowed_chats`,
  `allow_all_users: false`) denies everyone.

For groups/supergroups:

- if `allowed_chats` is non-empty, the real chat id must be listed;
- if `allowed_users` is non-empty, the sender must be listed;
- both checks apply when both lists are set;
- a chat-only allowlist admits any non-bot member of that chat;
- a user-only allowlist admits that user in any group;
- `group_require_mention` is applied after authorization.

The authorizer also denies senders with no id and, by default, bot senders.
Anonymous group admins appear as `GroupAnonymousBot`; they are denied unless
`allow_bots: true`, and enabling that admits anonymous admins.

### Platform-wide DM-gate caveat

Hermes's own authorization layer is scoped per *platform*, not per bot. For a
DM it sees the union of the enabled bots' allowlists (`union_allows_dm`), then
the adapter applies the exact per-bot list. This means:

- a user on the union can reach the adapter;
- they still cannot reach a bot that does not list them;
- if any bot sets `allow_all_users: true`, the platform-wide gate becomes
  permissive for DMs, but the other bots keep their own per-bot checks.

Do not rely on the union as an allowlist. The per-bot check is authoritative.

## Token resolution

`resolve_token(bot)` calls `gateway.platforms._shared.get_scoped_secret(
bot.token_env, "")`. Under a profile secret scope, a miss returns empty rather
than borrowing another profile's environment. If you manage secrets
externally, ensure the resolver exposes the same variable names to the active
profile.

A valid token must match `^\d+:[A-Za-z0-9_-]{30,}$`. Shape validation gives a
clear operator error before any network call.

## Validation and diagnostics

```powershell
hermes multi-telegram validate --no-tokens   # structure only
hermes multi-telegram validate               # structure + token presence/shape/duplicates
hermes multi-telegram validate --network     # + live getMe per enabled bot
hermes multi-telegram status                 # connection state + token presence
```

`validate` exits non-zero on duplicate ids, duplicate token variables, a
shared bot without `shared_group`, missing/invalid tokens (unless
`--no-tokens`), or a duplicate token value. It never prints a token.

## Common configuration mistakes

- Putting `session_mode: shared` without `shared_group`.
- Giving two bots the same `token_env` or the same token value under two
  variables.
- Reusing the native Telegram token variable.
- Listing usernames instead of numeric ids.
- Setting `group_require_mention: false` in a busy group with
  `allowed_users` empty and `allowed_chats` set to the group: that admits
  every member's every message.
- Installing the plugin under a different directory name without updating
  `plugins.enabled`.
- Leaving `plugins.isolation: host`.
