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
        with self.db:
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
            self.db.executemany('INSERT INTO posts(schedule_id,due,content) VALUES(?,?,?)',
                                [(sid, t.isoformat(), json.dumps(p)) for t, p in zip(times, posts)])
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

    def delete_schedule(self, schedule_id):
        with self.db:
            self.db.execute("DELETE FROM posts WHERE schedule_id=? AND status!='sending'", (schedule_id,))
            retained = self.db.execute('SELECT 1 FROM posts WHERE schedule_id=?', (schedule_id,)).fetchone() is not None
            self._cleanup()
        return retained

    def attention(self):
        return [self._post(r) for r in self.db.execute("SELECT * FROM posts WHERE status IN ('uncertain','failed') ORDER BY due,id")]

    def resolve(self, post_id, retry=False):
        with self.db:
            if retry:
                self.db.execute("UPDATE posts SET status='pending',error=NULL WHERE id=? AND status IN ('uncertain','failed')", (post_id,))
            else:
                self.db.execute("DELETE FROM posts WHERE id=? AND status IN ('uncertain','failed')", (post_id,))
            self._cleanup()

    def claim(self):
        with self.db:
            row = self.db.execute("UPDATE posts SET status='sending' WHERE id=(SELECT id FROM posts WHERE status='pending' AND due<=? ORDER BY due,id LIMIT 1) RETURNING *", (datetime.now(UTC).isoformat(),)).fetchone()
        post = self._post(row)
        if post:
            post['chat_id'] = self.db.execute('SELECT chat_id FROM schedules WHERE id=?', (post['schedule_id'],)).fetchone()[0]
        return post

    def finish(self, post_id):
        with self.db:
            self.db.execute("DELETE FROM posts WHERE id=? AND status='sending'", (post_id,))
            self._cleanup()

    def hold(self, post_id, status, error):
        with self.db:
            self.db.execute('UPDATE posts SET status=?,error=? WHERE id=?', (status, str(error), post_id))

    def close(self):
        self.db.close()
