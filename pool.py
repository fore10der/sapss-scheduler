"""Pool ingestion and independent, durable status replies. No delivery calls."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from telegram import ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

log = logging.getLogger(__name__)

STARTUP_PREFIX = 'Scheduler started\nServer time: '
STARTUP_MESSAGE = re.compile(
    re.escape(STARTUP_PREFIX) + r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC')


def content_of(message):
    if message.photo:
        return {'kind': 'photo', 'media': [{
            'file_id': message.photo[-1].file_id, 'caption': message.caption or '',
            'caption_entities': [e.to_dict() for e in message.caption_entities or []]}]}
    if message.text:
        return {'kind': 'text', 'text': message.text,
                'entities': [e.to_dict() for e in message.entities or []]}
    return None


async def ingest(update, context):
    message = getattr(update, 'channel_post', None)
    data = context.application.bot_data
    if not message or str(message.chat_id) != str(data.get('pool_chat_id')):
        return
    # Channel posts may hide the sending bot; reserve only this exact format,
    # including for delayed updates from a previous process.
    if STARTUP_MESSAGE.fullmatch(getattr(message, 'text', None) or ''):
        return
    reply = getattr(message, 'reply_to_message', None)
    if data['store'].is_status_reply(
            message.chat_id, reply.message_id if reply else None,
            getattr(message, 'text', None), message.message_id):
        return
    content = content_of(message)
    if not content:
        return
    if not message.media_group_id:
        data['store'].add_asset(message.chat_id, [message.message_id], content)
        return
    groups = data.setdefault('pool_groups', {})
    key = (message.chat_id, message.media_group_id)
    pending = groups.setdefault(key, {'items': {}})
    if 'task' in pending:
        pending['task'].cancel()
    pending['items'][message.message_id] = content['media'][0]

    async def flush():
        await asyncio.sleep(1.5)
        try:
            items = sorted(pending['items'].items())
            data['store'].add_asset(key[0], [mid for mid, _ in items],
                                   {'kind': 'album', 'media': [item for _, item in items]}, key[1])
        except Exception as exc:
            log.warning('Pool album ingestion failed error_type=%s', type(exc).__name__)
            try:
                await context.bot.send_message(data['owner_id'], 'Could not store an incoming pool album. Check storage and the pool before scheduling.')
            except Exception:
                log.warning('Pool album ingestion report failed')
        finally:
            groups.pop(key, None)
    pending['task'] = asyncio.create_task(flush())


def marker(job):
    label = {'pending': '⏳ Pending', 'published': '✅ Published'}[job['status']]
    stamp = datetime.fromisoformat(job['stamp'])
    if stamp.tzinfo is None:
        raise ValueError('Pool status timestamp must be timezone-aware')
    return f"{label} — {stamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"



async def maintain(app):
    store = app.bot_data['store']
    for job in store.status_jobs():
        if job['status'] not in ('pending', 'published'):
            store.status_done(job['id'], job['revision'])
            continue
        text = marker(job)
        if not store.claim_status(job['id'], job['revision'], text):
            continue
        try:
            result = await app.bot.send_message(
                chat_id=int(job['source_chat']), text=text, parse_mode=None,
                reply_parameters=ReplyParameters(
                    message_id=json.loads(job['source_ids'])[0],
                    allow_sending_without_reply=False))
        except BadRequest as exc:
            reason = str(exc).lower()
            if ('message to be replied not found' in reason
                    or 'message to be replied to not found' in reason
                    or 'reply message not found' in reason):
                store.status_result(job['id'], job['revision'], 'skipped')
                continue
            store.status_result(job['id'], job['revision'], 'retry')
            await report_failure(app, job, exc)
        except (Forbidden, RetryAfter) as exc:
            store.status_result(job['id'], job['revision'], 'retry')
            await report_failure(app, job, exc)
            if isinstance(exc, RetryAfter):
                delay = exc.retry_after
                await asyncio.sleep(delay.total_seconds() if hasattr(delay, 'total_seconds') else delay)
        except Exception as exc:
            if isinstance(exc, TelegramError) and not isinstance(exc, NetworkError):
                store.status_result(job['id'], job['revision'], 'retry')
                await report_failure(app, job, exc)
            else:
                # Unknown outcomes are never automatically sent again, even after restart.
                store.status_result(job['id'], job['revision'], 'uncertain')
                log.warning('Pool status outcome uncertain asset_id=%s error_type=%s',
                            job['id'], type(exc).__name__)
        else:
            store.status_result(job['id'], job['revision'], 'sent', result.message_id)
    for job in store.uncertain_notifications():
        if await report_failure(app, job, None):
            store.notification_reported(job['id'], job['revision'])


async def report_failure(app, job, exc):
    error = type(exc).__name__ if exc else 'uncertain outcome'
    log.warning('Pool status maintenance asset_id=%s error_type=%s', job['id'], error)
    action = ('Check the pool manually: Telegram may have accepted this reply. '
              'This revision will not be resent; newer status revisions can still send.'
              if exc is None else
              'Check pool posting permissions and the source reply target; for rate limits, wait for cooldown. '
              'Status maintenance will retry independently of destination delivery.')
    try:
        await app.bot.send_message(
            app.bot_data['owner_id'],
            f"Pool status #{job['id']} revision {job['revision']}: {error}. {action} "
            'Original sources are unchanged. Publication will not be retried.', parse_mode=None)
        return True
    except Exception:
        log.warning('Pool maintenance owner report failed')
        return False


async def worker(app):
    while True:
        await maintain(app)
        await asyncio.sleep(30)
