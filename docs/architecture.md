# Architecture

## Shape

```text
Hermes Gateway
  └── one platform adapter: telegram_multi
        ├── BotRuntime "support"   -> PTB Application / Bot + long-poll updater
        ├── BotRuntime "sales"     -> PTB Application / Bot + long-poll updater
        └── BotRuntime "..."       -> optional, unbounded
              │
              ▼
        TelegramMultiAdapter
          ├── per-bot BotAuthorizer (intake)
          ├── synthetic chat/message identity
          ├── inbound normalizer -> one MessageEvent
          ├── outbound resolver (event-bound, fail closed)
          ├── media download/cache
          └── status + update receipts
              │
              ▼
        Hermes gateway -> one profile/agent/session store
```

There is exactly one Hermes platform registration and one adapter instance.
Each bot gets its own PTB `Application`, `Bot`, HTTP request pool and updater.
The runtimes are supervised independently: one invalid token stops that bot
without taking down the others.

## Why not N platform registrations?

N platform identities would produce N Hermes platforms, N platform-specific
authorization scopes and N adapter instances. The requirement is one shared
profile/agent, one platform identity, arbitrary bot count, and exact reply
routing. A single adapter with N internal transports is the smallest design
that meets all three without core changes.

## Why not N agents?

Bots are communication identities. They share the profile's model/provider,
memory, tools and capabilities. Creating an agent per bot would duplicate model
configuration and fragment memory. Per-bot `purpose`/`instructions` are
ephemeral channel prompts on the same agent.

## Inbound flow

1. PTB updater receives an `Update`.
2. `BotRuntime._on_message` calls `adapter._handle_update(runtime, update,
   context)`.
3. The adapter checks the persisted update receipt (dedupe across restarts).
4. Ignore edited messages, channels and malformed updates.
5. Enforce the bot's `BotAuthorizer` **before** any agent work.
6. Enforce group mention/reply policy.
7. Handle `/start` locally (welcome) without an agent turn.
8. Download permitted media, enforce size cap, cache through Hermes helpers.
9. Build a synthetic `chat_id` and a bot-prefixed `message_id`.
10. Build `MessageEvent` with:
    - `source.chat_id` = synthetic session scope;
    - `source.message_id` / `event.message_id` = `<bot_id>:<tg_id>`;
    - `source.scope_id` = real chat id;
    - `event.metadata` = bot id + real chat id (live turn);
    - `channel_prompt` = bot identity/purpose/instructions;
    - `auto_skill` = optional per-bot skills;
    - `toolsets_for_source` = optional per-bot toolset.
11. Bind the bot id in a `ContextVar` around `handle_message`; asyncio copies
    the context into the background turn task, so interim sends in shared
    sessions resolve correctly.
12. Return to PTB; the gateway runs the turn asynchronously.

## Outbound flow

`TelegramMultiAdapter.send`/media/edit/delete resolve the transport from:

1. isolated synthetic chat id (bot encoded);
2. `<bot_id>:` message/reply id (persisted in session origin);
3. explicit metadata;
4. turn-local `ContextVar`;
5. shared per-conversation route (proactive only);
6. explicit `default_bot` (raw chat ids only).

Then it decodes the real chat id and calls the correct `BotRuntime`. Text is
rendered from Markdown to Telegram HTML, split to 4096 UTF-16 units, and falls
back to plain text if Telegram rejects the entity markup. A failed media send
returns a classified `SendResult`; voice falls back to audio.

Final delivery is additionally wrapped:

- `send_final_ledgered` sets the bot ContextVar from `event.source` before the
  base delivery ledger runs;
- `_deliver_attachments` and `_notify_turn_error` do the same;
- `status_metadata_for_turn` adds the bot id to status/typing metadata.

This is what makes concurrent traffic safe: the turn's event, not a global
variable, owns the transport.

## Session identity

Hermes `build_session_key()` uses platform, chat type, chat id, thread id and
(optionally) user id. The plugin encodes sharing semantics in `chat_id`:

```text
isolated: tgmt:i:<bot_id>:<real_chat>:<user>
shared:   tgmt:s:<shared_group>:<real_chat>:<user>
```

The user id is included even if global group-per-user isolation is off. The
real forum topic stays in `thread_id`. Shared bots therefore produce the same
session key for the same external chat/user/topic; isolated bots do not.
Hermes stores/reloads the same session id, so the transcript is genuinely
shared.

## Message identity

A raw Telegram `message_id` is not globally unique across bots. Prefixing it
with the bot id (`soporte:123`) makes it:

- unique for edits/deletes/reply anchors;
- persistent in `SessionSource.message_id` / `SessionEntry.origin`;
- sufficient to route a reply after a gateway restart, when live adapter
  provenance is gone.

The prefix is not shown to the user.

## Shared proactive route

For sends with no event binding, a shared synthetic chat id intentionally
contains no bot. The adapter records the receiving bot per shared conversation
in `plugin-data/multi-telegram/status.json` (`routes`). This is:

- scoped to one shared group + real chat + user, not process-global;
- persisted across restarts;
- used only after event/reply/metadata/context resolution fails;
- fail-closed if absent.

## Busy shared sessions

Hermes's per-session pending slot is keyed by session only and can merge photo
bursts. In a shared session that would let one bot's pending turn answer
another bot's message. `TelegramMultiAdapter._handle_message_while_active`
defers cross-bot events (except recognized control commands) and
`_drain_cross_bot` starts each as its own turn once the session guard clears.
Text debounce also refuses to merge different bots. Shared turns remain
serialized by Hermes; this only preserves transport ownership.

## Lifecycle and supervision

- `connect()` starts all enabled runtimes, waits for the first attempt, and
  returns success if at least one bot connected. Slow/failed bots retry in
  their own tasks.
- `BotRuntime` retries `NetworkError`/`TimedOut` with exponential backoff
  (2s..60s + jitter). Invalid token, unauthorized, conflict, bad token shape,
  a lock held by another process, and a missing token env are permanent for
  that bot.
- Reconnect keeps Telegram's queued updates (`_ever_started` survives; the
  first cold start drops stale updates if configured).
- `disconnect()` stops all runtimes and flushes status/receipts. The runtimes
  are retained for a same-adapter reconnect so outage queues are preserved.
- Status is written atomically to the plugin data directory. Update receipts
  are persisted per bot with a 24h TTL and a hard cap, so a restart cannot
  duplicate an agent turn when Telegram redelivers a batch.

## Files

| File | Responsibility |
|---|---|
| `__init__.py` | loader entry point (`register`) |
| `registration.py` | platform/CLI registration, passive dep probe, active lazy install, config-level connectedness |
| `adapter.py` | `BasePlatformAdapter`, inbound/outbound, routing, status |
| `bot_runtime.py` | one supervised PTB Application per bot |
| `config.py` | parsing, validation, token resolution/redaction |
| `session_identity.py` | synthetic chat/message ids, resolution order |
| `authz.py` | per-bot authorization and union DM gate |
| `formatting.py` | Markdown→Telegram HTML + UTF-16 chunking |
| `status_store.py` | atomic status, routes, update receipts |
| `cli.py` | `hermes multi-telegram ...` token-safe commands |

## Deliberate non-goals

- No core edits or monkey-patching.
- No per-bot model/provider/memory.
- No webhook ingress.
- No inline clarify/approval keyboards in this release.
- No global "last active bot" routing.
