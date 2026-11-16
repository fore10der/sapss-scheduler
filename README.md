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

## Workflow

- **Create schedule** → enter start, then end as `YYYY-MM-DD HH:mm`.
- **All dates are UTC. Convert your local time before input.** Windows may span days.
- Send text, a photo, or a photo album. Each album is one post. Video/document albums are not supported.
- See acceptance and updated minute-based spacing. Posts include both window endpoints; rounded gaps differ by at most a minute. A single post uses the start. Minimum gap: one minute.
- **Undo last**, **Preview**, **Commit**, or **Cancel** using inline buttons.
- Start must still be in the future at commit. Drafts are in memory and are lost on restart; committed schedules persist in SQLite.
- **View schedules** → **Details** sends each remaining post's UTC time, original text/caption, and actual media. Flooding the chat is intentional.
- **Delete** cancels a whole schedule after confirmation. Pending and uncertain entries are discarded; an in-flight send is not canceled. Published messages are never deleted.
- Main reply-keyboard labels are reserved navigation text, not post content.

## Delivery and duplicate prevention

Keep the application running under a process supervisor for continuous delivery. Due posts are atomically claimed and sent sequentially. Overdue pending posts send immediately after recovery, in scheduled order; they may be close together. Explicit Telegram rate-limit responses are respected.

The Bot API does **not** support exactly-once sends. A timeout or crash can happen after Telegram accepts a request but before we record success. Such posts become **uncertain**, are never automatically resent, and appear under **Needs attention**. Check the destination, then **Dismiss** or confirm **Retry** (which can duplicate a previously delivered post). Explicit permission/content rejections are held as failed for manual correction/retry. Failed notifications do not remove attention entries.

Each delivery attempt writes an INFO-level metadata log, followed by its result (success, failure, uncertain, or rate limited). Log prefixes use UTC. Logs and private reports to `OWNER_ID` include actual UTC server time, scheduled due time, attempt time, schedule/post IDs, destination ID (or configured channel username), and lateness in seconds measured at attempt start. These use the server clock, so keep the host clock synchronized. Reports never go to the destination chat. Start a private chat with the bot first so it can message you. Rate-limit reports explain that an automatic retry follows the cooldown; uncertain and rejected deliveries require manual attention. Logs/reports omit post content, bot tokens, and raw exception messages; error diagnostics use exception class names only. Treat logs as private because they contain destination and owner identifiers.

Successful queue removal happens **before** the success report. Report failures are logged separately and never retry an already delivered post or change its outcome; reports themselves are best-effort and are not queued for retry. A crash during delivery is recovered as uncertain on restart (see Needs attention), but cannot guarantee a private report.

After confirmed success, post content and queue entries are removed; the empty schedule is removed too. There is no sent-post history or post-publication editing. SQLite deletion is logical deletion, not a guarantee of forensic erasure from disk/backups.

Only one process may use the database (local advisory file lock). Do not run another instance with the same bot token or restore an old database backup blindly: replaying old pending records can duplicate publications. Persist the database directory across deployments; protect it and backups because they contain message content and media references.

Album collection uses a 1.5-second inactivity window because Telegram provides no completion event. Commit is blocked while an album is arriving. Exceptionally delayed album items can be split; inspect the preview before committing. Photos are referenced by Telegram `file_id`; files are not downloaded locally.

## Optional asset pool

Set `POOL_CHAT_ID` to a numeric channel ID (for example `-1001234567890`), or leave it blank to disable ingestion. Keep the pool separate from the destination. Through @BotFather create/configure the bot as usual; add it as a pool-channel administrator with permission to post messages, and retain destination posting permission. Edit/delete permissions are not needed for status maintenance. No discussion group, group privacy-mode change, or MTProto access is needed. Only new `channel_post` updates from that exact pool are imported; edits are ignored. Status replies use a reserved `[pool-status #ASSET_ID rREVISION]` prefix and a persisted per-revision claim plus the known source reply target to prevent recursive ingestion, including updates arriving before the send result is recorded. Do not use that reserved prefix for source content. No historical import is performed.

On each startup/restart, after initialization and before starting workers, the bot makes one best-effort send to `POOL_CHAT_ID`: `Scheduler started` followed by `Server time: YYYY-MM-DD HH:mm:ss UTC` on the next line. With no pool configured, it logs that the notification is disabled; nothing is sent to the destination. Failures log only error class names and attempt a useful private owner report without blocking workers. Neither send is automatically retried, including ambiguous timeout outcomes. The exact two-line startup format is reserved and excluded from pool ingestion regardless of sender, including delayed updates after restart (channel posts may conceal bot identity). Do not use that exact format as source content; other text beginning with “Scheduler started” remains importable.

