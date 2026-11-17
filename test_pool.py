import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from storage import Store
from pool import ingest, maintain, marker

UTC = timezone.utc
TEXT = {'kind': 'text', 'text': '😀bold', 'entities': [{'type': 'bold', 'offset': 2, 'length': 4}]}
PHOTO = {'kind': 'album', 'media': [
    {'file_id': 'a', 'caption': '😀bold', 'caption_entities': TEXT['entities']},
    {'file_id': 'b', 'caption': 'second', 'caption_entities': []}]}


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.start = datetime.now(UTC) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.app = SimpleNamespace(bot_data={'store': self.store, 'owner_id': 42, 'pool_chat_id': -100},
                                   bot=SimpleNamespace(edit_message_text=AsyncMock(),
                                                       edit_message_caption=AsyncMock(),
                                                       send_message=AsyncMock(return_value=SimpleNamespace(message_id=900))))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, content=TEXT):
        return self.store.add_asset(-100, [10, 11] if content['kind'] == 'album' else [10], content)

    def schedule(self, aid):
        return self.store.create_schedule(123, self.start, self.end, [self.store.asset_content(aid)])

    def due(self):
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=?', ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
        return self.store.claim()

    def test_marker_exact_human_format_and_utc_conversion(self):
        for status, expected in [('pending', '⏳ Pending'), ('published', '✅ Published')]:
            for stamp in ('2026-11-20T12:00:59.123456+00:00',
                          '2026-11-20T14:30:59+02:30', '2026-11-20T12:00:00Z'):
                with self.subTest(status=status, stamp=stamp):
                    self.assertEqual(marker({'id': 77, 'revision': 123, 'status': status, 'stamp': stamp}),
                                     f'{expected} — 2026-11-20 12:00 UTC')
        with self.assertRaises(ValueError):
            marker({'status': 'pending', 'stamp': '2026-11-20T12:00:00'})

    async def test_published_uses_confirmed_time_not_scheduled_time(self):
        self.schedule(self.add())
        post = self.due()
        with patch('storage.datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 20, 12, 0, 59, tzinfo=UTC)
            self.store.finish(post['id'])
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs['text'],
                         '✅ Published — 2026-11-20 12:00 UTC')

    async def test_human_reply_snapshot_survives_restart_and_new_revision(self):
        aid = self.add()
        sid = self.schedule(aid)
        text = marker(self.store.status_jobs()[0])
        self.assertTrue(self.store.claim_status(aid, 2, text))
        self.store.delete_schedule(sid)
        self.start += timedelta(minutes=10)
        self.end += timedelta(minutes=10)
        self.schedule(aid)
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        context = SimpleNamespace(application=self.app)
        msg = SimpleNamespace(chat_id=-100, message_id=900, text=text,
                              reply_to_message=SimpleNamespace(message_id=10),
                              photo=[], entities=[], media_group_id=None)
        await ingest(SimpleNamespace(channel_post=msg), context)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM assets').fetchone()[0], 1)
        self.assertTrue(self.store.is_status_reply(-100, 10, text))
        # Success mapping also works when Telegram omits the reply target.
        self.store.status_result(aid, 2, 'sent', 900)
        msg.reply_to_message = None
        await ingest(SimpleNamespace(channel_post=msg), context)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM assets').fetchone()[0], 1)

    async def test_human_status_not_globally_reserved(self):
        aid = self.add()
        self.schedule(aid)
        text = marker(self.store.status_jobs()[0])
        self.assertFalse(self.store.is_status_reply(-100, 10, text))
        self.assertTrue(self.store.claim_status(aid, 2, text))
        self.assertFalse(self.store.is_status_reply(-200, 10, text))
        self.assertFalse(self.store.is_status_reply(-100, 11, text))
        self.assertFalse(self.store.is_status_reply(-100, 10, text + ' source content'))
        context = SimpleNamespace(application=self.app)
        for mid, reply, value in [(901, None, text),
                                  (902, SimpleNamespace(message_id=11), text),
                                  (903, SimpleNamespace(message_id=10), text + ' source content')]:
            msg = SimpleNamespace(chat_id=-100, message_id=mid, text=value,
                                  reply_to_message=reply, photo=[], entities=[], media_group_id=None)
            await ingest(SimpleNamespace(channel_post=msg), context)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM assets').fetchone()[0], 4)

    async def test_notification_text_migration_preserves_legacy_claim(self):
        aid = self.add()
        sid = self.schedule(aid)
        with self.store.db:
            self.store.db.execute('DROP TABLE status_notifications')
            self.store.db.execute('''CREATE TABLE status_notifications (
                asset_id INTEGER NOT NULL REFERENCES assets(id), revision INTEGER NOT NULL,
                state TEXT NOT NULL, message_id INTEGER, reported INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(asset_id,revision))''')
            self.store.db.execute('INSERT INTO status_notifications VALUES(?,?,?,NULL,0)', (aid, 2, 'sending'))
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.assertEqual(len(self.store.get_posts(sid)), 1)
        self.assertEqual(self.store.status_jobs(), [])
        self.assertTrue(self.store.is_status_reply(-100, 10, f'[pool-status #{aid} r2]\n[pending]'))
        self.assertEqual(len(self.store.uncertain_notifications()), 1)

    async def test_identifiers_original_and_lifecycle(self):
        aid = self.add(PHOTO)
        row = self.store.assets()[0]
        self.assertEqual(row['source_chat'], '-100')
        self.assertEqual(json.loads(row['source_ids']), [10, 11])
        await maintain(self.app)
        self.app.bot.send_message.assert_not_awaited()
        sid = self.schedule(aid)
        await maintain(self.app)
        first = self.app.bot.send_message.call_args.kwargs
        self.assertEqual(first['chat_id'], -100)
        self.assertEqual(first['reply_parameters'].message_id, 10)
        self.assertFalse(first['reply_parameters'].allow_sending_without_reply)
        self.assertEqual(first['text'], f"⏳ Pending — {self.start.strftime('%Y-%m-%d %H:%M UTC')}")
        self.assertEqual(self.store.get_posts(sid)[0]['content'], PHOTO)
        self.assertEqual(self.store.assets('pending')[0]['stamp'], self.start.isoformat())
        await maintain(self.app)
        self.assertIn('UTC', self.app.bot.send_message.call_args.kwargs['text'])
        post = self.due()
        self.store.hold(post['id'], 'failed', 'rejected')
        self.assertEqual(len(self.store.assets('failed')), 1)
        self.store.resolve(post['id'], retry=True)
        post = self.store.claim()
        self.store.finish(post['id'])
        self.assertEqual(self.store.list_schedules(), [])
        published = self.store.assets('published')[0]
        self.assertIsNone(published['content'])
        self.assertIsNotNone(published['stamp'])
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs['text'], marker(published))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM asset_originals').fetchone()[0], 0)
        self.app.bot.edit_message_caption.assert_not_awaited()
        self.app.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.store.status_jobs(), [])

    async def test_pending_replies_follow_schedule_order_after_restart(self):
        aids = [self.store.add_asset(-100, [mid], TEXT) for mid in (10, 20, 30)]
        sid = self.store.create_schedule(123, self.start, self.end,
                                         [self.store.asset_content(aid) for aid in reversed(aids)])
        expected = [30, 20, 10]
        self.assertEqual([p['asset_id'] for p in self.store.get_posts(sid)], list(reversed(aids)))
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        await maintain(self.app)
        calls = self.app.bot.send_message.call_args_list
        self.assertEqual([c.kwargs['reply_parameters'].message_id for c in calls], expected)
        self.assertTrue(all(c.kwargs['text'].startswith('⏳ Pending — ') for c in calls))
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 3)

    async def test_published_replies_follow_completion_not_id_due_or_clock(self):
        aids = [self.store.add_asset(-100, [mid], TEXT) for mid in (10, 20, 30)]
        sid = self.store.create_schedule(123, self.start, self.end,
                                         [self.store.asset_content(aid) for aid in aids])
        await maintain(self.app)
        self.app.bot.send_message.reset_mock()
        posts = self.store.get_posts(sid)
        # Completion order survives tied timestamps and a backwards clock adjustment.
        now = datetime.now(UTC)
        with patch('storage.datetime') as clock:
            clock.now.side_effect = [now, now, now - timedelta(minutes=1)]
            for index in (2, 0, 1):
                with self.store.db:
                    self.store.db.execute("UPDATE posts SET status='sending' WHERE id=?", (posts[index]['id'],))
                self.store.finish(posts[index]['id'])
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        await maintain(self.app)
        calls = self.app.bot.send_message.call_args_list
        self.assertEqual([c.kwargs['reply_parameters'].message_id for c in calls], [30, 10, 20])
        self.assertTrue(all(c.kwargs['text'].startswith('✅ Published — ') for c in calls))

    async def test_mixed_backlog_follows_latest_transition_sequence(self):
        first = self.store.add_asset(-100, [10], TEXT)
        second = self.store.add_asset(-100, [20], TEXT)
        sid = self.schedule(second)
        # An earlier pending transition is superseded by its publication.
        post = self.store.get_posts(sid)[0]
        self.schedule(first)
        with self.store.db:
            self.store.db.execute("UPDATE posts SET status='sending' WHERE id=?", (post['id'],))
        self.store.finish(post['id'])
        await maintain(self.app)
        calls = self.app.bot.send_message.call_args_list
        self.assertEqual([c.kwargs['reply_parameters'].message_id for c in calls], [10, 20])
        self.assertIn('⏳ Pending — ', calls[0].kwargs['text'])
        self.assertIn('✅ Published — ', calls[1].kwargs['text'])

    async def test_legacy_status_order_migration_preserves_queue_and_claims(self):
        aids = [self.store.add_asset(-100, [mid], TEXT) for mid in (10, 20, 30)]
        sid = self.store.create_schedule(123, self.start, self.end,
                                         [self.store.asset_content(aid) for aid in reversed(aids)])
        self.assertTrue(self.store.claim_status(aids[1], 2))
        self.store.status_result(aids[1], 2, 'uncertain')
        with self.store.db:
            self.store.db.execute('DROP TRIGGER order_asset_status')
            self.store.db.execute('DROP TABLE asset_status_order')
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.assertEqual([p['asset_id'] for p in self.store.get_posts(sid)], list(reversed(aids)))
        self.assertEqual([job['id'] for job in self.store.status_jobs()], [aids[2], aids[0]])
        await maintain(self.app)
        calls = [c for c in self.app.bot.send_message.call_args_list if c.kwargs.get('chat_id') == -100]
        self.assertEqual([c.kwargs['reply_parameters'].message_id for c in calls], [30, 10])
        self.assertEqual(self.store.status_jobs(), [])

    async def test_atomic_duplicate_and_cross_connection_claim(self):
        aid = self.add()
        snapshot = self.store.asset_content(aid)
        with self.assertRaises(ValueError):
            self.store.create_schedule(123, self.start, self.end, [snapshot, snapshot])
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(len(self.store.assets()), 1)
        self.schedule(aid)
        other = Store(self.path)
        try:
            with self.assertRaises(ValueError):
                other.create_schedule(123, self.start, self.end, [snapshot])
        finally:
            other.close()
        self.assertEqual(len(self.store.list_schedules()), 1)

    async def test_cancellation_and_uncertain_discard_require_confirmation(self):
        aid = self.add()
        sid = self.schedule(aid)
        self.store.delete_schedule(sid)
        self.assertEqual(len(self.store.assets()), 1)
        sid = self.schedule(aid)
        post = self.due()
        self.assertTrue(self.store.delete_schedule(sid))
        self.store.hold(post['id'], 'uncertain', 'timeout')
        self.store.release_uncertain(aid)
        self.assertEqual(self.store.assets(), [])
        self.store.delete_schedule(sid)
        self.assertEqual(len(self.store.assets('uncertain')), 1)
        self.store.release_uncertain(aid)
        self.assertEqual(len(self.store.assets()), 1)

    async def test_recovery_syncs_uncertain(self):
        self.schedule(self.add())
        self.due()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.assets('uncertain')), 1)
        self.assertEqual(self.store.status_jobs()[0]['status'], 'uncertain')

    async def test_missing_source_noop_and_other_errors_persist_restart(self):
        from telegram.error import BadRequest
        sid = self.schedule(self.add())
        self.app.bot.send_message.side_effect = BadRequest('Message to be replied not found')
        await maintain(self.app)
        self.assertEqual(self.store.status_jobs(), [])
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.assertFalse(self.app.bot.send_message.call_args.kwargs[
            'reply_parameters'].allow_sending_without_reply)
        self.store.delete_schedule(sid)
        self.schedule(1)
        self.app.bot.send_message.side_effect = BadRequest('Not enough rights')
        await maintain(self.app)
        self.assertEqual(len(self.store.status_jobs()), 1)
        self.app.bot.send_message.assert_awaited()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.status_jobs()), 1)

    async def test_marker_failure_after_success_never_resends(self):
        from main import delivery_worker
        self.schedule(self.add())
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=?', ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
        with patch('main.send_content', new_callable=AsyncMock) as send:
            task = asyncio.create_task(delivery_worker(self.app))
            await asyncio.sleep(.03)
            from telegram.error import Forbidden
            self.app.bot.send_message.side_effect = Forbidden('Not enough rights')
            await maintain(self.app)
            await maintain(self.app)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(send.await_count, 1)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.status_jobs()[0]['status'], 'published')
        self.assertIsNone(self.store.claim())

    async def test_private_pool_multiple_selection_and_album(self):
        from bot import _callback, _message
        aid = self.add(PHOTO)
        second = self.store.add_asset(-100, [12], TEXT)
        draft = {'key': 'draft1', 'start': self.start, 'end': self.end, 'posts': [], 'pending': {}}
        context = SimpleNamespace(application=self.app, bot=self.app.bot, user_data={'draft': draft})
        message = SimpleNamespace(chat_id=42, text='Asset pool', reply_text=AsyncMock())
        with patch('bot.send_content', new_callable=AsyncMock) as preview:
            await _message(SimpleNamespace(effective_message=message), context)
            preview.assert_not_awaited()
        self.app.bot.send_message.assert_not_awaited()
        entries = message.reply_text.call_args_list[1:]
        self.assertEqual(len(entries), 2)
        self.assertIn('album · available · 2 photos', entries[0].args[0])
        self.assertIn('text · available', entries[1].args[0])
        for entry, asset_id in zip(entries, [aid, second]):
            button = entry.kwargs['reply_markup'].inline_keyboard[0][0]
            self.assertEqual(button.text, 'Add to draft')
            self.assertEqual(button.callback_data, f'asset:draft1:{asset_id}')
        for selected in [aid, second, aid]:
            query = SimpleNamespace(data=f'asset:draft1:{selected}', answer=AsyncMock())
            await _callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), context)
        self.assertEqual([p['asset_id'] for p in draft['posts']], [aid, second])
        sid = self.store.create_schedule(123, self.start, self.end, draft['posts'])
        self.assertEqual(len(self.store.get_posts(sid)), 2)
        self.assertEqual(self.store.get_posts(sid)[0]['content'], PHOTO)

    def forward_context(self):
        draft = {'key': 'forward1', 'start': self.start, 'end': self.end, 'posts': [], 'pending': {}}
        return SimpleNamespace(application=self.app, bot=self.app.bot,
                               user_data={'draft': draft, 'step': 'posts'})

    def forwarded_message(self, source_id=10, source_chat=-100, group=None, photo=False):
        from telegram import Chat, MessageOriginChannel
        origin = MessageOriginChannel(date=datetime.now(UTC),
                                      chat=Chat(source_chat, 'channel'), message_id=source_id)
        return SimpleNamespace(chat_id=42, message_id=1000 + source_id, text=None if photo else 'forwarded text',
                               photo=[SimpleNamespace(file_id='forwarded-file')] if photo else [],
                               caption='changed caption', caption_entities=[], entities=[],
                               media_group_id=group, forward_origin=origin, reply_text=AsyncMock())

    async def test_forwarded_text_keeps_received_text_and_entities(self):
        from bot import _message
        from telegram import MessageEntity
        aid = self.add()
        context = self.forward_context()
        msg = self.forwarded_message()
        msg.text = '😀new forwarded text'
        msg.entities = [MessageEntity('bold', 2, 3)]
        await _message(SimpleNamespace(effective_message=msg), context)
        post = context.user_data['draft']['posts'][0]
        self.assertEqual(post['text'], msg.text)
        self.assertEqual(post['entities'], [e.to_dict() for e in msg.entities])
        self.assertEqual(post['asset_id'], aid)

    async def test_commit_keeps_approved_snapshot_not_pool_original(self):
        aid = self.add()
        snapshot = {**self.store.asset_content(aid), 'text': 'approved draft text', 'entities': []}
        sid = self.store.create_schedule(123, self.start, self.end, [snapshot])
        saved = self.store.get_posts(sid)[0]['content']
        self.assertEqual(saved['text'], 'approved draft text')
        self.assertNotIn('asset_id', saved)
        original = json.loads(self.store.db.execute('SELECT content FROM assets WHERE id=?', (aid,)).fetchone()[0])
        self.assertEqual(original, TEXT)

    async def test_forwarded_text_and_photo_link_exact_source_and_commit(self):
        from bot import _message, _callback, send_content
        from pool import content_of
        from telegram import MessageEntity
        for content in (TEXT, {'kind': 'photo', 'media': [PHOTO['media'][0]]}):
            with self.subTest(kind=content['kind']):
                source_id = 20 if content['kind'] == 'text' else 21
                aid = self.store.add_asset(-100, [source_id], content)
                context = self.forward_context()
                msg = self.forwarded_message(source_id, photo=content['kind'] == 'photo')
                msg.entities = [MessageEntity('bold', 0, 9)]
                msg.caption_entities = [MessageEntity('italic', 0, 7)]
                expected = content_of(msg)
                await _message(SimpleNamespace(effective_message=msg), context)
                self.assertEqual(context.user_data['draft']['posts'], [{**expected, 'asset_id': aid}])
                self.assertIn(aid, [asset['id'] for asset in self.store.assets()])
                query = SimpleNamespace(data='draft:forward1:commit', answer=AsyncMock())
                self.app.bot_data['chat_id'] = 123
                with patch('bot._permission', new_callable=AsyncMock, return_value=None):
                    await _callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), context)
                row = self.store.db.execute('SELECT * FROM posts WHERE asset_id=?', (aid,)).fetchone()
                self.assertIsNotNone(row)
                saved = json.loads(row['content'])
                self.assertEqual(saved, expected)
                self.assertNotIn('asset_id', saved)
                self.assertEqual(json.loads(self.store.db.execute('SELECT content FROM assets WHERE id=?', (aid,)).fetchone()[0]), content)
                sender = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
                await send_content(sender, 123, saved)
                sent = (sender.send_message if content['kind'] == 'text' else sender.send_photo).call_args.kwargs
                key = 'text' if content['kind'] == 'text' else 'caption'
                entities_key = 'entities' if content['kind'] == 'text' else 'caption_entities'
                self.assertEqual(sent[key], msg.text if key == 'text' else msg.caption)
                self.assertEqual([e.to_dict() for e in sent[entities_key]], [e.to_dict() for e in getattr(msg, entities_key)])
                await maintain(self.app)
                call = self.app.bot.send_message.call_args.kwargs
                self.assertEqual(call['reply_parameters'].message_id, source_id)
                self.assertIn('⏳ Pending — ', call['text'])

    async def test_forwarded_album_members_select_full_asset_once(self):
        from bot import _message, _callback
        aid = self.add(PHOTO)
        context = self.forward_context()
        from telegram import MessageEntity
        from pool import content_of
        expected = json.loads(json.dumps(PHOTO))
        for source_id in [11, 10, 11]:
            msg = self.forwarded_message(source_id, group='forwarded-album', photo=True)
            msg.caption = f'😀forwarded caption {source_id}'
            msg.caption_entities = [MessageEntity('bold', 2, 9)]
            expected['media'][source_id - 10] = content_of(msg)['media'][0]
            await _message(SimpleNamespace(effective_message=msg), context)
        draft = context.user_data['draft']
        self.assertEqual(draft['posts'], [{**expected, 'asset_id': aid}])
        self.assertIn('forwarded-album', draft['pending'])
        # Commit must wait for all forwarded album updates.
        query = SimpleNamespace(data='draft:forward1:commit', answer=AsyncMock())
        await _callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), context)
        self.assertEqual(self.store.list_schedules(), [])
        await asyncio.sleep(1.6)
        self.assertEqual(draft['pending'], {})
        query = SimpleNamespace(data=f'asset:forward1:{aid}', answer=AsyncMock())
        await _callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), context)
        self.assertEqual(len(draft['posts']), 1)
        sid = self.store.create_schedule(123, self.start, self.end, draft['posts'])
        saved = self.store.get_posts(sid)[0]['content']
        self.assertEqual(saved, expected)
        from bot import send_content
        sender = SimpleNamespace(send_media_group=AsyncMock(return_value=[]))
        await send_content(sender, 123, saved)
        sent = sender.send_media_group.call_args.kwargs['media']
        self.assertEqual([m.caption for m in sent], [m['caption'] for m in expected['media']])
        self.assertEqual([[e.to_dict() for e in m.caption_entities] for m in sent],
                         [m['caption_entities'] for m in expected['media']])
        self.assertEqual(json.loads(self.store.db.execute('SELECT content FROM assets WHERE id=?', (aid,)).fetchone()[0]), PHOTO)
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs['reply_parameters'].message_id, 10)
        # Late members after commit cannot escape the asset reservation as standalone posts.
        later_context = self.forward_context()
        await _message(SimpleNamespace(effective_message=self.forwarded_message(11, group='forwarded-album', photo=True)), later_context)
        self.assertEqual(later_context.user_data['draft']['posts'], [])
        self.assertEqual(len(self.store.get_posts(sid)), 1)

    async def test_single_forwarded_album_member_preserves_empty_caption(self):
        from bot import _message
        aid = self.add(PHOTO)
        context = self.forward_context()
        msg = self.forwarded_message(11, photo=True)
        msg.caption = None
        await _message(SimpleNamespace(effective_message=msg), context)
        draft = context.user_data['draft']
        post = draft['posts'][0]
        self.assertEqual(post['media'][0], PHOTO['media'][0])
        self.assertEqual(post['media'][1]['caption'], '')
        self.assertEqual(post['media'][1]['caption_entities'], [])
        self.assertEqual(post['asset_id'], aid)
        sid = self.store.create_schedule(123, self.start, self.end, draft['posts'])
        self.assertEqual(self.store.get_posts(sid)[0]['content']['media'][1]['caption'], '')

    async def test_linked_album_unattributed_item_is_not_guessed(self):
        from bot import _message, _clear
        self.add(PHOTO)
        context = self.forward_context()
        msg = self.forwarded_message(10, group='linked', photo=True)
        await _message(SimpleNamespace(effective_message=msg), context)
        msg = self.forwarded_message(11, group='linked', photo=True)
        msg.forward_origin = None
        try:
            await _message(SimpleNamespace(effective_message=msg), context)
            self.assertIn('was not added', msg.reply_text.call_args.args[0])
            post = context.user_data['draft']['posts'][0]
            self.assertEqual(post['media'][1], PHOTO['media'][1])
            self.assertEqual(len(context.user_data['draft']['posts']), 1)
        finally:
            _clear(context)

    async def test_forwarded_unavailable_asset_never_becomes_standalone(self):
        from bot import _message
        aid = self.add()
        self.schedule(aid)
        context = self.forward_context()
        msg = self.forwarded_message()
        await _message(SimpleNamespace(effective_message=msg), context)
        self.assertEqual(context.user_data['draft']['posts'], [])
        self.assertIn('no longer available', msg.reply_text.call_args.args[0])
        self.assertEqual(len(self.store.list_schedules()), 1)

    async def test_forwarded_asset_commit_race_remains_atomic(self):
        from bot import _message
        aid = self.add()
        context = self.forward_context()
        await _message(SimpleNamespace(effective_message=self.forwarded_message()), context)
        self.schedule(aid)
        with self.assertRaises(ValueError):
            self.store.create_schedule(123, self.start, self.end, context.user_data['draft']['posts'])
        self.assertEqual(len(self.store.list_schedules()), 1)

    async def test_unknown_and_hidden_forward_sources_require_selection(self):
        from bot import _message
        from telegram import MessageOriginHiddenUser
        self.add(PHOTO)
        context = self.forward_context()
        msg = self.forwarded_message(999, photo=True)
        await _message(SimpleNamespace(effective_message=msg), context)
        self.assertEqual(context.user_data['draft']['posts'], [])
        self.assertIn('Asset pool', msg.reply_text.call_args.args[0])
        msg.forward_origin = MessageOriginHiddenUser(datetime.now(UTC), 'Hidden')
        await _message(SimpleNamespace(effective_message=msg), context)
        self.assertEqual(context.user_data['draft']['posts'], [])
        self.assertIn('Telegram hid', msg.reply_text.call_args.args[0])

    async def test_other_channel_forward_is_not_linked_by_message_id(self):
        from bot import _message
        self.add()
        context = self.forward_context()
        msg = self.forwarded_message(source_chat=-200)
        await _message(SimpleNamespace(effective_message=msg), context)
        post = context.user_data['draft']['posts'][0]
        self.assertNotIn('asset_id', post)
        self.assertEqual(post['text'], 'forwarded text')

    async def test_no_origin_upload_or_reply_is_standalone_not_guessed(self):
        from bot import _message
        self.add(PHOTO)
        context = self.forward_context()
        msg = self.forwarded_message(photo=True)
        msg.forward_origin = None
        msg.photo = [SimpleNamespace(file_id='a')]
        msg.reply_to_message = SimpleNamespace(message_id=900, photo=msg.photo)
        await _message(SimpleNamespace(effective_message=msg), context)
        post = context.user_data['draft']['posts'][0]
        self.assertNotIn('asset_id', post)
        self.assertEqual(post['media'][0]['caption'], 'changed caption')
        self.assertIn('not linked', msg.reply_text.call_args.args[0])
        self.assertIsNone(self.store.asset_from_source(-200, 10))

    async def test_private_pool_without_draft_lists_metadata_only(self):
        from bot import _message
        self.add(PHOTO)
        context = SimpleNamespace(application=self.app, bot=self.app.bot, user_data={})
        message = SimpleNamespace(chat_id=42, text='Asset pool', reply_text=AsyncMock())
        with patch('bot.send_content', new_callable=AsyncMock) as preview:
            await _message(SimpleNamespace(effective_message=message), context)
            preview.assert_not_awaited()
        self.assertIn('album · available · 2 photos', message.reply_text.call_args.args[0])
        self.assertIsNone(message.reply_text.call_args.kwargs['reply_markup'])
        self.app.bot.send_message.assert_not_awaited()

    async def test_status_filter_clears_dirty_without_claiming(self):
        aid = self.add()
        for status in ('available', 'failed', 'uncertain'):
            with self.subTest(status=status):
                with self.store.db:
                    self.store._asset_status(aid, status)
                with patch.object(self.store, 'claim_status', wraps=self.store.claim_status) as claim:
                    await maintain(self.app)
                    await maintain(self.app)
                    claim.assert_not_called()
                self.assertEqual(self.store.status_jobs(), [])
                self.assertEqual(self.store.db.execute('SELECT dirty FROM assets WHERE id=?',
                                                      (aid,)).fetchone()[0], 0)
        self.app.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM status_notifications').fetchone()[0], 0)

    async def test_filtered_asset_still_reports_uncertain_notification_privately(self):
        aid = self.add()
        self.schedule(aid)
        self.assertTrue(self.store.claim_status(aid, 2))
        self.store.status_result(aid, 2, 'uncertain')
        with self.store.db:
            self.store._asset_status(aid, 'uncertain')
        await maintain(self.app)
        self.assertEqual(self.store.status_jobs(), [])
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.assertEqual(self.app.bot.send_message.call_args.args[0], 42)
        self.assertIn('will not be resent', self.app.bot.send_message.call_args.args[1])
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 1)

    async def test_long_source_is_not_copied_or_edited(self):
        self.schedule(self.add({'kind': 'text', 'text': 'x' * 4096, 'entities': []}))
        await maintain(self.app)
        self.assertNotIn('xxxx', self.app.bot.send_message.call_args.kwargs['text'])
        self.app.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.store.status_jobs(), [])

    async def test_timeout_is_not_resent_but_new_revision_sends(self):
        from telegram.error import TimedOut
        aid = self.add()
        sid = self.schedule(aid)
        self.app.bot.send_message.side_effect = TimedOut('secret raw details')
        await maintain(self.app)
        await maintain(self.app)
        pool_calls = [c for c in self.app.bot.send_message.call_args_list
                      if c.kwargs.get('chat_id') == -100]
        self.assertEqual(len(pool_calls), 1)
        self.assertEqual(self.store.status_jobs(), [])
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.app.bot.send_message.side_effect = None
        await maintain(self.app)
        self.store.delete_schedule(sid)
        self.schedule(aid)
        await maintain(self.app)
        pool_calls = [c for c in self.app.bot.send_message.call_args_list
                      if c.kwargs.get('chat_id') == -100]
        self.assertEqual(len(pool_calls), 2)
        self.assertIn('⏳ Pending — ', pool_calls[-1].kwargs['text'])
        for call in self.app.bot.send_message.call_args_list:
            self.assertNotIn('secret raw details', str(call))

    async def test_cancellation_during_send_preserves_claim(self):
        self.schedule(self.add())
        async def interrupted(*args, **kwargs):
            raise asyncio.CancelledError()
        self.app.bot.send_message.side_effect = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await maintain(self.app)
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.uncertain_notifications()), 1)
        self.assertEqual(self.store.status_jobs(), [])

    async def test_status_claim_is_atomic_across_connections(self):
        aid = self.add()
        other = Store(self.path)
        try:
            self.assertTrue(self.store.claim_status(aid, 1))
            self.assertFalse(other.claim_status(aid, 1))
        finally:
            other.close()

    async def test_crash_claim_recovered_and_reported_without_resend(self):
        aid = self.add()
        sid = self.schedule(aid)
        self.assertTrue(self.store.claim_status(aid, 2))
        self.assertFalse(self.store.claim_status(aid, 2))
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.assertEqual(self.app.bot.send_message.call_args.args[0], 42)
        self.assertIn('will not be resent', self.app.bot.send_message.call_args.args[1])
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.store.delete_schedule(sid)
        self.schedule(aid)
        await maintain(self.app)
        self.assertEqual(self.app.bot.send_message.call_args.kwargs['chat_id'], -100)

    async def test_status_update_race_does_not_ingest_reply(self):
        aid = self.add()
        sid = self.schedule(aid)
        context = SimpleNamespace(application=self.app)
        async def send(*args, **kwargs):
            msg = SimpleNamespace(chat_id=-100, message_id=900,
                                  reply_to_message=SimpleNamespace(message_id=10),
                                  text=kwargs['text'], entities=[], photo=[], media_group_id=None)
            # Advance the asset before its old reply update arrives; the claim's
            # snapshot must identify the old text rather than the current stamp.
            self.store.delete_schedule(sid)
            self.start += timedelta(minutes=10)
            self.end += timedelta(minutes=10)
            self.schedule(aid)
            await ingest(SimpleNamespace(channel_post=msg), context)
            return SimpleNamespace(message_id=900)
        self.app.bot.send_message.side_effect = send
        await maintain(self.app)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM assets').fetchone()[0], 1)
        self.assertEqual(self.store.status_jobs()[0]['status'], 'pending')
        self.app.bot.send_message.side_effect = None
        await maintain(self.app)
        self.assertEqual(self.store.status_jobs(), [])

    async def test_existing_originals_and_queue_survive_migration(self):
        aid = self.add()
        sid = self.schedule(aid)
        with self.store.db:
            self.store.db.execute('INSERT INTO asset_originals VALUES(?,?,?)',
                                  (aid, json.dumps(TEXT), 'text'))
            self.store.db.execute('DROP TABLE status_notifications')
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.assertEqual(self.store.get_posts(sid)[0]['content'], TEXT)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM asset_originals').fetchone()[0], 1)
        await maintain(self.app)
        self.assertIn('⏳ Pending — ', self.app.bot.send_message.call_args.kwargs['text'])

    async def test_startup_notification_ignored_even_after_restart(self):
        from main import notify_startup
        self.app.bot_data['chat_id'] = -200
        await notify_startup(self.app)
        text = self.app.bot.send_message.call_args.kwargs['text']
        message = SimpleNamespace(chat_id=-100, message_id=900, text=text,
                                  entities=[], photo=[], media_group_id=None)
        context = SimpleNamespace(application=self.app)
        await ingest(SimpleNamespace(channel_post=message), context)
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data = {'store': self.store, 'pool_chat_id': -100, 'owner_id': 42}
        await ingest(SimpleNamespace(channel_post=message), context)
        self.assertEqual(self.store.assets(), [])
        self.assertEqual(self.store.status_jobs(), [])
        for mid, text in enumerate(['Scheduler started',
                                    text + '\nsource content',
                                    'Scheduler started\nServer time: tomorrow'], 901):
            message.message_id, message.text = mid, text
            await ingest(SimpleNamespace(channel_post=message), context)
        self.assertEqual(len(self.store.assets()), 3)

    async def test_ingestion_filters_edits_and_other_channels_and_album(self):
        context = SimpleNamespace(application=self.app)
        def message(mid, gid=None, chat=-100):
            return SimpleNamespace(chat_id=chat, message_id=mid, media_group_id=gid,
                                   text='hello', entities=[], photo=[], caption=None)
        await ingest(SimpleNamespace(edited_channel_post=message(1)), context)
        await ingest(SimpleNamespace(channel_post=message(1, chat=-200)), context)
        self.assertEqual(self.store.assets(), [])
        await ingest(SimpleNamespace(channel_post=message(1)), context)
        await ingest(SimpleNamespace(channel_post=message(1)), context)
        self.assertEqual(len(self.store.assets()), 1)
        for mid in [4, 3]:
            msg = message(mid, 'album')
            msg.photo = [SimpleNamespace(file_id=str(mid))]
            msg.caption = f'caption {mid}'
            msg.caption_entities = []
            await ingest(SimpleNamespace(channel_post=msg), context)
        await asyncio.sleep(1.6)
        asset = self.store.assets()[1]
        self.assertEqual(json.loads(asset['source_ids']), [3, 4])
        self.assertEqual(json.loads(asset['content'])['media'][0]['caption'], 'caption 3')


if __name__ == '__main__':
    unittest.main()
