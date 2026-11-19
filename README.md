# Telegram post scheduler

Private, single-owner Python bot using the Telegram Bot API only. Supports text, photos, and photo albums with original captions and formatting. Includes actual background delivery, not just a scheduling mock-up.

## Run (macOS / Linux, Python 3.11+)

1. Create a bot through @BotFather. Add it to the destination; for a channel, grant administrator permission to post.
2. Install dependencies:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

3. Fill in the project's `.env` file (excluded from Git):

   ```dotenv
   BOT_TOKEN=your-bot-token
   OWNER_ID=123456789
   CHAT_ID=-1001234567890
   DATABASE_PATH=data/scheduler.sqlite3
   POOL_CHAT_ID=
   ```

   Then run:

   ```sh
   .venv/bin/python main.py
   ```

   `OWNER_ID` is your numeric Telegram user ID, not a username. `CHAT_ID` may also be a public `@channelusername`. No MTProto credentials are needed. The bot automatically loads `.env` alongside `main.py`; existing shell environment variables take precedence.

4. Open a **private chat** with the bot and send `/start`.

## Docker Compose

`Dockerfile` and `compose.yaml` build and run one scheduler instance with standard Docker Compose; no hosting-provider tooling is required.

```sh
cp .env.example .env   # fill in BOT_TOKEN, OWNER_ID, CHAT_ID
docker compose up -d --build
```

Compose passes the host `.env` to the process using `env_file`. `.env` is excluded from the build context and is never copied into the image, so credentials are not baked in. The container runs as a non-root user with working directory `/app`, preserving the default `DATABASE_PATH=data/scheduler.sqlite3`.

The entire `/app/data` directory is the named volume `scheduler-data`. The database, its `.lock` file, and SQLite `-wal`/`-shm` sidecars survive container recreation and `docker compose down`. Run exactly one scheduler per database and bot token; the existing `flock` rejects a second process, so do not scale this service. Back up the volume while the container is stopped; it contains message content and media references.

## Long polling and webhooks

Mode selection is automatic, with no separate selector:

- `WEBHOOK_URL` empty or unset: **long polling**. No public inbound endpoint is needed. Startup removes any previously registered webhook without dropping pending updates.
- `WEBHOOK_URL` nonempty: **webhook mode**. Startup registers the public URL without dropping pending updates. Switching either direction therefore preserves pending updates.

