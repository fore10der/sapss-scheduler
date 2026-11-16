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

## Tests

```sh
.venv/bin/python -m unittest -v
```

Tests cover spacing, commit validation, queue claiming, restart recovery, deletion during delivery, uncertain transport failures, metadata-only UTC logs/private reports, success removal before reporting, and report failures without duplicate sends. Live Telegram testing requires your credentials and destination.
