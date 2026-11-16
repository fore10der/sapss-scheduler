import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from storage import Store, schedule_times

UTC = timezone.utc
CONTENT = {'kind': 'text', 'text': 'Hello', 'entities': []}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.store = Store(self.path)
        self.start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
        self.end = self.start + timedelta(hours=8)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create_due(self, count=1):
        sid = self.store.create_schedule('-100123', self.start, self.end, [CONTENT] * count)
        with self.store.db:
            self.store.db.execute('UPDATE posts SET due=?', ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
        return sid

    def test_spacing_rounding_and_endpoints(self):
        times = schedule_times(self.start, self.start + timedelta(minutes=10), 4)
        self.assertEqual([(t-self.start).seconds // 60 for t in times], [0, 3, 7, 10])
        self.assertEqual(schedule_times(self.start, self.end, 1), [self.start])
        with self.assertRaises(ValueError):
            schedule_times(self.start, self.start + timedelta(minutes=1), 3)

    def test_not_due_and_commit_validation(self):
        self.store.create_schedule('123', self.start, self.end, [CONTENT])
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.create_schedule('123', datetime.now(UTC)-timedelta(minutes=1), self.end, [CONTENT])

    def test_claim_once_then_success_leaves_no_history(self):
        self.create_due()
        post = self.store.claim()
        self.assertIsNone(self.store.claim())
        self.store.finish(post['id'])
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.store.attention(), [])

    def test_restart_holds_interrupted_delivery(self):
        self.create_due()
        post = self.store.claim()
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.attention()[0]['status'], 'uncertain')
        self.store.resolve(post['id'], retry=True)
        self.assertEqual(self.store.claim()['id'], post['id'])

    def test_delete_keeps_inflight_only(self):
        sid = self.create_due(3)
        post = self.store.claim()
        self.assertTrue(self.store.delete_schedule(sid))
        self.assertEqual(len(self.store.get_posts(sid)), 1)
        self.store.finish(post['id'])
        self.assertEqual(self.store.list_schedules(), [])

    def test_dismiss_uncertain_deletes_schedule(self):
        sid = self.create_due()
        post = self.store.claim()
        self.store.hold(post['id'], 'uncertain', 'timeout')
        self.assertFalse(self.store.delete_schedule(sid))
        self.assertEqual(self.store.attention(), [])


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_and_group_updates_are_ignored(self):
        from bot import register_handlers
        app = SimpleNamespace(bot_data={}, handlers=[], add_handler=lambda handler: app.handlers.append(handler))
        register_handlers(app, 42)
        context = SimpleNamespace(user_data={})
        for user_id, chat_type in [(99, 'private'), (42, 'group')]:
            message = SimpleNamespace(text='Create schedule', chat_id=1, reply_text=AsyncMock())
            update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                                     effective_chat=SimpleNamespace(id=1, type=chat_type),
                                     effective_message=message, callback_query=None)
            await app.handlers[1].callback(update, context)
            message.reply_text.assert_not_awaited()
        self.assertEqual(context.user_data, {})

    async def test_original_text_formatting_is_sent(self):
        from bot import send_content
        bot = SimpleNamespace(send_message=AsyncMock(return_value='message'))
        result = await send_content(bot, 42, {'kind': 'text', 'text': 'Hello',
                                            'entities': [{'type': 'bold', 'offset': 0, 'length': 5}]})
        self.assertEqual(result, ['message'])
        self.assertEqual(bot.send_message.call_args.kwargs['text'], 'Hello')
        self.assertEqual(bot.send_message.call_args.kwargs['entities'][0].type, 'bold')


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_startup(self, pool_id, errors=None):
        from main import startup, stop
        app = SimpleNamespace(
            bot_data={'pool_chat_id': pool_id, 'chat_id': -200, 'owner_id': 42},
            bot=SimpleNamespace(send_message=AsyncMock(side_effect=errors)))

        async def idle(app):
            await asyncio.Event().wait()

        with patch('main.delivery_worker', side_effect=idle), patch('pool.worker', side_effect=idle):
            with self.assertLogs('main', level='INFO') as logs:
                await startup(app)
            try:
                self.assertIsInstance(app.bot_data['worker'], asyncio.Task)
                self.assertIsInstance(app.bot_data['pool_worker'], asyncio.Task)
                self.assertFalse(app.bot_data['worker'].done())
                self.assertFalse(app.bot_data['pool_worker'].done())
            finally:
                await stop(app)
        return app.bot.send_message, '\n'.join(logs.output)

    async def test_configured_pool_startup(self):
        # Capture INFO even on the successful path without requiring a success log.
        from main import log
        with patch.object(log, 'info', wraps=log.info):
            from main import startup, stop
            app = SimpleNamespace(bot_data={'pool_chat_id': -100, 'chat_id': -200, 'owner_id': 42},
                                  bot=SimpleNamespace(send_message=AsyncMock()))
            async def idle(app):
                await asyncio.Event().wait()
            async def send(**kwargs):
                self.assertNotIn('worker', app.bot_data)
                self.assertNotIn('pool_worker', app.bot_data)
            app.bot.send_message.side_effect = send
            with patch('main.delivery_worker', side_effect=idle), patch('pool.worker', side_effect=idle):
                before = datetime.now(UTC).replace(microsecond=0)
                await startup(app)
                after = datetime.now(UTC)
                await stop(app)
            app.bot.send_message.assert_awaited_once()
            args = app.bot.send_message.call_args.kwargs
            self.assertEqual(args['chat_id'], -100)
            self.assertIsNone(args['parse_mode'])
            stamp = datetime.strptime(args['text'], 'Scheduler started\nServer time: %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)
            self.assertLessEqual(before, stamp)
            self.assertLessEqual(stamp, after)

    async def test_no_pool_skips_send(self):
        send, logs = await self.run_startup(None)
        send.assert_not_awaited()
        self.assertIn('disabled', logs)

    async def test_failure_reports_owner_and_starts_workers_without_retry(self):
        send, logs = await self.run_startup(-100, [TimeoutError('SECRET token'), None])
        self.assertEqual(send.await_count, 2)
        self.assertEqual(send.call_args_list[0].kwargs['chat_id'], -100)
        self.assertEqual(send.call_args_list[1].kwargs['chat_id'], 42)
        self.assertIn('will not be retried', send.call_args_list[1].kwargs['text'])
        self.assertNotIn('SECRET', logs + str(send.call_args_list))

    async def test_owner_report_failure_also_starts_workers(self):
        send, logs = await self.run_startup(-100, [RuntimeError('SECRET'), RuntimeError('SECRET')])
        self.assertEqual(send.await_count, 2)
        self.assertIn('Startup owner report failed', logs)
        self.assertNotIn('SECRET', logs + str(send.call_args_list))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def check_delivery(self, error=None, report_error=None):
        from main import delivery_worker
        from telegram.error import Forbidden
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            self.addCleanup(store.close)
            start = datetime.now(UTC) + timedelta(hours=1)
            store.create_schedule('-100987', start, start + timedelta(hours=1), [CONTENT])
            due = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            with store.db:
                store.db.execute('UPDATE posts SET due=?', (due,))
            reported = asyncio.Event()

            async def report(**kwargs):
                if error is None:
                    self.assertEqual(store.list_schedules(), [])
                    self.assertIsNone(store.claim())
                self.assertEqual(kwargs['chat_id'], 42)
                text = kwargs['text']
                for field in ['server_utc=', 'due_utc=', 'attempt_utc=', 'result=',
                              'destination_id=-100987', 'lateness_seconds=', 'schedule_id=', 'post_id=']:
                    self.assertIn(field, text)
                self.assertNotIn('Hello', text)
                self.assertNotIn('SECRET', text)
                reported.set()
                if report_error:
                    raise report_error

            app = SimpleNamespace(bot_data={'store': store, 'owner_id': 42},
                                  bot=SimpleNamespace(send_message=AsyncMock(side_effect=report)))
            with patch('main.send_content', new_callable=AsyncMock, side_effect=error) as send:
                with self.assertLogs('main', level='INFO') as logs:
                    task = asyncio.create_task(delivery_worker(app))
                    try:
                        await asyncio.wait_for(reported.wait(), timeout=2)
                        await asyncio.sleep(0.02)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                self.assertEqual(send.await_count, 1)
                output = '\n'.join(logs.output)
                self.assertIn('result=attempt', output)
                self.assertNotIn('SECRET', output)
                self.assertNotIn('Hello', output)
                expected = 'success' if error is None else ('failure' if isinstance(error, Forbidden) else 'uncertain')
                self.assertIn('result=' + expected, output)
                if error is not None:
                    self.assertEqual(store.attention()[0]['status'], 'failed' if expected == 'failure' else expected)
                    self.assertIsNone(store.claim())
                if report_error:
                    self.assertIn('Owner report failed', output)

    async def test_success_removed_before_private_report(self):
        await self.check_delivery()

    async def test_report_failure_does_not_retry_delivered_post(self):
        await self.check_delivery(report_error=RuntimeError('SECRET token'))

    async def test_rejection_report_and_safe_logs(self):
        from telegram.error import Forbidden
        await self.check_delivery(error=Forbidden('SECRET content'))

    async def test_uncertain_report_failure_retains_attention(self):
        await self.check_delivery(error=TimeoutError('SECRET token'),
                                  report_error=RuntimeError('SECRET token'))
    async def test_timeout_is_held_not_retried(self):
        from main import delivery_worker
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            start = datetime.now(UTC) + timedelta(hours=1)
            store.create_schedule('123', start, start + timedelta(hours=1), [CONTENT])
            with store.db:
                store.db.execute('UPDATE posts SET due=?', ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
            app = SimpleNamespace(bot_data={'store': store, 'owner_id': 123}, bot=SimpleNamespace(send_message=AsyncMock()))
            with patch('main.send_content', new_callable=AsyncMock, side_effect=TimeoutError('lost response')) as send:
                task = asyncio.create_task(delivery_worker(app))
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(send.await_count, 1)
                self.assertEqual(store.attention()[0]['status'], 'uncertain')
                self.assertIsNone(store.claim())
            store.close()


if __name__ == '__main__':
    unittest.main()
