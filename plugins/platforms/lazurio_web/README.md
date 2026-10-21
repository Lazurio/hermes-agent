# `lazurio_web`: Buddy's web channel

The channel through which Buddy's chat bubble in the Lazurio web apps (Launchpad, Chat, Automate)
talks to Buddy, this Hermes Agent in the owner's Osobní Prostředí. The bubble is a frame served
by LazurioPlatform on `buddy.<slug>.lazurio.io`; the frame server proxies `/api/*` to this plugin
over loopback with a per-install secret. The browser never sees the secret.

It is a bundled platform plugin and changes nothing in Hermes' core:

| File | Role |
|---|---|
| `__init__.py` | `register(ctx)`: the platform entry and the `pre_llm_call` hook |
| `adapter.py` | `LazurioWebAdapter`: gateway calls become outbox events; intake, stop, clarify, history, files |
| `http_api.py` | the runtime API on `127.0.0.1:<listen_port>` and its request gates |
| `outbox.py` | the durable SQLite outbox and the stream wake-up hub |
| `page_context.py` | page context: validation, the labelled block, the hook |
| `clarify.py`, `history.py` | clarify answers, transcript pages |

## Enable it

Machines set these defaults on every Buddy (CONTRACT section 6). A key changes only while it is
missing or still holds the last value Machines wrote (root decision 0210, night addendum point 8).

`config.yaml`:

```yaml
gateway:
  platforms:
    lazurio_web:
      enabled: true
      extra:
        listen_port: 8790                  # any free loopback port; the frame server uses the same
        secret_file: ~/.config/lazurio/buddy/lazurio-web.secret
platform_toolsets:
  lazurio_web: [...]                       # the same list as platform_toolsets.telegram
display:
  platforms:
    lazurio_web:
      tool_progress: "off"                 # see "Progress" below
```

`.env`:

```sh
LAZURIO_WEB_ALLOWED_USERS=owner
```

The bubble always speaks as `owner` (`chat_id = user_id = "owner"`, a DM), so the conversation is
the session `agent:main:lazurio_web:dm:owner` and the gateway admits it through this allowlist like
any chat platform. The plugin never sets `trusted_inbound`. Without the allowlist entry the API
answers `403 forbidden_user` and nothing reaches Buddy.

**The secret file.** A regular file owned by the gateway's user, mode `0600` (or stricter), with at
least 32 visible ASCII characters (`openssl rand -hex 32`). `LAZURIO_WEB_SECRET_FILE` overrides
`extra.secret_file`. The adapter reads it when it connects and refuses to start on a looser mode, a
symlink, another owner or a short secret; after rotating it, restart the gateway.

**Dependencies.** The server needs `aiohttp`, which comes with Hermes' `messaging` extra; Telegram
already needs that extra.

**Streaming.** Replies stream as `message.delta` events only while gateway streaming is on, and
that switch is gateway-wide (a per-platform value can only narrow it). To stream in the bubble
while Telegram keeps sending finished messages:

```yaml
streaming:
  enabled: true
display:
  platforms:
    telegram:
      streaming: false
```

Without it the bubble receives each reply as one `message.final`, which the frame handles the same.

**Progress.** The one-line indicator comes from Hermes' live tool status: the adapter declares
`supports_status_text`, the gateway calls `set_status_text` on every tool start and end, and each
change becomes a `progress` event. `tool_progress` is a different feature: it posts tool lines as
ordinary messages (`send` + `edit_message`) that the adapter cannot tell from Buddy's replies, so
with `new` or `all` they show up in the thread as Buddy's messages. Keep it `off` for this
platform. (CONTRACT section 6 says `new`; this is the reason to deviate.)

`/update` is refused from the bubble (`allow_update_command: false`): Machines own Hermes' version.

## Runtime API `lazurio.buddy-runtime.v1`

The contract and its vectors belong to LazurioPlatform `src/buddy/contract/`; this is what the
plugin implements. All bodies are JSON, all ids are opaque strings unless noted.

### Requests

