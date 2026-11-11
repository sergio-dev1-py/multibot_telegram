# Session modes and routing

The plugin does not create a separate Hermes agent per bot. Hermes derives a
session key from:

```text
namespace : platform : chat_type : chat_id [: thread_id] [: user_id]
```

The plugin controls the `chat_id` component. That is the supported way to get
different sharing semantics without touching Hermes core.

## Synthetic chat ids

Isolated bot:

```text
tgmt:i:<bot_id>:<real_chat_id>:<user_id>
```

Shared group:

```text
tgmt:s:<shared_group>:<real_chat_id>:<user_id>
```

The real Telegram chat id and sender id are preserved inside the value and are
decoded before every Telegram API call. The `user_id` is part of the synthetic
chat id deliberately: it keeps unrelated users separate even if the global
`group_sessions_per_user` setting is `false`.

The real forum-topic id remains in `thread_id` and therefore remains part of
the session key.

## Examples

Two isolated bots, same group and user:

```text
tgmt:i:soporte:-1001234567890:42
tgmt:i:ventas:-1001234567890:42
```

Different Hermes sessions. Each bot has its own transcript.

Two shared bots in group `novadesk`, same group and user:

```text
tgmt:s:novadesk:-1001234567890:42
tgmt:s:novadesk:-1001234567890:42
```

The same Hermes session key, therefore the same stored transcript. A turn that
starts on `soporte` and a later turn that starts on `ventas` continue one
conversation.

Different user in the same shared group:

```text
tgmt:s:novadesk:-1001234567890:99
```

A different session. Users never share a transcript just because the bots do.

Same shared group and user, different forum topic:

```text
chat_id   = tgmt:s:novadesk:-1001234567890:42
thread_id = 17  (or 18)
```

Different session keys because `thread_id` differs.

DM traffic uses the same scheme; the real chat id is the user's Telegram id.

## Why this is a real shared transcript

Hermes stores sessions by session key in its session store and reuses the
stored session id. The shared bots produce the same key, so Hermes does not
create a second session. This is not "shared memory" or a copied summary; it is
the same transcript row.

The plugin does not merge:

- different external chats;
- different users;
- different forum topics;
- different shared groups;
- isolated bots with each other.

## Runtime routing (which bot sends the reply)

Every inbound Telegram message id is prefixed with the bot id:

```text
<bot_id>:<telegram_message_id>
```

That prefix is stored in `SessionSource.message_id` (which Hermes persists in
`SessionEntry.origin`) and in `MessageEvent.message_id`. It is also copied into
the outbound reply anchor.

Outbound resolution order (`resolve_bot_id`):

1. an isolated synthetic `chat_id` (`tgmt:i:<bot_id>:...`) — explicit;
2. a reply/message id prefix (`<bot_id>:<id>`) — explicit;
3. explicit metadata (`multi_telegram_bot_id` / `mt_bot_id`);
4. the turn-local `ContextVar` set by the inbound adapter task;
5. for shared `chat_id`s only: the persisted per-conversation receiving-bot
   route;
6. for a raw (non-synthetic) Telegram chat id only: the explicit
   `default_bot`.

If none applies, the send fails closed with a clear error. There is no
process-global "last active bot" variable.

## Final delivery

`send_final_ledgered`, attachment delivery, and turn-error notices override the
adapter's `_BOUND_BOT_ID` `ContextVar` from `event.source` for the duration of
the delivery. Final responses are therefore event-bound even when two shared
bots' turns interleave. Tests exercise concurrent final sends.

`status_metadata_for_turn` adds the bot id from the reply anchor (or the turn
context) so status/typing refreshes also target the right transport.

## Proactive delivery into a shared conversation

Cron, restart notices and background notifications have no triggering event.
For a shared conversation the resolver uses the **per-conversation** route
recorded when a bot last received there. This is not global: it is keyed by the
full synthetic shared chat id (shared group + real chat + user).

If the route is missing (fresh install, deleted status file) and no explicit
metadata points at a bot, the send fails closed. An operator can set
`default_bot` to provide a deliberate fallback for raw chat ids; it is not a
hidden "last bot".

## Busy shared sessions

Hermes serializes turns per session. If a message from bot B arrives while a
shared session is busy with bot A, the adapter does **not** merge B's text or
media into A's pending event (the base adapter's pending slot is keyed by
session and its photo-burst merge would otherwise let A answer B's message).
It defers the cross-bot event and starts it as a separate turn when the
session guard clears. Commands that intentionally bypass an active session
(`/stop`, `/new`, `/reset`) still go through Hermes's normal bypass path.

This preserves exact transport ownership; it does not make turns concurrent.
Shared-session turns remain serialized by Hermes. Same-bot rapid text bursts
may still be debounced/merged, which is the normal chat behavior.

## Interaction with Hermes settings

- `group_sessions_per_user` is irrelevant for privacy here because the user id
  is already in the synthetic chat id. It still affects the final key shape
  (the user may appear twice).
- `thread_sessions_per_user` can further split forum topics; the plugin always
  keeps the topic id in the key.
- Per-bot `toolsets`/`skills` are turn-level overlays on the same profile.
  They do not create a separate model, memory or agent.

