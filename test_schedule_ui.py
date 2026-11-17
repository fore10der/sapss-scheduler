import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    async def begin_replacement(self):
        old, new = self.asset(), self.asset(2, {**CONTENT, 'text': 'Replacement'})
        sid, post = self.schedule(old)
        await self.callback(f"post_replace:{post['id']}")
        await self.message(forward_origin=self.origin(2), text='Forwarded')
        return old, new, sid, post, self.context.user_data['replacement']['key']

    async def test_draft_forward_selects_full_album_and_preserves_received_caption(self):
        album = {'kind': 'album', 'media': [
            {'file_id': 'first', 'caption': 'Original first', 'caption_entities': []},
            {'file_id': 'second', 'caption': 'Original second', 'caption_entities': []}]}
        aid = self.asset(content=album, ids=[10, 20])
        draft = self.draft()
        entity = MessageEntity('bold', 0, 3)
        message = await self.message(forward_origin=self.origin(20),
                                     photo=[SimpleNamespace(file_id='forwarded-second')],
                                     caption='New caption', caption_entities=[entity])
        self.assertEqual(len(draft['posts']), 1)
        selected = draft['posts'][0]
        self.assertEqual(selected['asset_id'], aid)
        self.assertEqual(selected['media'], [album['media'][0], {
            'file_id': 'forwarded-second', 'caption': 'New caption',
            'caption_entities': [entity.to_dict()]}])
        self.assertEqual(self.store.asset_content(aid)['media'], album['media'])
        self.assertEqual(self.asset_status(aid), 'available')
        self.assertIn('Full album selected', message.reply_text.call_args.args[0])
        await self.message(forward_origin=self.origin(10),
                           photo=[SimpleNamespace(file_id='first')])
        self.assertEqual(len(draft['posts']), 1)

    async def test_replacement_forward_previews_complete_stored_album(self):
        old = self.asset()
        _, post = self.schedule(old)
        album = {'kind': 'album', 'media': [
            {'file_id': 'first', 'caption': 'First'},
            {'file_id': 'second', 'caption': 'Second'}]}
        aid = self.asset(content=album, ids=[10, 20])
        await self.callback(f"post_replace:{post['id']}")
        message = await self.message(forward_origin=self.origin(20))
        replacement = self.context.user_data['replacement']
        self.assertEqual(replacement['asset_id'], aid)
        media = self.bot.send_media_group.call_args.kwargs['media']
        self.assertEqual([(item.media, item.caption) for item in media],
                         [('first', 'First'), ('second', 'Second')])
        self.assertEqual(self.buttons(message.reply_text.call_args.kwargs['reply_markup']), [[
            ('Confirm replacement', f"replace_yes:{replacement['key']}:{aid}"),
            ('Cancel', f"replace_cancel:{replacement['key']}")]])
        self.assertEqual(self.asset_status(aid), 'available')

    async def test_replacement_rejects_wrong_or_missing_source(self):
        _, _, sid, post, _ = await self.begin_replacement()
        replacement = dict(self.context.user_data['replacement'])
        self.bot.send_message.reset_mock()
        for origin in (self.origin(2, -999), None):
            with self.subTest(origin=origin):
                message = await self.message(forward_origin=origin, text='Wrong source')
                self.assertIn('configured pool channel', message.reply_text.call_args.args[0])
                self.assertEqual(self.context.user_data['replacement'], replacement)
                self.assertEqual(self.store.get_posts(sid)[0], post)
        self.bot.send_message.assert_not_awaited()

    async def test_replace_yes_preserves_slot_and_releases_old_asset(self):
        old, new, sid, post, key = await self.begin_replacement()
        query = await self.callback(f'replace_yes:{key}:{new}')
        result = self.store.get_posts(sid)[0]
        self.assertEqual((result['id'], result['due']), (post['id'], post['due']))
        self.assertEqual(result['asset_id'], new)
        self.assertEqual(self.asset_status(old), 'available')
        self.assertEqual(self.asset_status(new), 'pending')
        self.assertNotIn('replacement', self.context.user_data)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_replace_yes_rechecks_asset_availability_after_preview(self):
        old, new, sid, post, key = await self.begin_replacement()
        self.schedule(new)
        query = await self.callback(f'replace_yes:{key}:{new}')
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.asset_status(old), 'pending')
        self.assertIn('replacement', self.context.user_data)
        self.assertEqual(self.bot.send_message.call_args.args[1], 'Asset is no longer available.')
        query.edit_message_reply_markup.assert_not_awaited()

    async def test_replace_yes_rechecks_post_pending_after_preview(self):
        for status in ('sending', 'uncertain', 'failed'):
            with self.subTest(status=status):
                old = self.asset(100 + len(self.store.list_schedules()))
                new = self.asset(200 + len(self.store.list_schedules()))
                sid, post = self.schedule(old)
                self.context.user_data['replacement'] = {
                    'post_id': post['id'], 'asset_id': new, 'key': status}
                self.store.hold(post['id'], status, 'test')
                before = self.store.get_posts(sid)
                await self.callback(f'replace_yes:{status}:{new}')
                self.assertEqual(self.store.get_posts(sid), before)
                self.assertEqual(self.asset_status(new), 'available')
                self.assertNotIn('replacement', self.context.user_data)
                self.assertIn('no longer pending', self.bot.send_message.call_args.args[1])

    async def test_replacement_cancel_and_stale_key_do_not_mutate_post(self):
        _, new, sid, post, key = await self.begin_replacement()
        replacement = dict(self.context.user_data['replacement'])
        for action in ('replace_cancel:stale', f'replace_yes:stale:{new}'):
            query = await self.callback(action)
            self.assertEqual(self.context.user_data['replacement'], replacement)
            query.edit_message_reply_markup.assert_not_awaited()
        query = await self.callback(f'replace_cancel:{key}')
        self.assertNotIn('replacement', self.context.user_data)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        await self.callback(f'replace_yes:{key}:{new}')
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.asset_status(new), 'available')

    async def test_replace_yes_rejects_superseded_preview(self):
        _, new, sid, post, key = await self.begin_replacement()
        latest = self.asset(3)
        self.context.user_data['replacement']['asset_id'] = latest
        query = await self.callback(f'replace_yes:{key}:{new}')
        self.assertEqual(self.store.get_posts(sid)[0], post)
        self.assertEqual(self.context.user_data['replacement']['asset_id'], latest)
        self.assertEqual(self.bot.send_message.call_args.args[1], 'Use the latest replacement preview.')
        query.edit_message_reply_markup.assert_not_awaited()

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
