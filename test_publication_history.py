import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, MessageOriginChannel

from bot import _callback, _message
from storage import Store

UTC = timezone.utc
TEXT = {'kind': 'text', 'text': 'Original text', 'entities': []}
ALBUM = {'kind': 'album', 'media': [
    {'file_id': 'a', 'caption': 'first', 'caption_entities': []},
    {'file_id': 'b', 'caption': 'second', 'caption_entities': []}]}


class PublicationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.db'
        self.store = Store(self.path)
        self.start = datetime.now(UTC) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self.temp.cleanup()

    def pending(self, source_ids, content=TEXT):
        aid = self.store.add_asset(-100, source_ids, content)
        sid = self.store.create_schedule(
            -200, self.start, self.end, [self.store.asset_content(aid)])
        return aid, sid, self.store.get_posts(sid)[0]

    def schedule(self, source_ids, content=TEXT):
        aid, sid, post = self.pending(source_ids, content)
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=? WHERE id=?',
                                  ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                                   post['id']))
        claimed = self.store.claim()
        self.assertEqual(claimed['id'], post['id'])
        return aid, sid, claimed

    def publish(self, source_ids, content=TEXT):
        aid, _, post = self.schedule(source_ids, content)
        self.store.finish(post['id'])
        return aid

    def test_confirmed_finish_records_every_album_source(self):
        aid = self.publish([10, 11], ALBUM)
        self.assertEqual(self.store.published_source_ids(-100, [10, 11]), {10, 11})
        self.assertEqual(self.store.published_source_ids(-100, [10, 99]), {10})
        self.assertEqual(self.store.assets('published')[0]['id'], aid)

    def test_failed_and_uncertain_delivery_are_not_published(self):
        _, _, post = self.schedule([20])
        self.store.hold(post['id'], 'failed', 'rejected')
        self.assertEqual(self.store.published_source_ids(-100, [20]), set())
        self.store.resolve(post['id'], retry=True)
        post = self.store.claim()
        self.store.hold(post['id'], 'uncertain', 'timeout')
        self.assertEqual(self.store.published_source_ids(-100, [20]), set())
        self.assertEqual(self.store.assets('uncertain')[0]['status'], 'uncertain')

    def test_album_member_overlap_rejects_subset_and_regrouped_forward(self):
        self.publish([10, 11], ALBUM)
        for ids in ([10], [11, 12], [9, 10, 11]):
            with self.subTest(ids=ids):
                with self.assertRaisesRegex(ValueError, 'already published'):
                    self.store.create_schedule(-200, self.start, self.end, [
                        {**TEXT, 'source': {'chat_id': '-100', 'message_ids': ids}}])
                self.assertEqual(self.store.list_schedules(), [])
        # Same content with a different original message ID is not this check.
        sid = self.store.create_schedule(-200, self.start, self.end, [
            {**TEXT, 'source': {'chat_id': '-100', 'message_ids': [12]}}])
        self.assertEqual(self.store.list_schedules()[0]['id'], sid)

    def test_commit_rejection_rolls_back_earlier_reservations(self):
        self.publish([10])
        before = [dict(row) for row in self.store.db.execute(
            'SELECT id,status,revision FROM assets ORDER BY id')]
        with self.assertRaisesRegex(ValueError, 'already published'):
            self.store.create_schedule(-200, self.start, self.end, [
                {**TEXT, 'source': {'chat_id': '-100', 'message_ids': [30]}},
                {**TEXT, 'source': {'chat_id': '-100', 'message_ids': [10]}}])
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.assets('pending'), [])
        self.assertEqual(self.store.published_source_ids(-100, [30]), set())
        self.assertEqual([dict(row) for row in self.store.db.execute(
            'SELECT id,status,revision FROM assets ORDER BY id')], before)

    def test_replacement_rejection_preserves_post_and_old_source(self):
        old, sid, post = self.pending([40])
        self.publish([50])
        before_asset = dict(self.store.db.execute(
            'SELECT status,content FROM assets WHERE id=?', (old,)).fetchone())
        with self.assertRaisesRegex(ValueError, 'already published'):
            self.store.replace_post(
                post['id'], {**TEXT, 'source': {'chat_id': '-100', 'message_ids': [50, 51]}})
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(dict(self.store.db.execute(
            'SELECT status,content FROM assets WHERE id=?', (old,)).fetchone()), before_asset)
        self.assertEqual(self.store.published_source_ids(-100, [51]), set())

    def test_direct_asset_path_cannot_reuse_published_evidence(self):
        aid = self.publish([60])
        # Simulate a reset to a reusable status; evidence must still block this.
        with self.store.db:
            self.store.db.execute(
                "UPDATE assets SET status='available',content=? WHERE id=?",
                (json.dumps(TEXT), aid))
        with self.assertRaisesRegex(ValueError, 'already published'):
            self.store.create_schedule(-200, self.start, self.end, [
                {**TEXT, 'asset_id': aid}])
        self.assertEqual(self.store.list_schedules(), [])

    def test_migration_backfills_existing_published_assets_and_survives_reset(self):
        aid = self.publish([70, 71], ALBUM)
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE published_sources')
        self.store = Store(self.path)
        self.assertEqual(self.store.published_source_ids(-100, [70, 71]), {70, 71})
        with self.store.db:
            self.store.db.execute("UPDATE assets SET status='pending' WHERE id=?", (aid,))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.published_source_ids(-100, [70, 71]), {70, 71})
        with self.assertRaisesRegex(ValueError, 'already published'):
            self.store.create_schedule(-200, self.start, self.end, [
                {**TEXT, 'source': {'chat_id': '-100', 'message_ids': [71]}}])
        self.assertEqual(self.store.list_schedules(), [])


class PublicationUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'queue.db')
        self.addCleanup(self.store.close)
        self.start = datetime.now(UTC) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                   send_media_group=AsyncMock(return_value=[]))
        self.context = SimpleNamespace(
            user_data={}, bot=self.bot,
            application=SimpleNamespace(bot_data={
                'store': self.store, 'pool_chat_id': -100, 'chat_id': -200}))

    def draft(self):
        draft = {'key': 'draft-key', 'posts': [], 'pending': {},
                 'start': self.start, 'end': self.end}
        self.context.user_data.update(draft=draft, step='posts')
        return draft

    def origin(self, source_id=1, chat_id=-100):
        return MessageOriginChannel(datetime.now(UTC), Chat(chat_id, 'channel'), source_id)

    async def message(self, source_id=1, group=None, photo=False, text='Forwarded'):
        fields = dict(chat_id=42, message_id=1000 + source_id,
                      text=None if photo else text,
                      photo=[SimpleNamespace(file_id=f'file-{source_id}')] if photo else [],
                      caption='caption' if photo else None, caption_entities=[],
                      entities=[], media_group_id=group,
                      forward_origin=self.origin(source_id), reply_text=AsyncMock())
        message = SimpleNamespace(**fields)
        await _message(SimpleNamespace(effective_message=message), self.context)
        return message

    async def callback(self, data):
        query = SimpleNamespace(data=data, answer=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        await _callback(SimpleNamespace(callback_query=query,
                                       effective_chat=SimpleNamespace(id=42)), self.context)
        return query

    def publish(self, source_ids, content=TEXT):
        aid = self.store.add_asset(-100, source_ids, content)
        sid = self.store.create_schedule(
            -200, self.start, self.end, [self.store.asset_content(aid)])
        post = self.store.get_posts(sid)[0]
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=? WHERE id=?',
                                  ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                                   post['id']))
        claimed = self.store.claim()
        self.assertEqual(claimed['id'], post['id'])
        self.store.finish(claimed['id'])
        return aid

    def active_post(self, source_ids=(1,), content=TEXT):
        aid = self.store.add_asset(-100, list(source_ids), content)
        sid = self.store.create_schedule(
            -200, self.start, self.end, [self.store.asset_content(aid)])
        return aid, sid, self.store.get_posts(sid)[0]

    def asset_snapshot(self):
        return [(row['id'], row['status'], row['revision'], row['dirty'])
                for row in self.store.db.execute('SELECT * FROM assets ORDER BY id')]

    async def test_published_forward_rejected_without_state_changes(self):
        aid = self.publish([10])
        self.draft()
        before = self.asset_snapshot()
        message = await self.message(10)
        self.assertIn('already published', message.reply_text.call_args.args[0])
        self.assertEqual(self.context.user_data['draft']['posts'], [])
        self.assertEqual(self.asset_snapshot(), before)
        self.assertEqual(self.store.published_source_ids(-100, [10]), {10})
        self.assertEqual(self.store.assets('published')[0]['id'], aid)

    async def test_published_album_member_rejects_whole_received_album(self):
        self.publish([10, 11], ALBUM)
        draft = self.draft()
        message = await self.message(10, group='album', photo=True)
        self.assertIn('already published', message.reply_text.call_args.args[0])
        # A later member of the same forwarded album cannot leak in alone.
        message = await self.message(12, group='album', photo=True)
        self.assertIn('already published', message.reply_text.call_args.args[0])
        self.assertEqual(draft['posts'], [])
        self.assertEqual(draft['pending'], {})
        self.assertEqual(self.store.assets('pending'), [])

    async def test_late_published_member_discards_already_collected_album(self):
        self.publish([11], {'kind': 'photo', 'media': [ALBUM['media'][1]]})
        draft = self.draft()
        await self.message(10, group='album', photo=True)
        self.assertEqual(len(draft['posts']), 1)
        message = await self.message(11, group='album', photo=True)
        self.assertIn('album was not added', message.reply_text.call_args.args[0])
        self.assertEqual(draft['posts'], [])
        self.assertEqual(draft['pending'], {})
        self.assertEqual(self.store.assets('pending'), [])

    async def test_published_member_after_flush_discards_collected_album(self):
        self.publish([11], {'kind': 'photo', 'media': [ALBUM['media'][1]]})
        draft = self.draft()
        await self.message(10, group='album', photo=True)
        await asyncio.sleep(1.6)
        self.assertEqual(len(draft['posts']), 1)
        message = await self.message(11, group='album', photo=True)
        self.assertIn('album was not added', message.reply_text.call_args.args[0])
        self.assertEqual(draft['posts'], [])
        self.assertEqual(self.store.assets('pending'), [])

    async def test_replacement_preview_rejects_published_source(self):
        old, sid, post = self.active_post([20])
        self.publish([30])
        await self.callback(f"post_replace:{post['id']}")
        replacement = self.context.user_data['replacement']
        message = await self.message(30)
        self.assertIn('already published', message.reply_text.call_args.args[0])
        self.assertIsNone(replacement['content'])
        self.assertIsNone(replacement['source'])
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM assets WHERE id=?', (old,)).fetchone()[0], 'pending')

    async def test_commit_rechecks_source_published_after_forward(self):
        draft = self.draft()
        await self.message(40)
        self.assertEqual(len(draft['posts']), 1)
        # Another delivery publishes the source after forward-time validation.
        self.publish([40])
        with patch('bot._permission', new_callable=AsyncMock, return_value=None):
            await self.callback('draft:draft-key:commit')
        message = self.bot.send_message.call_args.args[1]
        self.assertIn('already published', message)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.assets('pending'), [])
        self.assertIn('draft', self.context.user_data)

    async def test_replacement_confirmation_rechecks_late_publication(self):
        old, sid, post = self.active_post([21])
        await self.callback(f"post_replace:{post['id']}")
        await self.message(41)
        key = self.context.user_data['replacement']['key']
        self.publish([41])
        await self.callback(f'replace_yes:{key}')
        message = self.bot.send_message.call_args.args[1]
        self.assertIn('already published', message)
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM assets WHERE id=?', (old,)).fetchone()[0], 'pending')
        self.assertEqual(self.store.assets('pending')[0]['id'], old)
        self.assertIn('replacement', self.context.user_data)

    async def test_cancel_after_rejection_creates_no_tracking_rows(self):
        self.publish([50])
        draft = self.draft()
        message = await self.message(50)
        self.assertIn('already published', message.reply_text.call_args.args[0])
        await self.callback('draft:draft-key:cancel')
        self.assertNotIn('draft', self.context.user_data)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM status_notifications').fetchone()[0], 0)
        self.assertEqual(self.store.published_source_ids(-100, [50]), {50})


if __name__ == '__main__':
    unittest.main()