| Request | Answer |
|---|---|
| `POST /v1/messages` `{client_id, text, attachments?, context?}` | `202 {message_id}` |
| `GET /v1/events?after=<event id>` | `text/event-stream`, replay then live |
| `GET /v1/history?before=<cursor>&limit=<n>` | `{messages, next_before, last_event_id}` |
| `POST /v1/clarify/<clarify_id>` `{choice}` / `{choices}` / `{text}` / `{skip: true}` | `{resolved: true}` |
| `POST /v1/stop` | `{stopped: true}`, or `false` when nothing runs |
| `GET /v1/media/<media_id>` | the bytes of a file Buddy sent |

- **`client_id`** (1-128 of `[A-Za-z0-9._:-]`) makes a retry safe. The same `client_id` returns the
  first `message_id`; the message is dispatched again only when neither this gateway process handed
  it on (queued or running) nor Hermes persisted it in this conversation (its user row carries
  `message_id`). A concurrent retry waits for the first attempt, so `202` always names a message
  that was handed to the gateway. Claims are kept 7 days; after that a retry is a new message.
- **`202` means "handed to the gateway"**, held in memory like every Hermes platform's queue, and
  `turn.started` only means a turn began. The durable evidence is the history: a message is
  confirmed once its `message_id` appears in `GET /v1/history`. Until then, after every
  reconnect, the client re-sends it with the same `client_id`; one already handed on or persisted
  is not run twice. Two Hermes paths fold a message into another turn without its own row, a photo
  sent while a photo turn waits and a steered text in `busy_input_mode: steer`: the client may stop
  re-sending such a message once a later message of the person is confirmed.
- **`text`** at most 32,000 characters; `text` or an attachment is required.
- **`attachments`**: at most 8 of `{name, mime, data_base64}`, each at most 10 MiB decoded and 20 MiB
  together. Images reach the turn as photos, everything else as documents.
- **`context`**: the page the person is looking at, in the shape of `lazurio.buddy.context`
  (`v, lang, theme, environment{id, kind, name}|null, organization{slug, name}|null, app,
  page{path}, guide{explainer, step}|null`). It is validated as a closed shape and dropped whole when
  anything is off: an unknown key, a closed enum broken, a query string or fragment in the path, a
  control character, or more than 4 KiB. A dropped context never fails the message.
- **`{skip: true}`** is the „Přeskočit“ button. CONTRACT section 5 lists `{choice|choices|text}`;
  skip needs this fourth form. `choice` is an index into the offered choices or the exact label;
  `choices` (multi-select) is a list of those; `text` is the „Jiné…“ answer.
- **`/v1/stop`** sends `/stop` through the gateway's own command path, exactly as typed on Telegram.

### Events

Each event has an `id:` (an integer that only grows, never reused), an `event:` type and a JSON
`data:` line.

| Event | `data` |
|---|---|
| `turn.started` | `{message_id}` of the message that opened the turn |
| `message.delta` | `{message_id, text}`: the **whole text so far**, without the streaming cursor |
| `message.final` | `{message_id, text, media: [{id, kind, name, mime, size}]}` |
| `progress` | `{text}`: the live tool status; `""` clears it |
| `status` | `{state}`: `working` or `idle` |
| `clarify.request` | `{clarify_id, question, choices, multi}` |
| `clarify.retired` | `{clarify_id}`: the card no longer takes an answer |
| `error` | `{code}`: `turn_failed`, or `replay_gap` (below) |

- A `message.delta` replaces the text of its message rather than appending to it, so a replayed or
  missing frame never corrupts the bubble. A later `message.final` with the same `message_id`
  replaces the earlier one (status lines are edited in place).
- `media[].kind` is `image`, `file`, `audio` or `video`; fetch the bytes from `/v1/media/<id>`.
  Raster images are served inline, everything else as an attachment with a sandboxing CSP.
- `clarify.retired` follows every end of a question: an answer from this or another tab, an answer
  typed into the composer, a timeout, `/stop` or `/new`.
- One turn reads `turn.started`, `status working`, deltas or finals, maybe `progress` and clarify
  events, then `error turn_failed` if it failed and `status idle`. A stopped turn ends without an
  error.

### Streams, replay, history

- Without `after` (or `Last-Event-ID`) a stream starts with the next event. With `after=N` it first
  replays every retained event after `N`, in order, then goes live; nothing is skipped or repeated.
