import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from telegram.error import NetworkError, RetryAfter

from rate_limiter import TelegramRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleepers = {}

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers[future] = self.now + delay
        try:
            await future
        finally:
            self.sleepers.pop(future, None)

    def advance(self, seconds):
        self.now += seconds
        for future, deadline in list(self.sleepers.items()):
            if deadline <= self.now and not future.done():
                future.set_result(None)


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.tasks = []
        self.limiter = self.make_limiter()

    def make_limiter(self, **options):
        return TelegramRateLimiter(clock=self.clock, sleep_func=self.clock.sleep, **options)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def settle(self):
        # _wait creates and cleans up child tasks before an acquisition can resume.
        for _ in range(30):
            await asyncio.sleep(0)

    async def advance(self, seconds):
        self.clock.advance(seconds)
        await self.settle()

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.settle()
        self.assertFalse(self.clock.sleepers, 'limiter left sleep tasks behind')

    async def test_smooth_global_budget_has_no_initial_burst(self):
        limiter = self.make_limiter(global_rate=2, per_chat_interval=0.1)
        await limiter.acquire(1)
        second = self.start(limiter.acquire(2))
        third = self.start(limiter.acquire(3))
        await self.settle()
        self.assertFalse(second.done())
        self.assertFalse(third.done())
        await self.advance(0.49)
        self.assertFalse(second.done())
        await self.advance(0.01)
        self.assertTrue(second.done())
        self.assertFalse(third.done())
        await self.advance(0.5)
        await third

    async def test_chat_budget_is_independent_and_numeric_strings_share_it(self):
        limiter = self.make_limiter(global_rate=100, per_chat_interval=2)
        await limiter.acquire(123)
        same = self.start(limiter.acquire('123'))
        other = self.start(limiter.acquire(456))
        await self.settle()
        await self.advance(0.02)
        await other
        self.assertFalse(same.done())
        await self.advance(1.98)
        await same

    async def test_album_charges_global_and_chat_budget_per_item(self):
        limiter = self.make_limiter(global_rate=2, per_chat_interval=1)
        await limiter.acquire(1, cost=3)
        other = self.start(limiter.acquire(2))
        same = self.start(limiter.acquire(1))
        await self.settle()
        await self.advance(1.49)
        self.assertFalse(other.done())
        await self.advance(0.01)
        await other
        await self.advance(1.49)
        self.assertFalse(same.done())
        await self.advance(0.01)
        await same

    async def test_negative_and_username_chats_have_rolling_album_windows(self):
        for chat, alias in [(-123, '-123'), ('@Channel', '@CHANNEL')]:
            with self.subTest(chat=chat):
                limiter = self.make_limiter(global_rate=100, per_chat_interval=0.01,
                                            group_max_per_min=10)
                await limiter.acquire(chat, cost=10)
                waiting = self.start(limiter.acquire(alias))
                await self.settle()
                await self.advance(59.99)
                self.assertFalse(waiting.done())
                await self.advance(0.01)
                await waiting

    async def test_positive_chats_do_not_have_group_window(self):
        limiter = self.make_limiter(global_rate=100, per_chat_interval=0.01,
                                    group_max_per_min=10)
        await limiter.acquire(123, cost=10)
        waiting = self.start(limiter.acquire(123, cost=10))
        await self.settle()
        await self.advance(0.11)
        await waiting

    async def test_rolling_window_releases_only_expired_item_costs(self):
        limiter = self.make_limiter(global_rate=100, per_chat_interval=0.01,
                                    group_max_per_min=10)
        await limiter.acquire(-1, cost=6)
        await self.advance(10)
        await limiter.acquire(-1, cost=4)
        waiting = self.start(limiter.acquire(-1, cost=7))
        await self.settle()
        await self.advance(50)
        self.assertFalse(waiting.done())
        await self.advance(10)
        await waiting

    async def test_priority_then_fifo_among_eligible_chats(self):
        limiter = self.make_limiter(global_rate=1, per_chat_interval=0.01)
        await limiter.acquire(99)
        order = []

        async def request(chat, priority):
            await limiter.acquire(chat, priority=priority)
            order.append(chat)

        for chat, priority in [(1, 0), (2, 10), (3, 10), (4, -1)]:
            self.start(request(chat, priority))
        await self.settle()
        for expected in [[2], [2, 3], [2, 3, 1], [2, 3, 1, 4]]:
            await self.advance(1)
            self.assertEqual(order, expected)

    async def test_blocked_high_priority_chat_does_not_block_eligible_chat(self):
        limiter = self.make_limiter(global_rate=10, per_chat_interval=10)
        await limiter.acquire(1)
        blocked = self.start(limiter.acquire(1, priority=100))
        eligible = self.start(limiter.acquire(2, priority=0))
        await self.settle()
        await self.advance(0.11)
        await eligible
        self.assertFalse(blocked.done())
        await self.advance(10)
        await blocked

    async def test_cancellation_removes_ticket_without_consuming_budget(self):
        limiter = self.make_limiter(global_rate=1, per_chat_interval=1)
        await limiter.acquire(-1)
        cancelled = self.start(limiter.acquire(-1, cost=10, priority=100))
        survivor = self.start(limiter.acquire(-1))
        await self.settle()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await self.advance(1)
        await survivor
        self.assertEqual(len(limiter._windows[-1]), 2)
        self.assertEqual(limiter._queue, [])
        await self.settle()
        self.assertFalse(self.clock.sleepers)

    async def test_shutdown_wakes_all_queued_acquisitions(self):
        await self.limiter.acquire(1)
        waiting = [self.start(self.limiter.acquire(chat)) for chat in [1, 2, 3]]
        await self.settle()
        await self.limiter.shutdown()
        await self.settle()
        for task in waiting:
            self.assertTrue(task.done(), 'shutdown did not wake queued acquisition')
            with self.assertRaisesRegex(RuntimeError, 'shut down'):
                await task
        self.assertEqual(self.limiter._queue, [])
        with self.assertRaises(RuntimeError):
            await self.limiter.acquire(4)

    async def test_shutdown_wakes_readiness_waiter(self):
        await self.limiter.acquire(1)
        waiting = self.start(self.limiter.wait_for_readiness(1))
        await self.settle()
        await self.limiter.shutdown()
        await self.settle()
        self.assertTrue(waiting.done(), 'shutdown did not wake readiness waiter')
        with self.assertRaisesRegex(RuntimeError, 'shut down'):
            await waiting

    async def test_readiness_is_nonreserving_and_initialize_reopens(self):
        await self.limiter.wait_for_readiness(1)
        await self.limiter.wait_for_readiness(1)
        await self.limiter.acquire(1)
        await self.limiter.shutdown()
        with self.assertRaises(RuntimeError):
            await self.limiter.wait_for_readiness(2)
        await self.limiter.initialize()
        await self.advance(2)
        await self.limiter.acquire(2)

    async def test_retry_after_numeric_and_timedelta_share_and_extend_cooldown(self):
        for value in [2, timedelta(seconds=2)]:
            with self.subTest(value=value):
                limiter = self.make_limiter(cooldown_margin=0.25)
                waiting = self.start(limiter.acquire(1))
                await limiter.register_retry_after(value)
                await self.settle()
                await self.advance(1)
                await limiter.register_retry_after(2)
                await limiter.register_retry_after(0)
                other = self.start(limiter.acquire(2, priority=100))
                await self.settle()
                await self.advance(2.24)
                self.assertFalse(waiting.done())
                self.assertFalse(other.done())
                await self.advance(0.01)
                await other
                self.assertFalse(waiting.done())
                await self.advance(0.1)
                await waiting

    async def test_retry_after_retries_are_bounded_and_last_failure_sets_cooldown(self):
        for retries in [0, 2]:
            with self.subTest(retries=retries):
                limiter = self.make_limiter(max_retries=retries, cooldown_margin=0)
                error = RetryAfter(1)
                callback = AsyncMock(side_effect=error)
                request = self.start(limiter.process_request(callback, (), {}, 'sendMessage',
                                                             {'chat_id': 1}, None))
                with self.assertLogs('rate_limiter', level='WARNING'):
                    await self.settle()
                    for _ in range(retries):
                        await self.advance(2)
                    with self.assertRaises(RetryAfter) as raised:
                        await request
                self.assertIs(raised.exception, error)
                self.assertEqual(callback.await_count, retries + 1)
                self.assertGreater(limiter._backoff_until, self.clock())

    async def test_retry_after_then_success_preserves_callback_arguments(self):
        limiter = self.make_limiter(cooldown_margin=0)
        callback = AsyncMock(side_effect=[RetryAfter(timedelta(seconds=2)), 'ok'])
        request = self.start(limiter.process_request(callback, ('arg',), {'key': 'value'},
                                                     'sendMessage', {'chat_id': 1}, None))
        with self.assertLogs('rate_limiter', level='WARNING'):
            await self.settle()
            await self.advance(1.99)
            self.assertEqual(callback.await_count, 1)
            await self.advance(0.01)
            self.assertEqual(await request, 'ok')
        self.assertEqual(callback.await_count, 2)
        callback.assert_awaited_with('arg', key='value')

    async def test_network_error_is_not_retried(self):
        error = NetworkError('connection lost')
        callback = AsyncMock(side_effect=error)
        with self.assertRaises(NetworkError) as raised:
            await self.limiter.process_request(callback, (), {}, 'sendMessage',
                                               {'chat_id': 1}, None)
        self.assertIs(raised.exception, error)
        callback.assert_awaited_once()
        self.assertEqual(self.limiter._backoff_until, 0)

    async def test_process_request_infers_message_cost_chat_and_callback_priority(self):
        cases = [
            ('sendMessage', {'chat_id': '-1'}, None, ('-1', 1, 0)),
            ('sendMediaGroup', {'chat_id': 1, 'media': [object()] * 7}, None, (1, 7, 0)),
            ('copyMessage', {'chat_id': 2}, None, (2, 1, 0)),
            ('forwardMessage', {'chat_id': 3}, None, (3, 1, 0)),
            ('answerCallbackQuery', {'chat_id': 4}, None, (None, 1, 20)),
            ('answerCallbackQuery', {}, {'priority': -5}, (None, 1, -5)),
            ('editMessageText', {'chat_id': 5}, {'priority': 8}, (None, 1, 8)),
            ('getMe', {}, None, (None, 1, 0)),
        ]
        for endpoint, data, options, expected in cases:
            with self.subTest(endpoint=endpoint, options=options):
                limiter = self.make_limiter()
                limiter.acquire = AsyncMock()
                callback = AsyncMock(return_value='result')
                result = await limiter.process_request(callback, (1,), {'x': 2},
                                                       endpoint, data, options)
                self.assertEqual(result, 'result')
                limiter.acquire.assert_awaited_once_with(*expected)
                callback.assert_awaited_once_with(1, x=2)

    async def test_nonmessage_request_consumes_global_but_not_chat_budget(self):
        limiter = self.make_limiter(global_rate=2, per_chat_interval=10)
        callback = AsyncMock(return_value='ok')
        await limiter.process_request(callback, (), {}, 'getChat', {'chat_id': 1}, None)
        request = self.start(limiter.process_request(callback, (), {}, 'sendMessage',
                                                     {'chat_id': 1}, None))
        await self.settle()
        self.assertEqual(callback.await_count, 1)
        await self.advance(0.5)
        await request
        self.assertEqual(callback.await_count, 2)

    async def test_invalid_costs_and_priority_do_not_invoke_callback_or_queue(self):
        for cost in [0, -1, 11, 1.5, '1', None]:
            for method in [self.limiter.acquire, self.limiter.wait_for_readiness]:
                with self.subTest(cost=cost, method=method.__name__):
                    with self.assertRaises(ValueError):
                        await method(1, cost=cost)
        callback = AsyncMock()
        for media in [[], [None] * 11]:
            with self.assertRaises(ValueError):
                await self.limiter.process_request(callback, (), {}, 'sendMediaGroup',
                                                   {'chat_id': 1, 'media': media}, None)
        for priority in [1.5, 'high', None]:
            with self.assertRaises(ValueError):
                await self.limiter.process_request(callback, (), {}, 'getMe', {},
                                                   {'priority': priority})
        callback.assert_not_awaited()
        self.assertEqual(self.limiter._queue, [])

    async def test_invalid_retry_after(self):
        for value in [-1, float('nan'), float('inf'), timedelta(seconds=-1)]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await self.limiter.register_retry_after(value)
        self.assertEqual(self.limiter._backoff_until, 0)


class ConstructorTests(unittest.TestCase):
    def test_invalid_configuration(self):
        cases = {
            'global_rate': [0, -1, float('nan'), float('inf')],
            'per_chat_interval': [0, -1, float('nan'), float('inf')],
            'group_max_per_min': [0, 9, 10.5, '20'],
            'max_retries': [-1, 1.5, '3'],
            'cooldown_margin': [-1, float('nan'), float('inf')],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        TelegramRateLimiter(**{name: value})


if __name__ == '__main__':
    unittest.main()