Both modes use the same configuration, handlers, subscribed update types (`message`, `callback_query`, `channel_post`, `edited_channel_post`), scheduler workers, and shutdown cleanup. See `.env.example` for the example block.

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_URL` | empty | Complete public HTTPS endpoint Telegram calls. Empty selects polling. |
| `WEBHOOK_SECRET` | none | Required in webhook mode. 1-256 characters from `A-Z`, `a-z`, `0-9`, `_`, `-`. |
| `WEBHOOK_HOST` | `0.0.0.0` | Local listener bind address. |
| `WEBHOOK_PORT` | `8443` | Local listener port. |
| `WEBHOOK_PATH` | `/webhook` | Local listener path; must match the path in `WEBHOOK_URL`. |

The public endpoint and the listener are distinct. `WEBHOOK_URL` must use HTTPS on port `443`, `80`, `88`, or `8443` per Telegram limits. `WEBHOOK_HOST`/`WEBHOOK_PORT`/`WEBHOOK_PATH` configure the container's plain-HTTP listener; TLS termination, the public port, domain, certificate, and reverse proxy are external infrastructure and are not part of this project. Compose publishes `WEBHOOK_PORT` (default `8443`) for that external proxy; do not expose the listener directly to the internet. In polling mode the published port is unused and can be removed or firewalled off.

`WEBHOOK_SECRET` is registered with Telegram and verified on every incoming request by the webhook server; missing or incorrect `X-Telegram-Bot-Api-Secret-Token` values are rejected with HTTP 403. Polling requires neither webhook variables nor a secret. Invalid webhook configuration (non-HTTPS URL, unsupported public port, path mismatch, bad secret, invalid listener values) exits at startup with a specific error before the database is opened.

## Workflow

- **Create schedule** → enter start, then end as `YYYY-MM-DD HH:mm`.
- **All dates are UTC. Convert your local time before input.** Windows may span days.
- Send text, a photo, or a photo album, or forward originals from the configured pool channel **with attribution intact**. Each album is one post. Video/document albums are not supported.
- Forwarding a pool original keeps the received content and source IDs in the draft. No tracked record or status reply exists until **Commit**.
- A pool source already published by this scheduler is rejected at forward time, regardless of destination, and rechecked at **Commit** and replacement confirmation. Failed or uncertain delivery alone does not mark a source published.
- See acceptance and updated minute-based spacing. Posts include both window endpoints; rounded gaps differ by at most a minute. A single post uses the start. Minimum gap: one minute.
- **Undo last**, **Preview**, **Commit**, or **Cancel** using inline buttons.
- Start must still be in the future at commit. Drafts are in memory and are lost on restart; committed schedules and their source records persist in SQLite.
- **View schedules** → **Details** sends each remaining post's UTC time, original text/caption, and actual media. Flooding the chat is intentional.
- In schedule **Details**, each pending post has **Delete post** and **Replace post** controls. **Delete post** asks for confirmation, removes only that pending post, and releases its tracked source for reuse. Other posts keep their due times; an empty schedule is removed.
- **Replace post** → forward an original from the configured pool channel **with attribution intact** → inspect the forwarded replacement → **Confirm replacement** or **Cancel**. Only forwarded album members are used. Confirmation atomically creates/reserves the replacement's tracked source and releases the old one; the due time stays unchanged. Cancel leaves the scheduled post unchanged. If delivery has already claimed the post, deletion/replacement does nothing; sources already attached to another pending post are rejected. A replacement source this scheduler already published is rejected, including when publication happened after the preview, without changing the scheduled post or reserving anything.
- **Delete** cancels a whole schedule after confirmation. Non-sending queue entries are removed; eligible tracked sources are released, while uncertain sources remain blocked for manual review. An in-flight send is not canceled or released. Published messages are never deleted.
- Main reply-keyboard labels are reserved navigation text, not post content.

## Delivery and duplicate prevention

Keep the application running under a process supervisor for continuous delivery. Due posts are atomically claimed and sent sequentially. Overdue pending posts become eligible after recovery in scheduled order; shared rate budgets may delay their delivery. Explicit Telegram rate-limit responses are respected.

The default policy smoothly admits 20 request/message units per second globally, with at least 1.1 seconds per message unit to a single chat. Photo albums reserve one unit per photo. Negative chat IDs (including channels) and unresolved usernames additionally use a conservative rolling budget of 20 messages per 60 seconds. Callback answers and other non-message API calls consume global capacity without consuming a chat's message budget. Telegram `RetryAfter` pauses all outgoing traffic for its cooldown plus 0.1 seconds; each API call allows three retries after its initial attempt. Network errors are not automatically retried. Already in-flight requests cannot be recalled. These limits coordinate one running bot process, not other clients using the token, and do not guarantee Telegram will never return 429.

Both application factories attach the shared `rate_limiter.TelegramRateLimiter()` (a PTB `BaseRateLimiter`, with no constructor arguments). This integration requires `rate_limiter.py` and `Store.peek_due()`: the latter returns the next due pending post, including `chat_id` and `content`, without claiming or changing it. The publication worker inspects that candidate, then awaits `wait_for_readiness(chat_id, cost=...)` before claiming; albums use their photo count and other posts use cost 1. Readiness does not reserve capacity. The subsequent atomic claim rechecks the current pending queue, so edits/deletions during the wait are respected; the actual request still passes through the limiter. Lightweight test bots without a PTB limiter skip readiness.

Request priority is an optional `rate_limit_args={"priority": integer}` dictionary, with higher numbers preferred: scheduled content uses **10**, ordinary requests (including owner reports and startup messages) default to **0**, and content previews/bulk sends default to **-10**. The limiter infers callback-answer priority **20** from the endpoint. `send_content()` accepts an override and passes it through text, photo, and album sends, including single-photo albums. These are scheduling policies, not guarantees of exact publication time, strict ordering, or freedom from Telegram rate limits. Readiness can become stale as other requests run, and cannot preempt an in-flight request. No aiogram dependency or PTB rate-limiter extras are required.

The Bot API does **not** support exactly-once sends. A timeout or crash can happen after Telegram accepts a request but before we record success. Such posts become **uncertain**, are never automatically resent, and appear under **Needs attention**. Check the destination, then **Dismiss** or confirm **Retry** (which can duplicate a previously delivered post). Explicit permission/content rejections are held as failed for manual correction/retry. Failed notifications do not remove attention entries.

Each delivery attempt writes an INFO-level metadata log, followed by its result (success, failure, uncertain, or rate limited). Log prefixes use UTC. Logs and private reports to `OWNER_ID` include actual UTC server time, scheduled due time, attempt time, schedule/post IDs, destination ID (or configured channel username), and lateness in seconds measured at attempt start. These use the server clock, so keep the host clock synchronized. Reports never go to the destination chat. Start a private chat with the bot first so it can message you. Rate-limit reports explain that an automatic retry follows the cooldown; uncertain and rejected deliveries require manual attention. Logs/reports omit post content, bot tokens, and raw exception messages; error diagnostics use exception class names only. Treat logs as private because they contain destination and owner identifiers.

Polling/application network failures use a registered error handler instead of PTB's repeated “No error handlers are registered” traceback. The first occurrence of each error class/reason logs one concise warning; repeats are suppressed for 60 seconds (monotonic time), and the next warning includes `suppressed=N`. TLS diagnostics classify certificate-verification/weak-key failures without printing raw exceptions, request URLs, or tokens. Unexpected application errors retain stack frames with sanitized exception details; no blanket Telegram logger suppression is applied. The convenience `build_application()` factory registers the same handler. PTB retains its polling retry/backoff; this logging change does not retry publications or send extra owner messages. PTB may retry polling timeouts silently.

If you see `Telegram polling TLS verification failed (certificate key too weak)`, check proxy/VPN HTTPS interception and the certificate configuration used by your Python environment. This indicates a certificate/security problem in the connection path, not a scheduling error. Fix the network/certificate configuration with your network administrator or certificate provider. Do **not** disable TLS verification or lower OpenSSL security settings to hide it. The concise handler covers runtime errors routed through Application; fatal initialization/bootstrap errors may still be reported separately by PTB.

Successful queue removal happens **before** the success report. Report failures are logged separately and never retry an already delivered post or change its outcome; reports themselves are best-effort and are not queued for retry. A crash during delivery is recovered as uncertain on restart (see Needs attention), but cannot guarantee a private report.

After confirmed success, post content and queue entries are removed; the empty schedule is removed too. There is no sent-post history or destination post-publication editing; pool-source tagging is described below. SQLite deletion is logical deletion, not a guarantee of forensic erasure from disk/backups.

Only one process may use the database (local advisory file lock). Do not run another instance with the same bot token or restore an old database backup blindly: replaying old pending records can duplicate publications. Persist the database directory across deployments; protect it and backups because they contain message content and media references.

Album collection uses a 1.5-second inactivity window because Telegram provides no completion event. Commit is blocked while an album is arriving. Exceptionally delayed album items can be split; inspect the preview before committing. Photos are referenced by Telegram `file_id`; files are not downloaded locally.

## Pool forwarding and source tracking

Set `POOL_CHAT_ID` to a numeric channel ID (for example `-1001234567890`), or leave it blank to disable forwarded pool scheduling. Keep the pool separate from the destination. Add the bot as a pool-channel administrator with permission to post messages; destination posting permission is still required. Source tagging requires **Edit messages** (`can_edit_messages`) permission to edit originals posted by others in the channel. Delete permission, a discussion group, privacy-mode changes, channel-history access, and MTProto are not needed.

There is no automatic pool import. New `channel_post` updates are ignored, so unrelated channel posts never become library records. A post can be scheduled even if it was published before the bot joined the channel, because no prior ingestion is required.

To create a schedule, choose **Create schedule**, enter the window, then forward one or more originals from the configured pool channel **with their attribution intact** while the draft is open. Telegram supplies `MessageOriginChannel`; the bot checks the channel ID and keeps the original channel/message IDs as draft-local source references. Forwarding, Preview, Undo, and Cancel do not create tracked records and do not change pool statuses. Commit is the first persistent step: it saves the schedule and content snapshot, creates or reuses one tracked source record per forwarded post, marks it **pending**, and lets the existing follow-up worker send the status reply. Abandoning a draft leaves the original channel posts and their statuses untouched. A source already attached to another pending queue entry is rejected atomically. A pool source with a recorded confirmed publication is rejected when forwarded again, regardless of its previous destination; forwarding checks only read the durable publication history.

Forwarding preserves the received text, caption (including an empty caption), photo `file_id`, and formatting entities. Albums use the same 1.5-second inactivity window as ordinary uploads and block Commit until collection finishes. Only forwarded album members are scheduled; the bot never fetches or invents missing members. Forward every member if the complete album is required. For source-edit tracking, an album's tracked IDs are the forwarded member IDs in message order. If any received member is already published in that channel, the whole received album is rejected, including single-member, subset, and differently grouped forwards.

Attribution is required for pool tracking. Forwards from another channel and forwards whose source Telegram hides are rejected with guidance; they are not guessed from `file_id`, content, or replies. A message with no forwarding metadata at all remains a standalone post and is accepted without source tracking. Same-message duplicates inside one draft are rejected, except that later members of the active forwarded album update the same draft album.

In schedule **Details**, **Replace post** now previews the forwarded replacement content directly. Confirm creates the replacement's tracked source record in the same transaction, reserves it as pending, and releases the old record as available. No tracking record is created by the preview or by Cancel. Confirmation rechecks publication history atomically, so a source published after the preview is rejected without changing the scheduled post or source records. Delete post and schedule deletion release eligible tracked records; in-flight deliveries are never canceled.

On each startup/restart, after initialization and before starting workers, the bot makes one best-effort send to `POOL_CHAT_ID`: `Scheduler started` followed by `Server time: YYYY-MM-DD HH:mm:ss UTC` on the next line. With no pool configured, it logs that the notification is disabled; nothing is sent to the destination. Failures log only error class names and attempt a useful private owner report without blocking workers. Neither send is automatically retried, including ambiguous timeout outcomes. That exact two-line format is treated as a status message, not source content, including delayed updates after restart.

A tracked source's `edited_channel_post` updates refresh recorded content and linked pending queue snapshots when the record is available or pending. Text, captions, formatting entities, and photo references are updated; an album edit replaces only the exact source-indexed member, preserving the others. Updates stop once a linked queue entry is claimed for delivery. Unknown sources are ignored and never create new records, so source edits cannot repopulate an asset library. In-memory drafts are not refreshed by source edits.

Source-channel status replies show **pending** after Commit, with scheduled UTC, and **published** after confirmed delivery, with recorded success UTC. Deleting a pending post or canceling a schedule releases eligible tracked sources with an **available — removed** reply; replacement gives the old source an **available — replaced** reply and the new source a **pending replacement** reply. Other available transitions, **failed**, and **uncertain** remain database/private UI states; maintenance marks those revisions clean without sending a source reply. Private reports for uncertain notification outcomes are preserved. Sources are never deleted. Uncertain delivery and crash recovery keep the linked source blocked; inspect the destination before retrying through **Needs attention**.

Status replies have no visible technical prefix. Confirmed sent-message IDs and exact text snapshots persisted with each revision claim prevent recursive processing, including updates arriving before the send result is recorded or after the source advances to a newer status/restart. Before a sent-message ID is known, only the exact claimed text replying to that source's first message is excluded; identical standalone text or replies to unrelated targets remain ordinary channel posts. Delayed legacy replies with the old `[pool-status #ASSET_ID rREVISION]` header are still recognized against their original claims.

Reply attempts follow a persistent global status-transition sequence, not asset IDs or source-channel message order. Within a committed schedule, **pending** replies follow its post order (due time, then post ID). **Published** replies follow the order in which deliveries are confirmed, including after restart and when timestamps tie or the clock changes. Across schedules and mixed backlogs, transition order is preserved for each source's latest eligible revision. Existing databases seed ordering from stored timestamps and post IDs; exact historical completion order cannot be reconstructed. Missing targets, uncertain outcomes, and later retries can leave gaps or delayed replies; they do not cause publication to be resent.

Visible status replies use these exact forms (timestamps below are examples):

```text
⏳ Pending — 2026-11-20 12:00 UTC
```

```text
⏳ Pending (replacement for another scheduled asset) — 2026-11-20 12:00 UTC
```

```text
♻️ Available — removed from the schedule; ready to schedule again.
```

```text
♻️ Available — replaced in the schedule by another asset; ready to schedule again.
```

Published replies have exactly one line; `#to_delete` is no longer included in the reply:

```text
✅ Published — 2026-11-20 12:00 UTC
```

After confirmed delivery, the bot appends `\n\n#to_delete` to nonempty original pool text/captions; an empty caption becomes only `#to_delete`. Every album member is tagged, including members with no caption. Existing text/caption formatting entities and their offsets are preserved, and media and destination messages remain unchanged. An existing tag-only value or trailing `\n\n#to_delete` is not appended again. Conservative UTF-16 length validation enforces 4096 units for text and 1024 for captions: content is never truncated, and over-limit edits fail and are reported.

Durable source-edit jobs are created only when delivery finishes with confirmed success, separately from status replies. They use the latest stored source snapshot at delivery completion, not the forwarded/queued destination content. There is no API fetch of the current original: manual source edits can race with tagging and be overwritten by that stored snapshot. Avoid manually editing published sources until tagging is complete.

Original text/caption/entity snapshots remain in source-edit job payloads while pending, failed, or uncertain (and while an edit is in flight); a job's payload is cleared only when it is done. Explicit `RetryAfter` responses leave the job pending for retry after the cooldown. Rejected edits (including permission and length failures) become failed; ambiguous errors, cancellation, and interrupted in-flight edits recovered after restart become uncertain. Failed/uncertain edits are not automatically retried and require manual inspection. The owner is notified once per job after a successful report, with a durable acknowledgement; failed report sends remain eligible, and a crash before acknowledgement can duplicate a report. Neither source-edit failures nor reports retry destination publication. Previously published assets with no retained original get no retroactive source-edit jobs.

`#to_delete` is only a marker: there is no automatic cleanup/deletion of pool originals or status replies. Video ingestion/delivery is not supported. All status replies target the original source post (the first member by message ID for an album), never a previous status reply, and have no standalone fallback. Pending uses the stored scheduled time; published uses the stored confirmed-delivery time. Timezone-aware timestamps are converted to UTC and displayed to minute precision, without seconds, ISO offsets, asset IDs, or revision headers. IDs/revisions remain internal maintenance metadata; verbose private delivery diagnostics are unchanged. Status-reply maintenance is durable and separate from publication, source-edit jobs, and destination availability. For status replies only, definitive permission/content rejections retry approximately every 30 seconds, including after restart; explicit rate-limit cooldowns are respected. Private reports identify the asset/revision and error class and suggest checking posting permissions/source targets, without exposing raw API errors or content. A confirmed missing reply target is skipped, never sent as a detached message. Each revision is persistently claimed before sending. Network failures, unknown outcomes, cancellation, or crashes must not cause blind retries: interrupted claims recover as uncertain, trigger a private check-the-pool notification, and are never automatically resent. Newer revisions remain eligible. Private uncertain reports retry if reporting fails, but a crash may duplicate a private report, not the pool reply. Intermediate states can be coalesced before maintenance; replies can temporarily lag database state. No status-maintenance outcome retries publication. The published asset's content is cleared, but source-edit job snapshots are retained until each job is done as described above. Confirmed publication is also stored separately from mutable tracked-source status, so cleanup or resetting a source to a reusable status cannot make it eligible again. The legacy `asset_originals` table and its existing records are preserved for backward-compatible migration but are no longer used or populated. Existing schedules and delivery deduplication are unchanged.

Draft album collection uses a 1.5-second inactivity heuristic because Telegram provides no completion event. Exceptionally late album items can be split into another post; inspect Preview before Commit. Only received members are scheduled. Drafts and incoming album buffers are not persistent; committed schedules, their source records, and status jobs are. Existing databases remain usable: previously committed schedules, pending deliveries, tracked sources, and follow-up jobs are preserved, and no fresh database is required. The durable publication-history table is created and backfilled from existing confirmed `published` records at startup; history that was not recorded is not invented.

## Tests

```sh
.venv/bin/python -m unittest -v
```

Tests cover spacing, commit validation, queue claiming, restart recovery, deletion during delivery, uncertain transport failures, metadata-only UTC logs/private reports, success removal before reporting, report failures without duplicate sends, forwarded-source validation, already-published source rejection at forward/Commit/replacement time, durable publication evidence across restart and status resets, draft-versus-Commit tracking, forwarded albums with only received members, removal of automatic ingestion, source edits, status follow-ups after restart, legacy database compatibility, webhook/polling mode selection, webhook configuration validation, secret-token rejection, and second-instance database locking. Live Telegram testing requires your credentials and destination.
