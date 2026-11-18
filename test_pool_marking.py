import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from pool import marked_content, mark_published_sources
from storage import Store


TEXT = {'kind': 'text', 'text': '😀bold',
        'entities': [{'type': 'bold', 'offset': 2, 'length': 4}]}


class MarkedContentTests(unittest.TestCase):
    def test_preserves_payload_and_does_not_duplicate_existing_tag(self):
        for original, expected in (('', '#to_delete'), ('hello', 'hello\n\n#to_delete'),
                                   ('#to_delete', '#to_delete'),
                                   ('hello\n\n#to_delete', 'hello\n\n#to_delete')):
            with self.subTest(original=original):
                payload = dict(TEXT, text=original)
                before = copy.deepcopy(payload)
                self.assertEqual(marked_content(payload), (expected, TEXT['entities']))
                self.assertEqual(payload, before)

    def test_utf16_conservative_limits_for_text_and_caption(self):
        for kind, limit in (('text', 4096), ('photo', 1024)):
            for excess in (0, 1):
                with self.subTest(kind=kind, excess=excess):
                    # One astral character occupies two UTF-16 units, not one.
                    original = '😀' + 'x' * (limit - 14 + excess)
                    payload = ({'kind': kind, 'text': original} if kind == 'text' else
                               {'kind': kind, 'media': [{'caption': original}]})
                    if excess:
                        with self.assertRaises(ValueError):
                            marked_content(payload)
                    else:
                        value, entities = marked_content(payload)
                        self.assertEqual(len(value.encode('utf-16-le')) // 2, limit)
                        self.assertEqual(entities, [])


class PoolMarkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.bot = SimpleNamespace(edit_message_text=AsyncMock(),
                                   edit_message_caption=AsyncMock(), send_message=AsyncMock())
        self.app = SimpleNamespace(bot=self.bot, bot_data={'store': self.store, 'owner_id': 42})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.app.bot_data['store'] = self.store

    def publish(self, content=TEXT, source_ids=None):
        aid = self.store.add_asset(-100, source_ids or [10], content)
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        sid = self.store.create_schedule(123, start, start + timedelta(hours=1),
                                         [self.store.asset_content(aid)])
        with self.store.db:
            self.store.db.execute("UPDATE posts SET due='2000-01-01T00:00:00+00:00'")
        post = self.store.claim()
        self.assertEqual(post['id'], self.store.get_posts(sid)[0]['id'])
        self.store.finish(post['id'])
        return aid

    def assert_terminal(self, state, error=None):
        self.assertEqual(self.store.marking_jobs(), [])
        job, = self.store.marking_jobs(state)
        self.assertEqual(job['error'], error)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertIsNone(self.store.claim())
        return job

    async def test_finish_jobs_edit_text_preserving_entities_and_survive_restart(self):
        self.publish()
        self.restart()
        await mark_published_sources(self.app)
        kwargs = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs, dict(chat_id=-100, message_id=10,
                                     text='😀bold\n\n#to_delete',
                                     entities=kwargs['entities'], parse_mode=None,
                                     rate_limit_args={'priority': -10}))
        self.assertEqual([entity.to_dict() for entity in kwargs['entities']], TEXT['entities'])
        self.bot.edit_message_caption.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.assert_terminal('done')['payload'])
        self.restart()
        await mark_published_sources(self.app)
        self.bot.edit_message_text.assert_awaited_once()

    async def test_empty_photo_caption_becomes_tag(self):
        self.publish({'kind': 'photo', 'media': [{'file_id': 'a', 'caption': '',
                                                 'caption_entities': []}]})
        await mark_published_sources(self.app)
        self.bot.edit_message_caption.assert_awaited_once_with(
            chat_id=-100, message_id=10, caption='#to_delete', caption_entities=[],
            parse_mode=None, rate_limit_args={'priority': -10})
        self.bot.edit_message_text.assert_not_awaited()
        self.assert_terminal('done')

    async def test_every_album_member_is_tagged_with_own_caption_and_entities(self):
        media = [{'file_id': 'a', 'caption': '😀bold', 'caption_entities': TEXT['entities']},
                 {'file_id': 'b', 'caption': '', 'caption_entities': []},
                 {'file_id': 'c', 'caption': 'third', 'caption_entities': []}]
        self.publish({'kind': 'album', 'media': media}, [10, 11, 12])
        await mark_published_sources(self.app)
        calls = self.bot.edit_message_caption.await_args_list
        self.assertEqual(len(calls), 3)
        for call, mid, item in zip(calls, [10, 11, 12], media):
            kwargs = call.kwargs
            self.assertEqual(kwargs['message_id'], mid)
            self.assertEqual(kwargs['chat_id'], -100)
            self.assertEqual(kwargs['caption'], (item['caption'] + '\n\n' if item['caption'] else '') + '#to_delete')
            self.assertEqual([e.to_dict() for e in kwargs['caption_entities']], item['caption_entities'])
            self.assertIsNone(kwargs['parse_mode'])
        self.assertEqual(len(self.store.marking_jobs('done')), 3)
        await mark_published_sources(self.app)
        self.assertEqual(self.bot.edit_message_caption.await_count, 3)
        self.bot.send_message.assert_not_awaited()

    async def test_overlength_content_is_not_edited_and_reports_failed(self):
        for kind, limit, mid in (('text', 4096, 10), ('photo', 1024, 20)):
            with self.subTest(kind=kind):
                content = ({'kind': kind, 'text': 'x' * limit} if kind == 'text' else
                           {'kind': kind, 'media': [{'file_id': 'a', 'caption': 'x' * limit}]})
                self.publish(content, [mid])
                await mark_published_sources(self.app)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.edit_message_caption.assert_not_awaited()
        self.assertEqual([j['error'] for j in self.store.marking_jobs('failed')],
                         ['ValueError', 'ValueError'])
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_permission_and_missing_message_errors_are_terminal_and_reported(self):
        for mid, error in enumerate((Forbidden('Not enough rights'),
                                     BadRequest('Not enough rights to edit messages'),
                                     BadRequest('Message to edit not found')), 10):
            with self.subTest(error=error):
                self.publish(source_ids=[mid])
                self.bot.edit_message_text.side_effect = error
                await mark_published_sources(self.app)
                job = self.store.marking_jobs('failed')[-1]
                self.assertEqual(job['error'], type(error).__name__)
                self.assertEqual(job['reported'], 1)
        self.restart()
        await mark_published_sources(self.app)
        self.assertEqual(self.bot.edit_message_text.await_count, 3)
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_message_not_modified_is_done_without_failure_report(self):
        self.publish()
        self.bot.edit_message_text.side_effect = BadRequest('Message is not modified')
        await mark_published_sources(self.app)
        self.assert_terminal('done')
        self.bot.send_message.assert_not_awaited()

    async def test_retry_after_leaves_pending_then_retries(self):
        self.publish()
        self.bot.edit_message_text.side_effect = RetryAfter(2)
        with patch('pool.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await mark_published_sources(self.app)
        sleep.assert_awaited_once_with(2)
        job, = self.store.marking_jobs()
        self.assertEqual((job['state'], job['error']), ('pending', 'RetryAfter'))
        self.bot.send_message.assert_not_awaited()
        self.restart()
        self.bot.edit_message_text.side_effect = None
        await mark_published_sources(self.app)
        self.assert_terminal('done')
        self.assertEqual(self.bot.edit_message_text.await_count, 2)

    async def test_network_error_is_uncertain_and_never_automatically_retried(self):
        self.publish()
        self.bot.edit_message_text.side_effect = NetworkError('Lost response')
        await mark_published_sources(self.app)
        self.assertEqual(self.assert_terminal('uncertain', 'NetworkError')['reported'], 1)
        self.restart()
        await mark_published_sources(self.app)
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_cancellation_persists_uncertain_and_propagates(self):
        self.publish()
        self.bot.edit_message_text.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await mark_published_sources(self.app)
        self.assertEqual(self.assert_terminal('uncertain', 'CancelledError')['reported'], 0)
        self.restart()
        await mark_published_sources(self.app)
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.marking_jobs('uncertain')[0]['reported'], 1)

    async def test_failed_owner_report_retries_after_restart_and_ack_is_persisted(self):
        self.publish()
        self.bot.edit_message_text.side_effect = Forbidden('Cannot edit')
        self.bot.send_message.side_effect = NetworkError('Owner unavailable')
        await mark_published_sources(self.app)
        self.assertEqual(self.assert_terminal('failed', 'Forbidden')['reported'], 0)
        self.restart()
        self.bot.send_message.side_effect = None
        await mark_published_sources(self.app)
        self.assertEqual(self.store.marking_jobs('failed')[0]['reported'], 1)
        args = self.bot.send_message.await_args
        self.assertEqual(args.args[0], 42)
        self.assertIn('message #10', args.args[1])
        self.assertIn('Destination publication succeeded and will not be retried', args.args[1])
        self.assertIsNone(args.kwargs['parse_mode'])
        self.restart()
        await mark_published_sources(self.app)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_count, 2)


if __name__ == '__main__':
    unittest.main()
