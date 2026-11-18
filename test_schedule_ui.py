import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, MessageEntity, MessageOriginChannel

from bot import _callback, _message, _show_posts
from storage import Store


CONTENT = {'kind': 'text', 'text': 'Original', 'entities': []}


class ScheduleUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'queue.db')
        self.addCleanup(self.store.close)
        self.start = datetime.now(timezone.utc) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                   send_media_group=AsyncMock(return_value=[]))
        self.context = SimpleNamespace(
            user_data={}, bot=self.bot,
            application=SimpleNamespace(bot_data={
                'store': self.store, 'pool_chat_id': -100, 'chat_id': -200}))

    def asset(self, source_id=1, content=None, source_chat=-100, ids=None):
        return self.store.add_asset(source_chat, ids or [source_id], content or CONTENT)

    def schedule(self, aid):
        sid = self.store.create_schedule('-200', self.start, self.end,
                                         [self.store.asset_content(aid)])
        return sid, self.store.get_posts(sid)[0]

    def draft(self):
        draft = {'key': 'draft-key', 'posts': [], 'pending': {},
                 'start': self.start, 'end': self.end}
        self.context.user_data.update(draft=draft, step='posts')
        return draft

    def origin(self, source_id=1, chat_id=-100):
        return MessageOriginChannel(datetime.now(timezone.utc),
                                    Chat(chat_id, 'channel'), source_id)

    async def message(self, **kwargs):
        fields = dict(chat_id=42, message_id=99, text=None, photo=[], entities=[],
                      caption=None, caption_entities=[], media_group_id=None,
                      forward_origin=None, reply_text=AsyncMock())
        fields.update(kwargs)
        message = SimpleNamespace(**fields)
        await _message(SimpleNamespace(effective_message=message), self.context)
        return message

    async def callback(self, data):
        query = SimpleNamespace(data=data, answer=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        await _callback(SimpleNamespace(callback_query=query,
                                       effective_chat=SimpleNamespace(id=42)), self.context)
        query.answer.assert_awaited_once_with()
        return query

    def buttons(self, markup):
        return [[(button.text, button.callback_data) for button in row]
                for row in markup.inline_keyboard]

    def asset_status(self, aid):
        return self.store.db.execute('SELECT status FROM assets WHERE id=?', (aid,)).fetchone()[0]

    async def begin_replacement(self, source_id=1, replacement_id=2):
        old = self.asset(source_id)
        sid, post = self.schedule(old)
        await self.callback(f"post_replace:{post['id']}")
        await self.message(forward_origin=self.origin(replacement_id), text='Forwarded')
        return old, sid, post, self.context.user_data['replacement']['key']

    async def test_draft_forward_album_collects_only_forwarded_members(self):
        draft = self.draft()
        entity = MessageEntity('bold', 0, 3)
        message = await self.message(forward_origin=self.origin(20),
                                     photo=[SimpleNamespace(file_id='forwarded-second')],
                                     caption='New caption', caption_entities=[entity],
                                     media_group_id='forwarded-album')
        self.assertEqual(len(draft['posts']), 1)
        self.assertIn('Receiving the album', message.reply_text.call_args.args[0])
        await self.message(forward_origin=self.origin(10),
                           photo=[SimpleNamespace(file_id='first')],
                           media_group_id='forwarded-album')
        await asyncio.sleep(1.6)
        selected = draft['posts'][0]
        self.assertEqual(selected['source'],
                         {'chat_id': '-100', 'message_ids': [10, 20]})
        self.assertEqual(selected['media'], [
            {'file_id': 'first', 'caption': '', 'caption_entities': []},
            {'file_id': 'forwarded-second', 'caption': 'New caption',
             'caption_entities': [entity.to_dict()]}])
        # No tracked source exists until Commit.
        self.assertEqual(self.store.assets(), [])
        with patch('bot._permission', new=AsyncMock(return_value=None)):
            await self.callback('draft:draft-key:commit')
        self.assertEqual(len(self.store.list_schedules()), 1)
        aid = self.store.asset_from_source(-100, 10)['id']
        self.assertEqual(self.asset_status(aid), 'pending')
        self.assertEqual(self.store.get_posts(self.store.list_schedules()[0]['id'])[0]['content'],
                         {k: v for k, v in selected.items() if k != 'source'})

    async def test_replacement_forward_previews_without_persisting_tracking(self):
        old = self.asset()
        _, post = self.schedule(old)
        await self.callback(f"post_replace:{post['id']}")
        message = await self.message(forward_origin=self.origin(20), text='Forwarded replacement')
        replacement = self.context.user_data['replacement']
        self.assertEqual(replacement['source'],
                         {'chat_id': '-100', 'message_ids': [20]})
        self.assertEqual(replacement['content']['text'], 'Forwarded replacement')
        self.assertIsNone(self.store.asset_from_source(-100, 20))
        self.assertEqual(self.buttons(message.reply_text.call_args.kwargs['reply_markup']), [[
            ('Confirm replacement', f"replace_yes:{replacement['key']}"),
            ('Cancel', f"replace_cancel:{replacement['key']}")]])
        self.assertEqual(self.asset_status(old), 'pending')

    async def test_replacement_rejects_wrong_or_missing_source(self):
        _, sid, post, _ = await self.begin_replacement()
        replacement = dict(self.context.user_data['replacement'])
        self.bot.send_message.reset_mock()
        for origin in (self.origin(2, -999), None):
            with self.subTest(origin=origin):
                message = await self.message(forward_origin=origin, text='Wrong source')
                self.assertIn('configured pool channel', message.reply_text.call_args.args[0])
                self.assertEqual(self.context.user_data['replacement'], replacement)
                self.assertEqual(self.store.get_posts(sid)[0], post)
        self.bot.send_message.assert_not_awaited()

    async def test_replacement_album_uses_only_forwarded_members(self):
        old = self.asset()
        sid, post = self.schedule(old)
        await self.callback(f"post_replace:{post['id']}")
        key = self.context.user_data['replacement']['key']
        await self.message(forward_origin=self.origin(20), media_group_id='replacement-album',
                           photo=[SimpleNamespace(file_id='forwarded-two')], caption='two')
        await self.message(forward_origin=self.origin(10), media_group_id='replacement-album',
                           photo=[SimpleNamespace(file_id='forwarded-one')], caption='one')
        self.assertIsNone(self.store.asset_from_source(-100, 10))
        await asyncio.sleep(1.6)
        media = self.bot.send_media_group.call_args.kwargs['media']
        self.assertEqual([(item.media, item.caption) for item in media],
                         [('forwarded-one', 'one'), ('forwarded-two', 'two')])
        self.assertIsNone(self.store.asset_from_source(-100, 10))
        query = await self.callback(f'replace_yes:{key}')
        result = self.store.get_posts(sid)[0]
        new = self.store.asset_from_source(-100, 10)['id']
        self.assertEqual(result['asset_id'], new)
        self.assertEqual([item['file_id'] for item in result['content']['media']],
                         ['forwarded-one', 'forwarded-two'])
        self.assertEqual(self.asset_status(old), 'available')
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_replace_yes_preserves_slot_and_tracks_new_source(self):
        old, sid, post, key = await self.begin_replacement()
        query = await self.callback(f'replace_yes:{key}')
        result = self.store.get_posts(sid)[0]
        self.assertEqual((result['id'], result['due']), (post['id'], post['due']))
        self.assertEqual(result['content']['text'], 'Forwarded')
        new = self.store.asset_from_source(-100, 2)['id']
        self.assertEqual(result['asset_id'], new)
        self.assertEqual(self.asset_status(old), 'available')
        self.assertEqual(self.asset_status(new), 'pending')
        self.assertNotIn('replacement', self.context.user_data)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_replace_yes_rechecks_source_availability_after_preview(self):
        old, sid, post, key = await self.begin_replacement()
        self.store.create_schedule('-200', self.start, self.end, [
            {k: v for k, v in CONTENT.items()}
            | {'source': {'chat_id': '-100', 'message_ids': [2]}}])
        query = await self.callback(f'replace_yes:{key}')
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.asset_status(old), 'pending')
        self.assertIn('replacement', self.context.user_data)
        self.assertIn('already scheduled', self.bot.send_message.call_args.args[1])
        query.edit_message_reply_markup.assert_not_awaited()

    async def test_replace_yes_rechecks_post_pending_after_preview(self):
        for index, status in enumerate(('sending', 'uncertain', 'failed')):
            with self.subTest(status=status):
                old, sid, post, key = await self.begin_replacement(
                    100 + index, 200 + index)
                self.store.hold(post['id'], status, 'test')
                before = self.store.get_posts(sid)
                await self.callback(f'replace_yes:{key}')
                self.assertEqual(self.store.get_posts(sid), before)
                self.assertIsNone(self.store.asset_from_source(-100, 200 + index))
                self.assertNotIn('replacement', self.context.user_data)
                self.assertIn('no longer pending', self.bot.send_message.call_args.args[1])

    async def test_replacement_cancel_and_stale_key_do_not_mutate_post(self):
        _, sid, post, key = await self.begin_replacement()
        replacement = dict(self.context.user_data['replacement'])
        for action in ('replace_cancel:stale', 'replace_yes:stale'):
            query = await self.callback(action)
            self.assertEqual(self.context.user_data['replacement'], replacement)
            query.edit_message_reply_markup.assert_not_awaited()
        query = await self.callback(f'replace_cancel:{key}')
        self.assertNotIn('replacement', self.context.user_data)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        await self.callback(f'replace_yes:{key}')
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertIsNone(self.store.asset_from_source(-100, 2))

    async def test_replacement_confirm_uses_latest_forward(self):
        _, sid, post, key = await self.begin_replacement()
        await self.message(forward_origin=self.origin(3), text='Latest')
        query = await self.callback(f'replace_yes:{key}')
        result = self.store.get_posts(sid)[0]
        self.assertEqual(result['content']['text'], 'Latest')
        self.assertEqual(self.store.asset_from_source(-100, 3)['status'], 'pending')
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_delete_confirmation_then_pending_only_deletion(self):
        aid = self.asset()
        sid, post = self.schedule(aid)
        await self.callback(f"post_delete:{post['id']}")
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.buttons(self.bot.send_message.call_args.kwargs['reply_markup']), [[
            ('Delete post', f"post_delete_yes:{post['id']}"), ('Keep', 'noop')]])
        query = await self.callback(f"post_delete_yes:{post['id']}")
        self.assertEqual(self.store.get_posts(sid), [])
        self.assertEqual(self.asset_status(aid), 'available')
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        await self.callback(f"post_delete_yes:{post['id']}")
        self.assertIn('no longer pending', self.bot.send_message.call_args.args[1])

    async def test_delete_rechecks_pending_status(self):
        for index, status in enumerate(('sending', 'uncertain', 'failed')):
            with self.subTest(status=status):
                aid = self.asset(index + 1)
                sid, post = self.schedule(aid)
                await self.callback(f"post_delete:{post['id']}")
                self.store.hold(post['id'], status, 'test')
                before = self.store.get_posts(sid)
                asset_status = self.asset_status(aid)
                await self.callback(f"post_delete_yes:{post['id']}")
                self.assertEqual(self.store.get_posts(sid), before)
                self.assertEqual(self.asset_status(aid), asset_status)
                self.assertIn('no longer pending', self.bot.send_message.call_args.args[1])

    async def test_show_posts_controls_only_for_pending_scheduled_posts(self):
        for index, status in enumerate(('pending', 'sending', 'uncertain', 'failed')):
            with self.subTest(status=status):
                sid, post = self.schedule(self.asset(index + 1))
                if status != 'pending':
                    self.store.hold(post['id'], status, 'test')
                self.bot.send_message.reset_mock()
                await _show_posts(self.context, 42, self.store.get_posts(sid))
                header, content = self.bot.send_message.call_args_list
                self.assertIn(status, header.args[1])
                markup = header.kwargs['reply_markup']
                if status == 'pending':
                    self.assertEqual(self.buttons(markup), [[
                        ('Delete post', f"post_delete:{post['id']}"),
                        ('Replace post', f"post_replace:{post['id']}")]])
                else:
                    self.assertIsNone(markup)
                self.assertEqual(content.kwargs['text'], CONTENT['text'])

    async def test_show_posts_draft_preview_has_no_schedule_controls(self):
        await _show_posts(self.context, 42, [CONTENT], [self.start])
        header, content = self.bot.send_message.call_args_list
        self.assertIsNone(header.kwargs['reply_markup'])
        self.assertNotIn('pending', header.args[1])
        self.assertEqual(content.kwargs['text'], CONTENT['text'])


if __name__ == '__main__':
    unittest.main()
