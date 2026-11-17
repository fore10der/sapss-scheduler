"""SQLite queue. Successful deliveries leave no post history."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def schedule_times(start, end, count):
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('Timezone-aware UTC dates required.')
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1 or count < 1:
        raise ValueError('Choose a valid window and at least one post.')
    if count > minutes + 1:
        raise ValueError('Posts must be at least one minute apart.')
    return [start + timedelta(minutes=(i * minutes + (count - 1) // 2) // (count - 1))
            for i in range(count)] if count > 1 else [start]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            start TEXT NOT NULL, end TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL REFERENCES schedules(id),
            due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            content TEXT NOT NULL, error TEXT);
        CREATE INDEX IF NOT EXISTS due_posts ON posts(status, due);
        ''')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_chat TEXT NOT NULL,
            source_ids TEXT NOT NULL, group_id TEXT, content TEXT,
            status TEXT NOT NULL DEFAULT 'available', stamp TEXT,
            dirty INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_chat, source_ids));
        CREATE UNIQUE INDEX IF NOT EXISTS asset_groups ON assets(source_chat, group_id)
            WHERE group_id IS NOT NULL;
        ''')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS asset_originals (
            asset_id INTEGER PRIMARY KEY REFERENCES assets(id), original TEXT NOT NULL,
            kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS status_notifications (
            asset_id INTEGER NOT NULL REFERENCES assets(id), revision INTEGER NOT NULL,
            state TEXT NOT NULL, message_id INTEGER, reported INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(asset_id, revision));
        UPDATE status_notifications SET state='uncertain' WHERE state='sending';
        ''')
        if 'status_reason' not in [r[1] for r in self.db.execute('PRAGMA table_info(assets)')]:
            self.db.execute('ALTER TABLE assets ADD COLUMN status_reason TEXT')
        if 'text' not in [r[1] for r in self.db.execute('PRAGMA table_info(status_notifications)')]:
            self.db.execute('ALTER TABLE status_notifications ADD COLUMN text TEXT')
        if 'asset_id' not in [r[1] for r in self.db.execute('PRAGMA table_info(posts)')]:
            self.db.execute('ALTER TABLE posts ADD COLUMN asset_id INTEGER REFERENCES assets(id)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_asset ON posts(asset_id) WHERE asset_id IS NOT NULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS asset_status_order (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL UNIQUE REFERENCES assets(id));
        CREATE TRIGGER IF NOT EXISTS order_asset_status
        AFTER UPDATE OF revision ON assets
        WHEN NEW.revision != OLD.revision AND NEW.status IN ('pending','published')
        BEGIN
            DELETE FROM asset_status_order WHERE asset_id=NEW.id;
            INSERT INTO asset_status_order(asset_id) VALUES(NEW.id);
        END;
        ''')
        with self.db:
            # Legacy databases have no event sequence. Seed deterministically once;
            # subsequent transitions use the trigger, independent of wall-clock changes.
            self.db.execute('''INSERT INTO asset_status_order(asset_id)
                SELECT a.id FROM assets a LEFT JOIN posts p ON p.asset_id=a.id
                WHERE a.status IN ('pending','published') AND NOT EXISTS
                    (SELECT 1 FROM asset_status_order o WHERE o.asset_id=a.id)
                ORDER BY a.stamp,p.id,a.id''')
            for row in self.db.execute("SELECT asset_id FROM posts WHERE status='sending'").fetchall():
                self._asset_status(row['asset_id'], 'uncertain')
            self.db.execute("UPDATE posts SET status='uncertain', error='Process stopped during delivery' WHERE status='sending'")

    def _cleanup(self):
        self.db.execute('DELETE FROM schedules WHERE NOT EXISTS (SELECT 1 FROM posts WHERE schedule_id=schedules.id)')

    def create_schedule(self, chat_id, start, end, posts):
        if start <= datetime.now(UTC):
            raise ValueError('Start must still be in the future.')
        times = schedule_times(start, end, len(posts))
        with self.db:
            cur = self.db.execute('INSERT INTO schedules(chat_id,start,end) VALUES(?,?,?)',
                                  (str(chat_id), start.isoformat(), end.isoformat()))
            sid = cur.lastrowid
            for t, p in zip(times, posts):
                aid = p.get('asset_id')
                if aid is not None:
                    changed = self.db.execute("UPDATE assets SET status='pending',stamp=?,status_reason=NULL,dirty=1,revision=revision+1 WHERE id=? AND status='available' AND content IS NOT NULL",
                                              (t.isoformat(), aid)).rowcount
                    if not changed:
                        raise ValueError('Asset is no longer available. Refresh the pool.')
                p = {key: value for key, value in p.items() if key != 'asset_id'}
                self.db.execute('INSERT INTO posts(schedule_id,due,content,asset_id) VALUES(?,?,?,?)',
                                (sid, t.isoformat(), json.dumps(p), aid))
        return sid

    def list_schedules(self):
        return [dict(r) for r in self.db.execute('SELECT s.*, count(p.id) AS count FROM schedules s JOIN posts p ON p.schedule_id=s.id GROUP BY s.id ORDER BY s.start')]

    @staticmethod
    def _post(row):
        if row is None:
            return None
        result = dict(row)
        result['content'] = json.loads(result['content'])
        return result

    def get_posts(self, schedule_id):
        return [self._post(r) for r in self.db.execute('SELECT * FROM posts WHERE schedule_id=? ORDER BY due,id', (schedule_id,))]

    def delete_post(self, post_id):
        """Delete only a pending post; return whether it was deleted."""
        with self.db:
            row = self.db.execute("DELETE FROM posts WHERE id=? AND status='pending' RETURNING asset_id",
                                  (post_id,)).fetchone()
            if row is None:
                return False
            self._asset_status(row['asset_id'], 'available', reason='deleted')
            self._cleanup()
        return True

    def replace_post(self, post_id, content):
        """Replace a pending snapshot, optionally reserving content['asset_id']."""
        with self.db:
            # Acquire the write lock before reading, so another connection cannot
            # claim the post between validation and replacement.
            row = self.db.execute("UPDATE posts SET content=content WHERE id=? AND status='pending' RETURNING *",
                                  (post_id,)).fetchone()
            if row is None:
                return False
            aid = content.get('asset_id')
            payload = json.dumps({k: v for k, v in content.items() if k != 'asset_id'})
            if aid != row['asset_id']:
                if aid is not None:
                    changed = self.db.execute("""UPDATE assets SET status='pending',stamp=?,
                        status_reason='replacement',dirty=1,revision=revision+1
                        WHERE id=? AND status='available' AND content IS NOT NULL""",
                        (row['due'], aid)).rowcount
                    if not changed:
                        raise ValueError('Asset is no longer available. Refresh the pool.')
                self._asset_status(row['asset_id'], 'available', reason='replaced')
            elif aid is not None:
                self._asset_status(aid, 'pending', row['due'], reason='replacement')
            self.db.execute('UPDATE posts SET content=?,asset_id=? WHERE id=?',
                            (payload, aid, post_id))
        return True

    def delete_schedule(self, schedule_id):
        with self.db:
            for row in self.db.execute("SELECT asset_id,status FROM posts WHERE schedule_id=? AND status!='sending'", (schedule_id,)).fetchall():
                self._asset_status(row['asset_id'], 'uncertain' if row['status'] == 'uncertain' else 'available',
                                                   reason='deleted')
            self.db.execute("DELETE FROM posts WHERE schedule_id=? AND status!='sending'", (schedule_id,))
            retained = self.db.execute('SELECT 1 FROM posts WHERE schedule_id=?', (schedule_id,)).fetchone() is not None
            self._cleanup()
        return retained

    def attention(self):
        return [self._post(r) for r in self.db.execute("SELECT * FROM posts WHERE status IN ('uncertain','failed') ORDER BY due,id")]

    def resolve(self, post_id, retry=False):
        with self.db:
            row = self.db.execute("SELECT * FROM posts WHERE id=? AND status IN ('uncertain','failed')", (post_id,)).fetchone()
            if row:
                self._asset_status(row['asset_id'], 'pending' if retry else ('uncertain' if row['status'] == 'uncertain' else 'available'), row['due'] if retry else None)
            if retry:
                self.db.execute("UPDATE posts SET status='pending',error=NULL WHERE id=? AND status IN ('uncertain','failed')", (post_id,))
            else:
                self.db.execute("DELETE FROM posts WHERE id=? AND status IN ('uncertain','failed')", (post_id,))
            self._cleanup()

    def peek_due(self):
        """Inspect the next candidate without preventing edits while rate limited."""
        row = self.db.execute("""SELECT p.*,s.chat_id FROM posts p
            JOIN schedules s ON s.id=p.schedule_id
            WHERE p.status='pending' AND p.due<=? ORDER BY p.due,p.id LIMIT 1""",
            (datetime.now(UTC).isoformat(),)).fetchone()
        return self._post(row)

    def claim(self):
        with self.db:
            row = self.db.execute("UPDATE posts SET status='sending' WHERE id=(SELECT id FROM posts WHERE status='pending' AND due<=? ORDER BY due,id LIMIT 1) RETURNING *", (datetime.now(UTC).isoformat(),)).fetchone()
        post = self._post(row)
        if post:
            post['chat_id'] = self.db.execute('SELECT chat_id FROM schedules WHERE id=?', (post['schedule_id'],)).fetchone()[0]
        return post

    def finish(self, post_id):
        with self.db:
            row = self.db.execute("SELECT asset_id FROM posts WHERE id=? AND status='sending'", (post_id,)).fetchone()
            if row:
                self._asset_status(row['asset_id'], 'published', datetime.now(UTC).isoformat())
                self.db.execute("UPDATE assets SET content=NULL WHERE id=?", (row['asset_id'],))
            self.db.execute("DELETE FROM posts WHERE id=? AND status='sending'", (post_id,))
            self._cleanup()

    def hold(self, post_id, status, error):
        with self.db:
            row = self.db.execute('SELECT asset_id,due FROM posts WHERE id=?', (post_id,)).fetchone()
            if row:
                self._asset_status(row['asset_id'], status, row['due'] if status == 'pending' else None)
            self.db.execute('UPDATE posts SET status=?,error=? WHERE id=?', (status, str(error), post_id))

    def _asset_status(self, asset_id, status, stamp=None, reason=None):
        self.db.execute('UPDATE assets SET status=?,stamp=?,status_reason=?,dirty=1,revision=revision+1 WHERE id=?',
                        (status, stamp, reason, asset_id))

    def add_asset(self, source_chat, source_ids, content, group_id=None):
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO assets(source_chat,source_ids,group_id,content) VALUES(?,?,?,?)',
                                  (str(source_chat), json.dumps(source_ids), group_id, json.dumps(content)))
            if cur.rowcount:

                return cur.lastrowid
        return None

    def assets(self, status='available'):
        return [dict(r) for r in self.db.execute('SELECT * FROM assets WHERE status=? ORDER BY id', (status,))]

    def asset_from_source(self, source_chat, source_id):
        matches = [dict(row) for row in self.db.execute(
            'SELECT * FROM assets WHERE source_chat=?', (str(source_chat),))
            if source_id in json.loads(row['source_ids'])]
        if len(matches) > 1:
            raise ValueError('Source matches multiple assets. Use Asset pool to select explicitly.')
        return matches[0] if matches else None

    def update_asset_content(self, source_chat, source_id, content):
        """Apply a source message edit; return False for unknown or unsafe assets.

        Album edits accept a single-member media payload, not a whole album.
        Only available/pending assets and pending scheduled snapshots are edited.
        """
        with self.db:
            self.db.execute('UPDATE assets SET id=id WHERE source_chat=?', (str(source_chat),))
            asset = self.asset_from_source(source_chat, source_id)
            if not asset or asset['status'] not in ('available', 'pending') or not asset['content']:
                return False
            posts = self.db.execute('SELECT id,status,content FROM posts WHERE asset_id=?',
                                    (asset['id'],)).fetchall()
            if any(p['status'] != 'pending' for p in posts):
                return False
            original = json.loads(asset['content'])
            index = json.loads(asset['source_ids']).index(source_id)

            def edited(snapshot):
                if original.get('kind') != 'album':
                    return {k: v for k, v in content.items() if k != 'asset_id'}
                if len(content.get('media', [])) != 1:
                    raise ValueError('An album edit must contain exactly one media member.')
                snapshot['media'][index] = content['media'][0]
                return snapshot

            updated = json.dumps(edited(original))
            snapshots = [(json.dumps(edited(json.loads(p['content']))), p['id']) for p in posts]
            self.db.execute('UPDATE assets SET content=? WHERE id=?', (updated, asset['id']))
            self.db.executemany("UPDATE posts SET content=? WHERE id=? AND status='pending'", snapshots)
        return True

    def asset_content(self, asset_id):
        row = self.db.execute("SELECT content FROM assets WHERE id=? AND status='available'", (asset_id,)).fetchone()
        if not row or not row[0]:
            raise ValueError('Asset is no longer available.')
        return {**json.loads(row[0]), 'asset_id': asset_id}

    def release_uncertain(self, asset_id):
        with self.db:
            self.db.execute("UPDATE assets SET status='available',stamp=NULL,status_reason=NULL,dirty=1,revision=revision+1 WHERE id=? AND status='uncertain' AND NOT EXISTS (SELECT 1 FROM posts WHERE asset_id=assets.id)", (asset_id,))

    def status_jobs(self):
        return [dict(r) for r in self.db.execute('''SELECT a.* FROM assets a
            LEFT JOIN asset_status_order o ON o.asset_id=a.id
            WHERE a.dirty=1 AND NOT EXISTS (SELECT 1 FROM status_notifications n
            WHERE n.asset_id=a.id AND n.revision=a.revision)
            ORDER BY o.seq,a.id''')]

    def claim_status(self, asset_id, revision, text=None):
        with self.db:
            return bool(self.db.execute('''INSERT OR IGNORE INTO status_notifications
                (asset_id,revision,state,text) SELECT id,revision,'sending',? FROM assets
                WHERE id=? AND revision=? AND dirty=1''', (text, asset_id, revision)).rowcount)

    def status_result(self, asset_id, revision, state, message_id=None):
        with self.db:
            if state == 'retry':
                self.db.execute('DELETE FROM status_notifications WHERE asset_id=? AND revision=?',
                                (asset_id, revision))
            else:
                self.db.execute('''UPDATE status_notifications SET state=?,message_id=?
                    WHERE asset_id=? AND revision=?''', (state, message_id, asset_id, revision))
                self.status_done(asset_id, revision)

    def uncertain_notifications(self):
        return [dict(r) for r in self.db.execute('''SELECT asset_id AS id,revision
            FROM status_notifications WHERE state='uncertain' AND reported=0''')]

    def notification_reported(self, asset_id, revision):
        with self.db:
            self.db.execute('UPDATE status_notifications SET reported=1 WHERE asset_id=? AND revision=?',
                            (asset_id, revision))

    def is_status_reply(self, chat_id, source_id, text, message_id=None):
        # Persisted text belongs to the claimed revision, not the asset's current
        # state. It protects updates arriving before the send result or after restart.
        if message_id is not None and self.db.execute('''SELECT 1 FROM status_notifications n
                JOIN assets a ON a.id=n.asset_id
                WHERE a.source_chat=? AND n.message_id=? AND n.state='sent' ''',
                (str(chat_id), message_id)).fetchone():
            return True
        rows = self.db.execute('''SELECT a.source_ids FROM status_notifications n
            JOIN assets a ON a.id=n.asset_id WHERE a.source_chat=? AND n.text=?
            AND n.state IN ('sending','sent','uncertain')''', (str(chat_id), text))
        if any(json.loads(row['source_ids'])[0] == source_id for row in rows):
            return True
        # Delayed updates from the previous visible-prefix format remain excluded.
        import re
        match = re.match(r'^\[pool-status #(\d+) r(\d+)\]\n', text or '')
        if not match:
            return False
        row = self.db.execute('''SELECT a.source_ids FROM assets a
            JOIN status_notifications n ON n.asset_id=a.id
            WHERE a.source_chat=? AND a.id=? AND n.revision=? AND n.text IS NULL''',
            (str(chat_id), int(match[1]), int(match[2]))).fetchone()
        return bool(row and json.loads(row['source_ids'])[0] == source_id)

    def status_done(self, asset_id, revision):
        with self.db:
            self.db.execute('UPDATE assets SET dirty=0 WHERE id=? AND revision=?', (asset_id, revision))

    def close(self):
        self.db.close()
