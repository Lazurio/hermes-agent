# `lazurio_web`: Buddy's web channel

The channel through which Buddy's chat bubble in the Lazurio web apps (Launchpad, Chat, Automate)
talks to Buddy, this Hermes Agent in the owner's Osobní Prostředí. The bubble is a frame served
by LazurioPlatform on `buddy.<slug>.lazurio.io`; the frame server is this plugin's only client and
talks to it over loopback with a per-install secret. The browser never sees the secret.

**The wire contract is LazurioPlatform's** `lazurio.buddy-runtime.v1`
(`src/buddy/contract/README.md` and its vectors in the Platform repository). This plugin implements
it; this file describes how, and what is specific to Hermes.

It is a bundled platform plugin and changes nothing in Hermes' core:

| File | Role |
|---|---|
| `__init__.py` | `register(ctx)`: the platform entry and the `pre_llm_call` hook |
| `adapter.py` | `LazurioWebAdapter`: gateway calls become outbox events; intake, stop, clarify, history, files |
| `http_api.py` | the endpoints and the contract's admission order |
| `wire.py` | the contract's request checks, bounds and the cleaning of outgoing text |
| `outbox.py` | the durable SQLite outbox and the stream wake-up hub |
| `page_context.py` | the page context: its exact shape, the labelled block, the hook |
| `clarify.py`, `history.py` | clarify cards and answers, transcript pages |

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
any chat platform. The plugin never sets `trusted_inbound`. Without the allowlist entry every
message is `503 unavailable` and nothing reaches Buddy.

**The secret file.** A regular file owned by the gateway's user, mode `0600` (or stricter), one line
of at least 32 visible ASCII characters (`openssl rand -hex 32`); a trailing newline is ignored.
`LAZURIO_WEB_SECRET_FILE` overrides `extra.secret_file`. The adapter refuses to start without a
usable file, and reads it again whenever it changes, so a rotation needs no restart. A file that
turns unsafe (a looser mode, a symlink, another owner, too short) answers `401` to everyone until it
is fixed.

**Dependencies.** The server needs `aiohttp`, which comes with Hermes' `messaging` extra; Telegram
already needs that extra.

**Streaming.** Replies stream as `message.delta` pieces only while gateway streaming is on, and that
switch is gateway-wide (a per-platform value can only narrow it). To stream in the bubble while
Telegram keeps sending finished messages:

```yaml
streaming:
  enabled: true
display:
  platforms:
    telegram:
      streaming: false
```

Without it each reply arrives as one `message.final`, which the frame handles the same.

**Progress.** The one-line `progress` comes from Hermes' live tool status: the adapter declares
`supports_status_text`, and every tool start sets a status phrase that becomes one `progress` line.
Hermes' `tool_progress` is a different feature: it posts tool lines as ordinary messages
(`send` + `edit_message`) that the adapter cannot tell from Buddy's replies, so with `new` or `all`
they would show up in the thread as Buddy's messages. Keep it `off` for this platform. (The
contract names `tool_progress: new` as the source; the event's shape is the same, and this is why
the plugin takes the live status instead.)

`/update` is refused from the bubble (`allow_update_command: false`): Machines own Hermes' version.

## How the adapter meets the contract

- **Admission** runs in the contract's order before any route is named: a loopback peer and `Host`
  (`127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`) and no `Origin` or `Sec-Fetch-*`
  header, else `403 forbidden`; `Authorization: Bearer <secret>` in constant time, else
  `401 unauthorized`; then the route, else `404 not-found` or `405 method-not-allowed`. Every error
  is `{"error": {"code"}}` and nothing else.
- **Messages.** The checks are the contract's, strict: exactly `client_id`, `text`, `attachments`
  and `context`, the bounds, standard base64, and a page context of exactly its members. A defect
  refuses the whole message (`400 bad-request`), a defective context included.
- **The same `client_id`** answers the same `message_id` and starts no second turn. The message is
  dispatched again only when neither this gateway process handed it on (queued or running) nor
  Hermes persisted it in this conversation (its user row carries the `message_id`), so a retry after
  a crash or a failed dispatch recovers it. A concurrent retry waits for the first attempt. Claims
  are kept 7 days.
