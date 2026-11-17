"""Shared PTB request admission. Policies are conservative, not Telegram guarantees."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import timedelta
from itertools import count

from telegram.error import RetryAfter
from telegram.ext import BaseRateLimiter

log = logging.getLogger(__name__)


class TelegramRateLimiter(BaseRateLimiter):
    def __init__(self, global_rate=20.0, per_chat_interval=1.1,
                 group_max_per_min=20, max_retries=3, cooldown_margin=0.1,
                 clock=time.monotonic, sleep_func=asyncio.sleep):
        for name, value in [('global_rate', global_rate), ('per_chat_interval', per_chat_interval)]:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        if not isinstance(group_max_per_min, int) or group_max_per_min < 10:
            raise ValueError('group_max_per_min must be an integer >= 10 (one full album)')
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError('max_retries must be a nonnegative integer')
        if not math.isfinite(cooldown_margin) or cooldown_margin < 0:
            raise ValueError('cooldown_margin must be nonnegative and finite')
        self.global_rate = global_rate
        self.per_chat_interval = per_chat_interval
        self.group_max_per_min = group_max_per_min
        self.max_retries = max_retries
        self.cooldown_margin = cooldown_margin
        self._clock, self._sleep = clock, sleep_func
        self._global_next = self._backoff_until = 0.0
        self._chat_next = {}
        self._windows = {}
        self._queue = []
        self._readiness_events = set()
        self._sequence = count()
        self._closed = False

    async def initialize(self):
        self._closed = False

    async def shutdown(self):
        self._closed = True
        self._wake()

    def _wake(self):
        for event in self._readiness_events:
            event.set()
        for ticket in self._queue:
            ticket[4].set()

    @staticmethod
    def _restricted(chat_id):
        # Channels and unresolved usernames deliberately get the stricter policy.
        return chat_id is not None and (isinstance(chat_id, str) or chat_id < 0)

    @staticmethod
    def _chat_key(chat_id):
        if isinstance(chat_id, str):
            try:
                return int(chat_id)
            except ValueError:
                return chat_id.lower()
        return chat_id

    def _delay(self, chat_id, cost):
        now = self._clock()
        deadline = max(self._global_next, self._backoff_until)
        if chat_id is not None:
            deadline = max(deadline, self._chat_next.get(chat_id, 0))
        if self._restricted(chat_id):
            window = self._windows.setdefault(chat_id, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            excess = len(window) + cost - self.group_max_per_min
            if excess > 0:
                deadline = max(deadline, window[excess - 1] + 60)
        return max(0.0, deadline - now)

    async def _wait(self, event, delay):
        tasks = [asyncio.create_task(event.wait())]
        if delay is not None:
            tasks.append(asyncio.create_task(self._sleep(delay)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_readiness(self, chat_id, cost=1):
        """Nonreserving hint; callers must still recheck state and use the API limiter."""
        chat_id = self._chat_key(chat_id)
        self._validate_cost(cost)
        event = asyncio.Event()
        self._readiness_events.add(event)
        try:
            while True:
                if self._closed:
                    raise RuntimeError('Rate limiter is shut down')
                event.clear()
                delay = self._delay(chat_id, cost)
                if delay <= 0:
                    return
                await self._wait(event, delay)
        finally:
            self._readiness_events.remove(event)

    @staticmethod
    def _validate_cost(cost):
        if not isinstance(cost, int) or not 1 <= cost <= 10:
            raise ValueError('message cost must be an integer from 1 to 10')

    async def acquire(self, chat_id=None, cost=1, priority=0):
        chat_id = self._chat_key(chat_id)
        self._validate_cost(cost)
        ticket = (priority, next(self._sequence), chat_id, cost, asyncio.Event())
        self._queue.append(ticket)
        self._wake()
        try:
            while True:
                if self._closed:
                    raise RuntimeError('Rate limiter is shut down')
                ticket[4].clear()
                # FIFO within each priority/chat; blocked chats do not freeze others.
                ordered = sorted(self._queue, key=lambda t: (-t[0], t[1]))
                eligible = next((t for t in ordered if self._delay(t[2], t[3]) <= 0), None)
                if eligible is ticket:
                    now = self._clock()
                    self._global_next = now + cost / self.global_rate
                    if chat_id is not None:
                        self._chat_next[chat_id] = now + cost * self.per_chat_interval
                    if self._restricted(chat_id):
                        self._windows[chat_id].extend([now] * cost)
                    return
                delays = [self._delay(t[2], t[3]) for t in ordered]
                positive = [d for d in delays if d > 0]
                await self._wait(ticket[4], min(positive) if positive else None)
        finally:
            self._queue.remove(ticket)
            self._wake()

    async def register_retry_after(self, retry_after):
        delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError('retry_after must be nonnegative and finite')
        self._backoff_until = max(self._backoff_until, self._clock() + delay + self.cooldown_margin)
        self._wake()

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        options = rate_limit_args or {}
        priority = options.get('priority', 20 if endpoint == 'answerCallbackQuery' else 0)
        if not isinstance(priority, int):
            raise ValueError('priority must be an integer')
        sends = endpoint.startswith('send') or endpoint in ('copyMessage', 'forwardMessage')
        chat_id = data.get('chat_id') if sends else None
        cost = len(data.get('media', [])) if endpoint == 'sendMediaGroup' else 1
        for attempt in range(self.max_retries + 1):
            await self.acquire(chat_id, cost, priority)
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as exc:
                await self.register_retry_after(exc.retry_after)
                log.warning('Telegram rate limited endpoint=%s attempt=%s', endpoint, attempt + 1)
                if attempt == self.max_retries:
                    raise