In private chat, **Asset pool** lists text metadata only (asset ID, type, availability, and album photo count), with **Add to draft** buttons when a draft window is ready. It does not automatically send original text, photos, or albums. Explicit **Preview** and schedule **Details** still send full content. Create a draft window, then select **Add to draft** for as many assets as needed. An album counts as one post. Selection does not reserve an asset: commit atomically claims all selected assets or rolls back the whole schedule if any is unavailable. Delivery uses a frozen clean original, never the status marker. The pool also records every source message ID for albums. Each status notification is a new text reply to the first item (by message ID), sent directly in the same pool channel using `ReplyParameters(..., allow_sending_without_reply=False)`. Source text, captions, entities, and media are never edited or deleted.

You can also forward an imported pool source into a ready private draft **with its source attribution intact**. The bot matches Telegram's `MessageOriginChannel` channel ID and message ID against stored source IDs, attaches the asset ID, and uses the clean stored original. Forwarding any album member selects the **entire stored album**, not just that photo; subsequent members/repeated forwards and Add to draft selections are deduplicated. The acceptance message explicitly identifies the asset/full album; inspect **Preview** before committing. Unavailable assets and unknown pool source IDs are rejected rather than silently scheduled as independent copies. Commit still atomically enforces one active queue entry per asset, including races between drafts.

Telegram can remove source attribution when forwarding without an author or copying content. Hidden-origin forwards prompt you to use **Asset pool → Add to draft**. When no forwarding metadata exists at all, the bot cannot distinguish a copy from an ordinary upload: it remains a standalone post and the acceptance notice says it is not linked. Media `file_id`, captions, text, and replies to bot previews/details are not used to guess an asset identity. A private reply is not an asset selection; use **Add to draft** to track that source. Existing standalone schedules are not retroactively linked or resent.

Source-channel status replies show only **pending** after schedule commit, with scheduled UTC, and **published** after confirmed delivery, with recorded success UTC. **Available**, **failed**, and **uncertain** remain database/private UI states; maintenance marks those revisions clean without claiming or sending a source reply. Private reports for uncertain notification outcomes are preserved. Sources are never deleted. Cancellation returns unsent/failed assets to available; in-flight sends remain queued. Uncertain delivery and crash recovery block reuse. Dismissing/deleting an uncertain queue entry does not clear that block: inspect the destination, then explicitly accept the duplicate risk under **Asset pool** to release the asset. An active uncertain queue entry must first be dismissed, or explicitly retried through Needs attention.

Reply attempts follow a persistent global status-transition sequence, not asset IDs or source-channel message order. Within a committed schedule, **pending** replies follow its post order (due time, then post ID), even when assets were selected in reverse source order. **Published** replies follow the order in which deliveries are confirmed, including after restart and when timestamps tie or the clock changes. Across schedules and mixed pending/published backlogs, transition order is preserved for each asset's latest eligible revision. Existing databases seed ordering from stored timestamps and post IDs; exact historical completion order cannot be reconstructed. Missing targets, uncertain outcomes, and later retries can leave gaps or delayed replies; they do not cause publication to be resent.

Status replies retain the `[status · timestamp UTC]` format (timestamp when applicable), with an asset ID/revision header. Maintenance is durable and separate from publication and destination availability. Definitive permission/content rejections retry approximately every 30 seconds, including after restart; explicit rate-limit cooldowns are respected. Private reports identify the asset/revision and error class and suggest checking posting permissions/source targets, without exposing raw API errors or content. A confirmed missing reply target is skipped, never sent as a detached message. Each revision is persistently claimed before sending. Network failures, unknown outcomes, cancellation, or crashes must not cause blind retries: interrupted claims recover as uncertain, trigger a private check-the-pool notification, and are never automatically resent. Newer revisions remain eligible. Private uncertain reports retry if reporting fails, but a crash may duplicate a private report, not the pool reply. Intermediate states can be coalesced before maintenance; replies can temporarily lag database state. No status-maintenance outcome retries publication. After publication, new assets retain only source identifiers and lifecycle/notification metadata, not a separate copy of original text/caption/entities. The legacy `asset_originals` table and its existing records are preserved for backward-compatible migration but are no longer used or populated. Existing schedules and delivery deduplication are unchanged.

Pool album collection uses the same 1.5-second inactivity heuristic as drafts, independently per chat/group. Exceptionally late items after finalization are ignored rather than changing an already-frozen asset; incomplete album buffers can be lost on shutdown/crash. Review assets before scheduling. Drafts and incoming album buffers are not persistent; completed assets and status jobs are.

## Tests

```sh
.venv/bin/python -m unittest -v
```

Tests cover spacing, commit validation, queue claiming, restart recovery, deletion during delivery, uncertain transport failures, metadata-only UTC logs/private reports, success removal before reporting, and report failures without duplicate sends. Live Telegram testing requires your credentials and destination.
