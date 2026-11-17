import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import CallbackQuery, Message
from telegram.ext import ExtBot
from telegram.request import BaseRequest

from bot import build_application, send_content
from main import delivery_worker
from rate_limiter import TelegramRateLimiter
from storage import Store


class RecordingLimiter(TelegramRateLimiter):
    """Keep real endpoint policy while replacing timed admission with a spy."""

    def __init__(self):
        super().__init__()
        self.admissions = []

    async def acquire(self, chat_id=None, cost=1, priority=0):
        self.admissions.append((chat_id, cost, priority))


class FakeRequest(BaseRequest):
    def __init__(self):
        self.endpoints = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        self.endpoints.append(endpoint)
        if endpoint == 'getMe':
            result = {'id': 123, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
        elif endpoint == 'answerCallbackQuery':
            result = True
        else:
            data = request_data.parameters
            message = {'message_id': 10, 'date': 1,
                       'chat': {'id': int(data['chat_id']), 'type': 'private'},
                       'text': data.get('text', '')}
            result = [message, {**message, 'message_id': 11}] if endpoint == 'sendMediaGroup' else message
        return 200, json.dumps({'ok': True, 'result': result}).encode()


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_factory_attaches_shared_limiter(self):
        store = object()
        app = build_application('123:TEST_ONLY', 42, -100, store)
        self.assertIsInstance(app.bot, ExtBot)
        self.assertIsInstance(app.bot.rate_limiter, TelegramRateLimiter)
        self.assertIs(app.bot_data['store'], store)
        self.assertEqual(app.bot_data['chat_id'], -100)

    async def test_send_content_priorities_and_cost_reach_real_extbot_limiter(self):
        limiter = RecordingLimiter()
        request = FakeRequest()
        bot = ExtBot('123:TEST_ONLY', request=request, rate_limiter=limiter)
        contents = [
            ({'kind': 'text', 'text': 'hello'}, 'sendMessage', 1),
            ({'kind': 'photo', 'media': [{'file_id': 'photo1'}]}, 'sendPhoto', 1),
            ({'kind': 'album', 'media': [{'file_id': 'photo1'}, {'file_id': 'photo2'}]},
             'sendMediaGroup', 2),
            ({'kind': 'album', 'media': [{'file_id': 'photo1'}]}, 'sendPhoto', 1),
        ]
        async with bot:
            for content, endpoint, cost in contents:
                for options, priority in [(None, -10), ({'priority': 10}, 10),
                                          ({'priority': 0}, 0)]:
                    with self.subTest(kind=content['kind'], cost=cost, options=options):
                        limiter.admissions.clear()
                        request.endpoints.clear()
                        messages = await asyncio.wait_for(
                            send_content(bot, 42, content, rate_limit_args=options), 1)
                        self.assertEqual(len(messages), cost)
                        self.assertTrue(all(isinstance(message, Message) for message in messages))
                        self.assertEqual(request.endpoints, [endpoint])
                        self.assertEqual(limiter.admissions, [(42, cost, priority)])

    async def test_reply_shortcuts_and_callback_share_extbot_limiter(self):
        limiter = RecordingLimiter()
        request = FakeRequest()
        bot = ExtBot('123:TEST_ONLY', request=request, rate_limiter=limiter)
        async with bot:
            message = Message.de_json({'message_id': 1, 'date': 1, 'text': 'incoming',
                                       'chat': {'id': 42, 'type': 'private'}}, bot)
            query = CallbackQuery.de_json({
                'id': 'callback1', 'chat_instance': 'instance',
                'from': {'id': 42, 'is_bot': False, 'first_name': 'Owner'},
                'message': message.to_dict(), 'data': 'test'}, bot)
            limiter.admissions.clear()
            request.endpoints.clear()
            await asyncio.wait_for(message.reply_text('reply'), 1)
            await asyncio.wait_for(message.reply_photo('photo1'), 1)
            await asyncio.wait_for(message.edit_text('edited'), 1)
            await asyncio.wait_for(query.answer(), 1)
            self.assertEqual(request.endpoints, [
                'sendMessage', 'sendPhoto', 'editMessageText', 'answerCallbackQuery'])
            self.assertEqual(limiter.admissions, [
                (42, 1, 0), (42, 1, 0), (None, 1, 0), (None, 1, 20)])


class BlockedReadinessLimiter(TelegramRateLimiter):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def wait_for_readiness(self, chat_id, cost=1):
        self.calls.append((chat_id, cost))
        self.entered.set()
        await self.release.wait()


class WorkerReadinessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.limiter = BlockedReadinessLimiter()
        self.app = SimpleNamespace(
            bot=SimpleNamespace(rate_limiter=self.limiter),
            bot_data={'store': self.store, 'owner_id': 42})
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    def create_due(self, content):
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        sid = self.store.create_schedule('-100', start, start + timedelta(hours=1), [content])
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=?',
                                  ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
        return sid, self.store.get_posts(sid)[0]['id']

    async def start_blocked_worker(self):
        task = asyncio.create_task(delivery_worker(self.app))
        self.tasks.append(task)
        await asyncio.wait_for(self.limiter.entered.wait(), 1)
        self.assertFalse(task.done())
        return task

    async def test_pending_album_can_be_replaced_and_worker_claims_fresh_content(self):
        album = {'kind': 'album', 'media': [{'file_id': 'a'}, {'file_id': 'b'}]}
        replacement = {'kind': 'text', 'text': 'replacement'}
        sid, post_id = self.create_due(album)
        reported = asyncio.Event()

        async def report(*args):
            reported.set()

        with patch('main.send_content', new_callable=AsyncMock) as send, \
                patch('main.notify', side_effect=report):
            await self.start_blocked_worker()
            self.assertEqual(self.limiter.calls, [('-100', 2)])
            self.assertEqual(self.store.get_posts(sid)[0]['status'], 'pending')
            send.assert_not_awaited()
            self.assertTrue(self.store.replace_post(post_id, replacement))
            self.limiter.release.set()
            await asyncio.wait_for(reported.wait(), 1)
            send.assert_awaited_once_with(self.app.bot, '-100', replacement,
                                         rate_limit_args={'priority': 10})
            self.assertEqual(self.store.get_posts(sid), [])

    async def test_deleted_candidate_is_not_sent_after_readiness(self):
        sid, post_id = self.create_due({'kind': 'text', 'text': 'delete me'})
        idle = asyncio.Event()
        never = asyncio.Event()

        async def idle_sleep(delay):
            self.assertEqual(delay, 1)
            idle.set()
            await never.wait()

        with patch('main.send_content', new_callable=AsyncMock) as send, \
                patch('main.notify', new_callable=AsyncMock) as notify, \
                patch('main.asyncio.sleep', side_effect=idle_sleep):
            await self.start_blocked_worker()
            self.assertEqual(self.store.get_posts(sid)[0]['status'], 'pending')
            self.assertTrue(self.store.delete_post(post_id))
            self.limiter.release.set()
            await asyncio.wait_for(idle.wait(), 1)
            send.assert_not_awaited()
            notify.assert_not_awaited()
            self.assertIsNone(self.store.peek_due())
            self.assertEqual(self.store.get_posts(sid), [])


if __name__ == '__main__':
    unittest.main()
