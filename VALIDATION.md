# Validation report

Date: 2026-11-10
Host: Windows (`win32`), local Hermes install
Hermes: 0.23.0, commit `2bd378ed1b8d` (per the installed checkout)
Python: 3.14.9
`python-telegram-bot`: 22.8

## Summary

| Check | Result |
|---|---|
| `python -m compileall .` | PASS |
| Automated test suite | **140 tests, 0 failures, 0 errors** |
| `hermes plugins validate .` | PASS (`ok: true`, no warnings) |
| `hermes plugins validate . --install-deps` | PASS (dependencies resolved/installed) |
| `hermes plugins doctor . --ci` | exit 0; runtime discovery/import/registration PASS |
| Capability probe | PASS: `register()` ran in isolation |
| Security scan | PASS: `safe` |
| Isolation audit | `in_process` (platform adapter requirement) |
| Config-load integration | PASS with a temporary `HERMES_HOME` |
| Simulated demo | PASS |
| **Real Telegram live test** | **PASS for the core flow** — 3/3 connected, plans reply, shared handoff and isolated Support verified; group, unauthorized, concurrency and media checks are still pending |

## Automated tests

Command:

```powershell
python -m unittest discover -s tests -t . -v
```

Result:

```text
Ran 140 tests in ~6s

OK
```

The suite uses:

- simulated Telegram updates (`SimpleNamespace` messages and real PTB
  `Update.de_json` objects);
- a simulated PTB bot surface for outbound calls (`send_message`,
  `edit_message_text`, `send_photo`, `send_document`, `send_voice`,
  `send_chat_action`, `delete_message`);
- simulated per-bot runtimes;
- real gateway classes for `SessionSource`, `build_session_key`,
  `BasePlatformAdapter` and `SendResult`;
- temporary locations for status/receipt stores.

Coverage highlights:

- config parsing/validation, duplicate ids/envs/tokens, shared-group rules,
  numeric allowlists, token shape, optional `shared_context`;
- whole-token redaction in logs/errors, including tokens not previously known;
- isolated/shared synthetic ids, real session-key equality for shared bots,
  user/topic/chat isolation, persisted message-id routing;
- per-bot DM/group/bot-sender authorization, the union DM gate, and the
  derived platform-wide `extra.allowed_users` / `group_allowed_chats` that
  Hermes 0.23 checks before dispatch;
- inbound PTB update handling, dedupe receipts, mention/reply policy,
  `/start`, callback fallback, media download and oversize rejection;
- per-bot ephemeral prompt injection for `shared_context`, `purpose` and
  `instructions`, including the empty-context default;
- outbound text/media/edit/delete/typing, voice fallback, HTML fallback,
  retry-after propagation, fail-closed unbound sends;
- concurrent `send_final_ledgered` turns on two shared bots, proving each
  response leaves through its own transport (no global last bot);
- cross-bot busy-session deferral (no text/media merge across bots) and
  same-bot debounce behavior;
- runtime supervise/retry/permanent-failure classes and token-safe state;
- PTB startup wiring (builder request objects, handler registration,
  webhook deletion, polling allowed updates, first-start vs reconnect pending
  update policy, base URL application) with a simulated PTB Application;
- status persistence, per-conversation proactive routes, update receipts;
- CLI status/validate output never contains a token value;
- registration call shape (`register_platform`, `register_cli_command`,
  lazy dependency installer).

## Plugin validation

```powershell
hermes plugins validate . --json
```

Observed: `"ok": true`, `"warnings": []`.

Checks passed: manifest parse/fields, `requires_env`, loadable entry,
1 Python requirement from `pyproject`, capability probe, declared
tools/hooks/middleware, built-in tool collisions, security scan (`safe`), no
core override.

Isolation audit:

```json
{
  "verdict": "in_process",
  "host_ready": false,
  "reasons": [
    "kind 'platform': gateway platform adapters run in the Hermes process",
    "registration.py ctx.register_platform(): platform adapters run on the gateway's event loop with native SDK clients"
  ]
}
```

This is expected and documented: set `plugins.isolation: in_process`.

## Dependency install

```powershell
hermes plugins validate . --install-deps --json
```

Observed: dependency resolution and installation completed, followed by the
same passing validation. The installed environment contains PTB 22.8.

## Plugin doctor

```powershell
hermes plugins doctor . --ci
```

Observed: exit code 0.

```text
Plugin Doctor: ...\multi-telegram
  manifest: multi-telegram 1.1.1 (platform)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 0 hook(s)
```

The plugin is installed into the active Hermes profile and enabled. The
doctor's exit code is 0. On this host it also prints a host-level notice that
the Hermes environment is out of sync with `uv.lock`; that notice is about the
Hermes installation, not the plugin, and does not affect registration.

## Configuration-load integration

