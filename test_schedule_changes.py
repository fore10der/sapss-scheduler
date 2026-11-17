import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import Store


class ScheduleChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.db'
        self.store = Store(self.path)
        self.start = datetime.now(timezone.utc) + timedelta(hours=1)
        self.content = {'kind': 'text', 'text': 'original', 'entities': []}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def asset(self, source_id=1, content=None, ids=None):
        return self.store.add_asset('-100', ids or [source_id], content or self.content)

    def schedule(self, content):
        sid = self.store.create_schedule('42', self.start, self.start + timedelta(hours=1), [content])
        return sid, self.store.get_posts(sid)[0]

    def row(self, aid):
        return dict(self.store.db.execute('SELECT * FROM assets WHERE id=?', (aid,)).fetchone())

    def test_delete_pending_releases_and_cleans_up(self):
        aid = self.asset()
        sid, post = self.schedule(self.store.asset_content(aid))
        self.assertTrue(self.store.delete_post(post['id']))
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.row(aid)['status'], 'available')
        self.assertEqual(self.row(aid)['status_reason'], 'deleted')
        self.assertFalse(self.store.delete_post(post['id']))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.status_jobs()[0]['status_reason'], 'deleted')
        self.schedule(self.store.asset_content(aid))
        self.assertIsNone(self.row(aid)['status_reason'])

    def test_replace_preserves_slot_and_durable_reasons(self):
        old, new = self.asset(), self.asset(2)
        sid, post = self.schedule(self.store.asset_content(old))
        self.assertTrue(self.store.replace_post(post['id'], self.store.asset_content(new)))
        result = self.store.get_posts(sid)[0]
        self.assertEqual((result['id'], result['due']), (post['id'], post['due']))
        self.assertEqual(result['asset_id'], new)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.row(old)['status_reason'], 'replaced')
        self.assertEqual(self.row(new)['status_reason'], 'replacement')
        self.assertEqual(self.row(new)['stamp'], post['due'])

    def test_unavailable_replacement_rolls_back(self):
        old, new = self.asset(), self.asset(2)
        replacement = self.store.asset_content(new)
        sid, post = self.schedule(self.store.asset_content(old))
        self.schedule(replacement)
        before = self.row(old)
        with self.assertRaises(ValueError):
            self.store.replace_post(post['id'], replacement)
        self.assertEqual(self.row(old), before)
        self.assertEqual(self.store.get_posts(sid)[0], post)

    def test_replacement_write_failure_rolls_back_reservation_and_release(self):
        old, new = self.asset(), self.asset(2)
        sid, post = self.schedule(self.store.asset_content(old))
        replacement = self.store.asset_content(new)
        before = (self.row(old), self.row(new))
        self.store.db.execute('''CREATE TRIGGER reject_replacement BEFORE UPDATE OF asset_id ON posts
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_post(post['id'], replacement)
        self.assertEqual((self.row(old), self.row(new)), before)
        self.assertEqual(self.store.get_posts(sid)[0], post)

    def test_same_asset_and_standalone_replacements(self):
        aid = self.asset()
        sid, post = self.schedule(self.store.asset_content(aid))
        self.assertTrue(self.store.replace_post(post['id'], {**self.content, 'asset_id': aid}))
        self.assertEqual(self.row(aid)['status'], 'pending')
        self.assertTrue(self.store.replace_post(post['id'], self.content))
        self.assertIsNone(self.store.get_posts(sid)[0]['asset_id'])
        self.assertEqual(self.row(aid)['status'], 'available')
        self.assertTrue(self.store.delete_post(post['id']))

    def test_non_pending_posts_are_untouched(self):
        for status in ('sending', 'uncertain', 'failed'):
            with self.subTest(status=status):
                aid = self.asset(10 + len(self.store.list_schedules()))
                sid, post = self.schedule(self.store.asset_content(aid))
                self.store.hold(post['id'], status, 'test')
                before = self.store.get_posts(sid)
                self.assertFalse(self.store.delete_post(post['id']))
                self.assertFalse(self.store.replace_post(post['id'], self.content))
                self.assertEqual(self.store.get_posts(sid), before)
        self.assertFalse(self.store.replace_post(-1, self.content))

    def test_delete_schedule_preserves_sending_and_uncertain_safety(self):
        for index, status in enumerate(('sending', 'uncertain', 'pending', 'failed')):
            aid = self.asset(20 + index)
            sid, post = self.schedule(self.store.asset_content(aid))
            self.store.hold(post['id'], status, 'test')
            self.assertEqual(self.store.delete_schedule(sid), status == 'sending')
            expected = 'uncertain' if status == 'uncertain' else ('sending' if status == 'sending' else 'available')
            self.assertEqual(self.row(aid)['status'], expected)

    def test_source_edit_updates_pending_snapshot_and_available_asset(self):
        aid = self.asset()
        edited = {**self.content, 'text': 'edited'}
        self.assertTrue(self.store.update_asset_content('-100', 1, edited))
        self.assertEqual(self.store.asset_content(aid)['text'], 'edited')
        sid, post = self.schedule(self.store.asset_content(aid))
        edited['text'] = 'edited again'
        self.assertTrue(self.store.update_asset_content('-100', 1, edited))
        result = self.store.get_posts(sid)[0]
        self.assertEqual(result['content'], edited)
        self.assertEqual(result['due'], post['due'])
        self.store.hold(post['id'], 'sending', 'test')
        self.assertFalse(self.store.update_asset_content('-100', 1, self.content))
        self.assertEqual(self.store.get_posts(sid)[0]['content'], edited)
        self.assertFalse(self.store.update_asset_content('-100', 999, edited))

    def test_album_edit_changes_only_source_index(self):
        album = {'kind': 'album', 'media': [{'file_id': 'a'}, {'file_id': 'b'}]}
        aid = self.asset(content=album, ids=[9, 3])
        sid, post = self.schedule(self.store.asset_content(aid))
        edit = {'kind': 'photo', 'media': [{'file_id': 'new', 'caption': 'edited'}]}
        self.assertTrue(self.store.update_asset_content('-100', 3, edit))
        expected = {'kind': 'album', 'media': [album['media'][0], edit['media'][0]]}
        self.assertEqual(self.store.get_posts(sid)[0]['content'], expected)
        self.assertEqual(json.loads(self.row(aid)['content']), expected)
        with self.assertRaises(ValueError):
            self.store.update_asset_content('-100', 9, {'kind': 'album', 'media': []})
        self.assertEqual(json.loads(self.row(aid)['content']), expected)

    def test_status_reason_migration_preserves_existing_data(self):
        aid = self.asset()
        sid, post = self.schedule(self.store.asset_content(aid))
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE assets DROP COLUMN status_reason')
        self.store = Store(self.path)
        self.assertIsNone(self.row(aid)['status_reason'])
        self.assertEqual(self.store.get_posts(sid)[0], post)


if __name__ == '__main__':
    unittest.main()
