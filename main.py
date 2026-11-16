"""Run with python main.py. Configure using environment variables (see README)."""
import asyncio
import fcntl
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application

from bot import register_handlers, send_content
from storage import Store

log = logging.getLogger(__name__)


def delivery_details(post, attempted_at, result):
    now = datetime.now(timezone.utc)
    due = datetime.fromisoformat(post['due']).astimezone(timezone.utc)
    lateness = max(0, (attempted_at - due).total_seconds())
    return (f"server_utc={now.isoformat()} due_utc={due.isoformat()} "
            f"attempt_utc={attempted_at.isoformat()} result={result} "
            f"schedule_id={post['schedule_id']} post_id={post['id']} "
            f"destination_id={post['chat_id']} lateness_seconds={lateness:.3f}")


async def delivery_worker(app):
    store = app.bot_data['store']
    while True:
        post = store.claim()
        if not post:
            await asyncio.sleep(1)
            continue
        attempted_at = datetime.now(timezone.utc)
        log.info('Delivery %s', delivery_details(post, attempted_at, 'attempt'))
        try:
            await send_content(app.bot, post['chat_id'], post['content'])
        except RetryAfter as exc:
            # Telegram explicitly rejected this attempt; retry after its cooldown.
            delay = exc.retry_after
            log.warning('Delivery %s', delivery_details(post, attempted_at, 'rate_limited'))
            await notify(app, post, attempted_at, 'failure: rate limited; automatic retry after cooldown')
            await asyncio.sleep(delay.total_seconds() if isinstance(delay, timedelta) else delay)
            store.hold(post['id'], 'pending', type(exc).__name__)
        except (BadRequest, Forbidden) as exc:
            store.hold(post['id'], 'failed', type(exc).__name__)
            log.warning('Delivery %s', delivery_details(post, attempted_at, 'failure'))
            await notify(app, post, attempted_at, 'failure: Telegram rejected delivery; open Needs attention')
        except asyncio.CancelledError:
            store.hold(post['id'], 'uncertain', 'Stopped during delivery')
            log.warning('Delivery %s', delivery_details(post, attempted_at, 'uncertain'))
            await notify(app, post, attempted_at, 'uncertain: stopped during delivery; check destination before retrying')
            raise
        except Exception as exc:
            # Transport errors and unexpected failures must never trigger a resend.
            log.warning('Delivery %s error_type=%s',
                        delivery_details(post, attempted_at, 'uncertain'), type(exc).__name__)
            store.hold(post['id'], 'uncertain', type(exc).__name__)
            await notify(app, post, attempted_at, 'uncertain: check destination before retrying; open Needs attention')
        else:
            store.finish(post['id'])
            log.info('Delivery %s', delivery_details(post, attempted_at, 'success'))
            await notify(app, post, attempted_at, 'success')


async def notify(app, post, attempted_at, result):
    # Reporting is separate from delivery: a failed report must never resend a post.
    try:
        await app.bot.send_message(
            chat_id=app.bot_data['owner_id'],
            text='Delivery report\n' + delivery_details(post, attempted_at, result),
        )
    except Exception as exc:
        log.warning('Owner report failed %s owner_id=%s error_type=%s',
                    delivery_details(post, attempted_at, result),
                    app.bot_data['owner_id'], type(exc).__name__)


async def startup(app):
    app.bot_data['worker'] = asyncio.create_task(delivery_worker(app))


async def stop(app):
    task = app.bot_data.get('worker')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    load_dotenv(Path(__file__).resolve().parent / '.env', override=False)
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)sZ %(levelname)s %(name)s %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    token = os.environ['BOT_TOKEN']
    owner = int(os.environ['OWNER_ID'])
    destination = os.environ['CHAT_ID']
    destination = int(destination) if destination.lstrip('-').isdigit() else destination
    path = Path(os.environ.get('DATABASE_PATH', 'data/scheduler.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Recovery must not mark another live worker's requests as uncertain.
    lock = open(str(path) + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('Another scheduler is already using this database.')
    store = Store(path)
    path.chmod(0o600)
    app = (Application.builder().token(token).concurrent_updates(False)
           .post_init(startup).post_stop(stop).build())
    app.bot_data.update(store=store, chat_id=destination, owner_id=owner)
    register_handlers(app, owner)
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        app.run_polling(drop_pending_updates=False)
    finally:
        store.close()
        lock.close()


if __name__ == '__main__':
    main()