The active profile configuration was loaded by the running gateway. The
plugin reports 3/3 bots connected and resolves:

```text
soporte  isolated  novadesk context  allowed_users=1
ventas   shared    shared_group=novadesk
tecnico  shared    shared_group=novadesk
```

An earlier temporary `HERMES_HOME` test also loaded a minimal
`gateway.platforms.telegram_multi` block through the real
`gateway.config.load_gateway_config()`, confirming that the platform block
reaches `PlatformConfig.extra` and parses into `MultiTelegramConfig`.

## Simulated demo

```powershell
python scripts/demo.py
```

Observed:

- shared `ventas` and `tecnico` produce the same synthetic scope;
- isolated `soporte` differs;
- a different user produces a different scope;
- routing resolves isolated chat id, reply prefix, explicit metadata and
  shared route, and returns `None` when nothing is provable;
- no token is read and no network call is made.

## Real Telegram testing - core flow PASS

Three dedicated test bots are configured in the active profile, all tokens are
present in the profile secret scope, and none are written to this repository.

Observed so far:

- `hermes multi-telegram validate --network` exits 0;
- `getMe` succeeds for all three bots;
- the gateway logs `3/3 bot(s) connected: soporte, ventas, tecnico`;
- `hermes multi-telegram status` reports `Overall: connected`;
- `soporte` is isolated; `ventas` and `tecnico` share `shared_group: novadesk`;
- the gateway reconnected cleanly after a plugin/config restart, and the
  earlier transient `httpx.ReadError` did not stop polling;
- live `ventas` replied with the NovaDesk plans;
- `hermes plugins validate --install-deps` passes with the final manifest;
- the session store contains two distinct keys:
  `agent:main:telegram_multi:dm:tgmt:i:soporte:...` and
  `agent:main:telegram_multi:dm:tgmt:s:novadesk:...`;
- `tecnico` reused the shared `ventas`/`tecnico` session and recalled the
  reference code from the shared transcript.

### Important finding: transcript isolation is not memory isolation

During the first live handoff, `ventas` saved the reference code with Hermes's
profile-wide `memory` tool. `soporte` then repeated it from memory even though
its transcript was a different session. This is expected for one Hermes
profile: `MEMORY.md`, `USER.md`, `session_search` and tools are shared across
all bots. It is not a failure of the synthetic session keys.

The demo was reset for a clean retest:

- the leaked demo memory entry was removed (backed up first);
- the two test sessions were deleted;
- every demo bot now uses `toolsets: [web, vision, todo]`, which excludes
  `memory`, `session_search`, `terminal` and `file`;
- the demo code is `VERDE-77` and the instructions now tell `ventas` to keep it
  in the shared transcript and `tecnico` to quote it from that transcript.

After the reset, the clean live flow passed:

- `ventas` replied with the plans and explicitly registered `verde-77`;
- `tecnico` continued the shared transcript, quoted `verde-77` and repeated the
  Growth recommendation and price;
- `soporte` was asked only “¿Qué código te dio venta?” and did **not** know
  `verde-77`; it said it had no such code and asked for clarification;
- the session store shows exactly two keys for this run:
  `agent:main:telegram_multi:dm:tgmt:i:soporte:...` and
  `agent:main:telegram_multi:dm:tgmt:s:novadesk:...`;
- `MEMORY.md` stayed empty after the test, confirming the demo toolsets prevented
  another memory write.

The following live checks are still pending and are not claimed:

- group messages without a mention do not trigger a bot;
- an unauthorized second account gets no agent turn;
- concurrent messages to `ventas` and `tecnico` receive replies from the exact
  bot addressed;
- an authorized media upload reaches the agent and a reply is delivered.

To finish the validation, use the three test bots with the profile owner
account:

```powershell
hermes multi-telegram validate --network
hermes multi-telegram status
hermes logs gateway -n 100
```

Then run the conversation checklist and record the observed results here
before publishing a catalog PR.

## Residual risks / unverified areas

- Real Telegram API and BotFather privacy-mode behavior.
- Real Hermes gateway streaming/interim-message behavior against Telegram
  flood limits.
- Long-running reconnect behavior under a real network outage.
- Windows service/scheduled-task secret-scope visibility.
- Out-of-process cron delivery (no `standalone_sender_fn` in this release).
- Telegram API changes after PTB 22.8.
- The platform-wide DM authorization union caveat (documented; adapter is
  authoritative per bot).
- Inline keyboards/clarify buttons are not implemented; text fallback only.
- Telegram DM topics (as opposed to group forum topics) are unverified.

## Reproduce locally

```powershell
# from the plugin directory
python -m compileall -q .
python -m unittest discover -s tests -t . -v
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
python scripts/demo.py
```

The test command needs a Python environment with the Hermes checkout and PTB
22.8. The repository's dependency manifest installs PTB; `hermes`'s own
`telegram` extra also provides it.