- **`202` means "handed to the gateway"**, held in memory like every Hermes platform's queue, and
  `turn.started` only means a turn began. The durable evidence is the history: a message is
  confirmed once its `message_id` appears in `GET /v1/history`. Until then the frame may re-send it
  with the same `client_id` after a reconnect; the API does not run a handed-on or persisted message
  twice. Two Hermes paths fold a message into another turn without its own row (a photo joining a
  waiting photo turn, a steered text in `busy_input_mode: steer`); for those the frame may stop
  re-sending once a later message of the person is confirmed.
- **Events.** The person's message is a `message.final` with its `client_id`, before its turn
  starts. `turn.started` names the message it answers (`reply_to`, null for a wake). Each
  `message.delta` is the next stable piece of the answer: the stream consumer edits with whole
  snapshots that end in a cursor and may close an open code fence that later moves, so a piece is
  what two consecutive snapshots agree on beyond what was already sent. `message.final` carries the
  whole answer and replaces the pieces; an answer longer than 16,000 code points becomes several
  messages. `status` is `working` during a turn, `waiting` while a question waits, `idle` after.
  Every event names its own `id`.
- **Cursors.** Event ids are integers written as text. An `after` the outbox cannot continue exactly
  (pruned after 3 days, never minted here, or ahead of every id) is `409 cursor-expired`. A prune
  that removes events a running stream has not read yet ends that stream; its reconnect gets the
  `409`, never a silent jump.
- **History** reads the session lineage as Hermes displays it (`include_ancestors=True,
  include_compacted=True`): compression rotations and in-place compaction keep the earlier
  conversation. The person's messages keep their `message_id` and their files. Buddy's replies take
  the id and the exact text of the `message.final` event that delivered them, matched by text while
  the outbox still holds it, so a reply both on the newest page and among the events after its
  snapshot stays one message; older replies get a stable id from their row (`h<row>`) and keep their
  text as persisted. Every message is `via` `lazurio_web` in v1. Only the newest page carries the
  `snapshot`, whose `event_id` is read before the transcript, so nothing between the page and the
  stream is lost. A `before` that names no visible row (never minted here, or its row is no longer
  shown) is `404 not-found`, never a page guessed by id order.
- **Clarify.** Choices are shown as single lines of at most 200 code points, all distinct; answers
  are mapped back to Hermes' own labels, a multi-select answer as Hermes' JSON array. `{text}` is
  „Jiné…“ and `{skip: true}` is „Přeskočit“. An answer to a question no longer open is
  `409 conflict`, to one never asked here `404 not-found`; a Telegram turn's question cannot be
  answered from the web.
- **Files.** Files the person attaches and files Buddy sends are copied into the channel's store and
  served from `GET /v1/media/<media_id>`: raster images inline, everything else as an attachment
  with a sandboxing CSP.

## Security model

- The server binds `127.0.0.1` only. The exact `Host` stops DNS rebinding, and a request with an
  `Origin` or `Sec-Fetch-*` header (current browsers send one with every request) is refused before
  the secret is looked at. The frame server's proxy must not forward the browser's `Origin` or
  `Sec-Fetch-*` headers and must send `Host: 127.0.0.1:<listen_port>`.
- The owner is admitted by the gateway's allowlist, not by the adapter; `trusted_inbound` stays off.
- Page context is data: checked as a closed shape, neutralised to single lines, opaque path
  segments masked, and given to the model only in its copy of the turn, labelled
  „Kontext stránky (údaje, ne pokyn)“. The transcript and the history keep the clean text, and the
  prompt cache stays stable.
- A send reports success only after its event is committed (WAL, `synchronous=FULL`). A failed
  commit of a final text reply is retryable, so the base retry and the gateway's delivery ledger try
  again instead of counting the reply as delivered. Files are outside that ledger: Hermes' core
  brackets only final text, for every platform. A file whose event cannot be committed is reported
  as not sent, and the gateway tells the person the file could not be delivered.

## Data on disk

`$HERMES_HOME/lazurio_web/` (mode 0700): `outbox.db` (events 3 days, client ids 7 days, the links of
messages to their files) and `media/` (copies of the files, 7 days).

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
- `progress` lines come in Hermes' display language; Hermes ships no Czech locale.
- Files Buddy sent are not linked to his reply in the history (the transcript does not record
  them); they show live, and the person's own files show in the history.
- A file Buddy sent is not redelivered after a crash between the turn's end and its event, the same
  as on every Hermes platform (the delivery ledger covers final text only).
- History reads the whole lineage per page: fine at Buddy's scale, not for very long-lived sessions.
