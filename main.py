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
from telegram.ext import Application, BaseRateLimiter

from bot import register_handlers, send_content
from config import webhook_config
from error_reporting import application_error
from storage import Store

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ('message', 'callback_query', 'channel_post', 'edited_channel_post')


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
        limiter = getattr(app.bot, 'rate_limiter', None)
        if isinstance(limiter, BaseRateLimiter):
            candidate = store.peek_due()
            if not candidate:
                await asyncio.sleep(1)
                continue
            content = candidate['content']
            cost = len(content['media']) if content['kind'] == 'album' else 1
            await limiter.wait_for_readiness(candidate['chat_id'], cost=cost)
        # Readiness reserves nothing. Claim rechecks the current pending queue,
        # including deletions/replacements made while the worker was waiting.
        post = store.claim()
        if not post:
            await asyncio.sleep(1)
            continue
        attempted_at = datetime.now(timezone.utc)
        log.info('Delivery %s', delivery_details(post, attempted_at, 'attempt'))
        try:
            await send_content(app.bot, post['chat_id'], post['content'],
                                           rate_limit_args={'priority': 10})
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


async def notify_startup(app):
    pool_id = app.bot_data.get('pool_chat_id')
    if pool_id is None:
        log.info('Pool startup notification disabled: POOL_CHAT_ID is not configured')
        return
    from pool import STARTUP_PREFIX
    text = STARTUP_PREFIX + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    try:
        await app.bot.send_message(chat_id=pool_id, text=text, parse_mode=None)
    except Exception as exc:
        error = type(exc).__name__
        log.warning('Pool startup notification failed error_type=%s; no retry', error)
        try:
            await app.bot.send_message(
                chat_id=app.bot_data['owner_id'],
                text=f'Pool startup notification failed: {error}. '
                'Check POOL_CHAT_ID and pool posting permissions. '
                'Telegram may have accepted the message; it will not be retried. '
                'Scheduler workers will still start.', parse_mode=None)
        except Exception as report_exc:
            log.warning('Startup owner report failed error_type=%s', type(report_exc).__name__)


async def startup(app):
    pool_id = app.bot_data.get('pool_chat_id')
    destination = app.bot_data['chat_id']
    if pool_id and isinstance(destination, str) and destination.startswith('@'):
        resolved = await app.bot.get_chat(destination)
        if resolved.id == pool_id:
            raise ValueError('Pool and destination must be different channels.')
    await notify_startup(app)
    from pool import worker
    app.bot_data['worker'] = asyncio.create_task(delivery_worker(app))
    app.bot_data['pool_worker'] = asyncio.create_task(worker(app))


async def stop(app):
    tasks = [app.bot_data.get('worker'), app.bot_data.get('pool_worker')]
    for task in tasks:
        if task:
            task.cancel()
    for task in tasks:
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass


def open_database_lock(path):
    """Create the sibling lock file and take the process-wide scheduler lock."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(str(path) + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('Another scheduler is already using this database.') from None
    return lock


def run_application(app, webhook):
    """Run one mode. An empty WEBHOOK_URL means polling; otherwise webhooks."""
    if webhook is None:
        log.info('Starting long polling: WEBHOOK_URL is empty')
        app.run_polling(drop_pending_updates=False, allowed_updates=list(ALLOWED_UPDATES))
        return
    log.info('Starting webhook listener host=%s port=%s path=%s url=%s',
             webhook.host, webhook.port, webhook.path, webhook.url)
    app.run_webhook(listen=webhook.host, port=webhook.port, url_path=webhook.path,
                    webhook_url=webhook.url, secret_token=webhook.secret,
                    drop_pending_updates=False, allowed_updates=list(ALLOWED_UPDATES))


def main():
    load_dotenv(Path(__file__).resolve().parent / '.env', override=False)
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)sZ %(levelname)s %(name)s %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        webhook = webhook_config(os.environ)
    except ValueError as exc:
        raise SystemExit(f'Invalid webhook configuration: {exc}') from None
    token = os.environ['BOT_TOKEN']
    owner = int(os.environ['OWNER_ID'])
    destination = os.environ['CHAT_ID']
    destination = int(destination) if destination.lstrip('-').isdigit() else destination
    path = Path(os.environ.get('DATABASE_PATH', 'data/scheduler.sqlite3'))
    # Recovery must not mark another live worker's requests as uncertain.
    lock = open_database_lock(path)
    store = Store(path)
    path.chmod(0o600)
    from rate_limiter import TelegramRateLimiter
    app = (Application.builder().token(token).concurrent_updates(False)
           .rate_limiter(TelegramRateLimiter())
           .post_init(startup).post_stop(stop).build())
    pool_id = os.environ.get('POOL_CHAT_ID', '').strip()
    app.bot_data.update(store=store, chat_id=destination, owner_id=owner,
                        pool_chat_id=int(pool_id) if pool_id else None)
    if pool_id and (str(destination) == str(int(pool_id))):
        raise SystemExit('POOL_CHAT_ID must differ from CHAT_ID to avoid treating deliveries as pool sources.')
    app.add_error_handler(application_error)
    register_handlers(app, owner)
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        run_application(app, webhook)
    finally:
        store.close()
        lock.close()


if __name__ == '__main__':
    main()
