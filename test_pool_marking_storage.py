import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import Store


TEXT = {'kind': 'text', 'text': '😀original',
        'entities': [{'type': 'bold', 'offset': 2, 'length': 8}]}
ALBUM = {'kind': 'album', 'media': [
    {'file_id': 'a', 'caption': '😀original', 'caption_entities': TEXT['entities']},
    {'file_id': 'b', 'caption': '', 'caption_entities': []}]}


class PoolMarkingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.path)

    def schedule(self, content=TEXT, source_ids=None):
        aid = self.store.add_asset(-100, source_ids or [10], content)
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        sid = self.store.create_schedule(123, start, start + timedelta(hours=1),
                                         [self.store.asset_content(aid)])
        post = self.store.get_posts(sid)[0]
        with self.store.db:
            self.store.db.execute("UPDATE posts SET due='2000-01-01T00:00:00+00:00'")
        return aid, post['id']

    def test_only_confirmed_finish_enqueues_and_preserves_source_not_snapshot(self):
        aid, pid = self.schedule()
        self.store.finish(pid)
        self.assertEqual(self.store.marking_jobs(), [])
        self.assertIsNotNone(self.store.claim())
        with self.store.db:
            self.store.db.execute('UPDATE posts SET content=? WHERE id=?',
                                  (json.dumps({'kind': 'text', 'text': 'snapshot'}), pid))
        self.store.finish(pid)
        self.store.finish(pid)
        jobs = self.store.marking_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['payload'], TEXT)
        self.assertEqual((jobs[0]['asset_id'], jobs[0]['source_id'], jobs[0]['source_chat'],
                          jobs[0]['kind'], jobs[0]['state']),
                         (aid, 10, '-100', 'text', 'pending'))
        self.assertIsNone(self.store.db.execute('SELECT content FROM assets WHERE id=?',
                                               (aid,)).fetchone()[0])
        self.assertFalse(self.store.update_asset_content(-100, 10, TEXT))
        self.assertIsNone(self.store.add_asset(-100, [10], TEXT))
        self.restart()
        self.assertEqual(self.store.marking_jobs()[0]['payload'], TEXT)

    def test_every_album_source_and_single_photo_preserve_entities(self):
        for content, ids in ((ALBUM, [10, 11]),
                             ({'kind': 'photo', 'media': [ALBUM['media'][0]]}, [20])):
            aid, pid = self.schedule(content, ids)
            self.store.claim()
            self.store.finish(pid)
            jobs = [j for j in self.store.marking_jobs() if j['asset_id'] == aid]
            self.assertEqual([j['source_id'] for j in jobs], ids)
            for job, media in zip(jobs, content['media']):
                self.assertEqual(job['kind'], 'photo')
                self.assertEqual(job['payload'], {'kind': 'photo', 'media': [media]})

    def test_hold_and_restart_uncertain_publication_do_not_enqueue(self):
        _, pid = self.schedule()
        self.store.claim()
        for state in ('pending', 'failed', 'uncertain'):
            self.store.hold(pid, state, 'not confirmed')
            self.store.finish(pid)
            self.assertEqual(self.store.marking_jobs(), [])
        self.store.hold(pid, 'pending', 'retry')
        self.store.claim()
        self.restart()
        self.assertEqual(self.store.attention()[0]['status'], 'uncertain')
        self.assertEqual(self.store.marking_jobs(), [])

    def test_claim_recovery_explicit_retry_and_terminal_payload_cleanup(self):
        _, pid = self.schedule()
        self.store.claim()
        self.store.finish(pid)
        job = self.store.marking_jobs()[0]
        self.assertTrue(self.store.claim_mark(job['id']))
        other = Store(self.path)
        try:
            self.assertFalse(other.claim_mark(job['id']))
        finally:
            other.close()
        self.restart()
        self.assertEqual(self.store.marking_jobs(), [])
        uncertain = self.store.marking_jobs('uncertain')[0]
        self.assertEqual(uncertain['payload'], TEXT)
        self.assertTrue(uncertain['error'])
        self.assertTrue(self.store.mark_result(job['id'], 'pending'))
        self.assertTrue(self.store.claim_mark(job['id']))
        self.assertTrue(self.store.mark_result(job['id'], 'failed', ValueError('bad edit')))
        self.restart()
        failed = self.store.marking_jobs('failed')[0]
        self.assertEqual(failed['payload'], TEXT)
        self.assertEqual(failed['error'], 'bad edit')
        self.store.mark_result(job['id'], 'pending')
        self.assertTrue(self.store.claim_mark(job['id']))
        self.store.mark_result(job['id'], 'done')
        self.restart()
        self.assertIsNone(self.store.marking_jobs('done')[0]['payload'])
        self.assertFalse(self.store.mark_result(job['id'], 'pending'))
        self.assertFalse(self.store.claim_mark(job['id']))
        with self.assertRaises(ValueError):
            self.store.mark_result(job['id'], 'sending')

    def test_report_acknowledgement_is_explicit_and_durable(self):
        for state, source_id in (('failed', 10), ('uncertain', 20)):
            with self.subTest(state=state):
                _, pid = self.schedule(TEXT, [source_id])
                self.store.claim()
                self.store.finish(pid)
                job = self.store.marking_jobs()[0]
                self.assertEqual(job['reported'], 0)
                self.assertFalse(self.store.mark_reported(job['id']))
                self.store.claim_mark(job['id'])
                self.store.mark_result(job['id'], state, 'edit failed')
                self.restart()
                self.assertEqual(self.store.marking_jobs(state)[-1]['reported'], 0)
                self.assertTrue(self.store.mark_reported(job['id']))
                self.assertFalse(self.store.mark_reported(job['id']))
                self.restart()
                reported = self.store.marking_jobs(state)[-1]
                self.assertEqual(reported['reported'], 1)
                self.assertEqual(reported['payload'], TEXT)
                self.assertEqual(reported['error'], 'edit failed')
        self.assertFalse(self.store.mark_reported(-1))

    def test_migrates_existing_mark_jobs_with_unreported_default(self):
        _, pid = self.schedule()
        self.store.claim()
        self.store.finish(pid)
        job = self.store.marking_jobs()[0]
        self.store.claim_mark(job['id'])
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE pool_mark_jobs DROP COLUMN reported')
        self.store = Store(self.path)
        recovered = self.store.marking_jobs('uncertain')[0]
        self.assertEqual(recovered['id'], job['id'])
        self.assertEqual(recovered['reported'], 0)
        self.assertEqual(recovered['payload'], TEXT)
        self.assertTrue(self.store.mark_reported(job['id']))
        self.restart()
        self.assertEqual(self.store.marking_jobs('uncertain')[0]['reported'], 1)

    def test_migrates_legacy_database_without_inventing_published_originals(self):
        aid, pid = self.schedule()
        self.store.claim()
        self.store.finish(pid)
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE pool_mark_jobs')
        self.store = Store(self.path)
        self.assertEqual(self.store.marking_jobs(), [])
        self.assertEqual(self.store.assets('published')[0]['id'], aid)
        _, pid = self.schedule(TEXT, [20])
        self.store.claim()
        self.store.finish(pid)
        self.assertEqual(self.store.marking_jobs()[0]['source_id'], 20)


if __name__ == '__main__':
    unittest.main()