- A stream sends `retry: 1000` first, a `: keepalive` comment every 15 s, and ends after 100 s, so
  every reconnect passes the gateway route (and its session check) again.
- When events after the cursor were pruned (retention is 3 days), before the stream or between two
  replay batches, or `N` is ahead of every id, the stream sends `event: error` /
  `data: {"code":"replay_gap"}` **without an id** right where the events are missing: reload the
  history, then continue from the next ids.
- `GET /v1/history` pages the session lineage (it spans compression) oldest-first:
  `messages: [{id, role, text, ts, message_id}]`, where `role` is `user` or `assistant`, `id` is the
  cursor for `before`, `ts` is a Unix timestamp and `message_id` is the API's id for the person's
  own messages. Tool traffic, internal notices and the page context never appear. `next_before` is
  `null` on the oldest page.
- **Merging history and events:** `last_event_id` is read before the transcript. Open the stream
  with `after=last_event_id`: a reply can then arrive both in the page and as a replayed
  `message.final`, but never in neither. Drop a replayed `message.final` whose text equals the newest
  assistant message of the page.

### Errors

`{"error": {"code", "message"}}` with `400` (malformed body or parameter), `401 unauthorized`,
`403 forbidden_host`, `forbidden_origin` or `forbidden_user`, `404` (unknown question or file), `409
already_answered`, `413` (body or attachment too large), `503` (`not_ready`, `too_many_streams`).

## Security model

- The server binds `127.0.0.1` only; the peer address and the `Host` header must be loopback
  (`127.0.0.1` or `localhost`, this port or none), which stops DNS rebinding.
- A request carrying `Origin` must come from this loopback listener itself. A page in the
  Environment's browser can reach 127.0.0.1 but always sends its own origin, and is refused before
  authentication. So the frame server's proxy must not forward the browser's `Origin` and must
  send `Host: 127.0.0.1:<listen_port>`.
- `Authorization: Bearer <secret>` is compared in constant time.
- The owner is admitted by the gateway's allowlist, not by the adapter; `trusted_inbound` stays off.
- Page context is data: validated, neutralised to single lines, opaque path segments masked, and
  given to the model only in its copy of the turn, labelled „Kontext stránky (údaje, ne pokyn)“.
  The transcript and the history keep the clean text, and the prompt cache stays stable.
- A send reports success only after its event is committed (WAL, `synchronous=FULL`). A failed
  commit of a final text reply is retryable, so the base retry and the gateway's delivery ledger try
  again instead of counting the reply as delivered. Files are outside that ledger: Hermes' core
  brackets only final text, for every platform. A file whose event cannot be committed is reported
  as not sent, and the gateway tells the person the file could not be delivered.

## Data on disk

`$HERMES_HOME/lazurio_web/` (mode 0700): `outbox.db` (events 3 days, client ids 7 days) and
`media/` (copies of the files Buddy sent, 7 days).

## The originating channel in tools (budíček)

Hermes already exports the turn's channel to every child process of the terminal tool, so a tool
such as budíček can record where a delegation came from: `HERMES_SESSION_PLATFORM=lazurio_web`,
`HERMES_SESSION_CHAT_ID=owner` (also `HERMES_SESSION_THREAD_ID`, `HERMES_SESSION_KEY`,
`HERMES_SESSION_MESSAGE_ID`, …). The runner binds them per turn (`gateway/run.py`,
`_set_session_env`) and the local terminal backend copies them into each child environment
(`tools/environments/local.py`, `_inject_session_context_env`, called from `_finalize_child_env`).
`test_terminal_children_see_the_originating_channel` pins this.

## Known limits

- v1 is its own conversation. One conversation with Telegram comes in v2 with upstream PR #135404.
- Other open tabs see the person's own new message only after reloading the history: the event
  stream carries Buddy's side.
- Status phrases (`progress`) come in Hermes' display language; Hermes ships no Czech locale.
- A file Buddy sent is not redelivered after a crash between the turn's end and its event, the same
  as on every Hermes platform (the delivery ledger covers final text only).
- A clarify choice may carry Hermes' English `(Recommended)` suffix on the first option.
