import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, MessageEntity, MessageOriginChannel

from bot import _callback, _message
from pool import ingest, maintain
from storage import Store

UTC = timezone.utc
TEXT = {'kind': 'text', 'text': 'Original', 'entities': []}


class ForwardWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.start = datetime.now(UTC) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)))
        self.app = SimpleNamespace(
            bot=self.bot,
            bot_data={'store': self.store, 'pool_chat_id': -100,
                      'chat_id': -200, 'owner_id': 42})
        self.context = SimpleNamespace(application=self.app, bot=self.bot, user_data={})

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self.temp.cleanup()

    def draft(self):
        draft = {'key': 'draft-key', 'posts': [], 'pending': {},
                 'start': self.start, 'end': self.end}
        self.context.user_data.update(draft=draft, step='posts')
        return draft

    def origin(self, source_id=10, channel=-100):
        return MessageOriginChannel(datetime.now(UTC), Chat(channel, 'channel'), source_id)

    async def forward(self, source_id=10, group=None, photo=False, caption=None):
        fields = dict(chat_id=42, message_id=1000 + source_id, text=None if photo else 'forwarded',
                      photo=[SimpleNamespace(file_id=f'file-{source_id}')] if photo else [],
                      caption=caption, caption_entities=[], entities=[], media_group_id=group,
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

    async def test_forward_and_cancel_are_draft_local(self):
        self.draft()
        await self.forward()
        self.assertEqual(len(self.context.user_data['draft']['posts']), 1)
        self.assertEqual(self.store.assets(), [])
        self.assertEqual(self.store.status_jobs(), [])
        await self.callback('draft:draft-key:cancel')
        self.assertNotIn('draft', self.context.user_data)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.assets(), [])
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM status_notifications').fetchone()[0], 0)

    async def test_commit_and_restart_preserve_source_and_pending_reply(self):
        self.draft()
        await self.forward(777)
        self.assertEqual(self.store.assets(), [])
        with patch('bot._permission', new_callable=AsyncMock, return_value=None):
            await self.callback('draft:draft-key:commit')
        sid = self.store.list_schedules()[0]['id']
        asset = self.store.asset_from_source(-100, 777)
        self.assertEqual(asset['status'], 'pending')
        self.assertEqual(json.loads(asset['source_ids']), [777])
        self.assertEqual(self.store.get_posts(sid)[0]['asset_id'], asset['id'])
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.bot.send_message.reset_mock()
        await maintain(self.app)
        call = self.bot.send_message.call_args.kwargs
        self.assertEqual(call['chat_id'], -100)
        self.assertEqual(call['reply_parameters'].message_id, 777)
        self.assertIn('⏳ Pending — ', call['text'])
        self.assertEqual(self.store.asset_from_source(-100, 777)['id'], asset['id'])
        self.assertEqual(self.store.status_jobs(), [])

    async def test_forwarded_album_schedules_only_received_members(self):
        self.draft()
        for source_id, caption in ((30, 'first'), (32, 'third')):
            await self.forward(source_id, group='g', photo=True, caption=caption)
        await asyncio.sleep(1.6)
        await self.callback('draft:draft-key:cancel')
        self.assertEqual(self.store.assets(), [])
        # Recreate the draft and commit the received pair.
        self.draft()
        for source_id, caption in ((30, 'first'), (32, 'third')):
            await self.forward(source_id, group='g', photo=True, caption=caption)
        await asyncio.sleep(1.6)
        with patch('bot._permission', new_callable=AsyncMock, return_value=None):
            await self.callback('draft:draft-key:commit')
        aid = self.store.asset_from_source(-100, 30)['id']
        asset = next(a for a in self.store.assets('pending') if a['id'] == aid)
        self.assertEqual(json.loads(asset['source_ids']), [30, 32])
        self.assertEqual([media['caption'] for media in json.loads(asset['content'])['media']],
                         ['first', 'third'])
        self.assertIsNone(self.store.asset_from_source(-100, 31))

    async def test_new_channel_posts_are_ignored_and_edits_never_create_records(self):
        context = SimpleNamespace(application=self.app)
        posted = SimpleNamespace(chat_id=-100, message_id=50, text='new post', entities=[],
                                 photo=[], caption=None, media_group_id=None)
        await ingest(SimpleNamespace(channel_post=posted), context)
        edited = SimpleNamespace(chat_id=-100, message_id=51, text='edited unknown', entities=[],
                                 photo=[], caption=None, media_group_id=None)
        await ingest(SimpleNamespace(edited_channel_post=edited), context)
        self.assertEqual(self.store.assets(), [])
        self.assertEqual(self.store.status_jobs(), [])

    async def test_legacy_committed_schedule_survives_restart(self):
        aid = self.store.add_asset(-100, [55], TEXT)
        sid = self.store.create_schedule('-200', self.start, self.end,
                                         [self.store.asset_content(aid)])
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store
        self.assertEqual(self.store.list_schedules()[0]['id'], sid)
        post = self.store.get_posts(sid)[0]
        self.assertEqual(post['content'], TEXT)
        self.assertEqual(post['asset_id'], aid)
        self.bot.send_message.reset_mock()
        await maintain(self.app)
        self.assertEqual(self.bot.send_message.call_args.kwargs['reply_parameters'].message_id, 55)
        self.assertIn('⏳ Pending — ', self.bot.send_message.call_args.kwargs['text'])


if __name__ == '__main__':
    unittest.main()
