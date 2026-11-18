"""Tracked-source updates and durable status replies. No delivery calls.

New channel posts are deliberately not imported: pool sources become tracked
records only when the owner commits a forwarded draft or replacement.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from telegram import MessageEntity, ReplyParameters
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
    """Process pool-channel updates needed by existing tracked sources.

    New posts are not imported. Edits refresh snapshots for available/pending
    tracked sources and their linked pending queue entries; they never create a
    tracked record. Status replies and the reserved startup notice are ignored.
    """
    edited = getattr(update, 'edited_channel_post', None)
    message = edited or getattr(update, 'channel_post', None)
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
    if not edited:
        return
    content = content_of(message)
    if not content:
        return
    data['store'].update_asset_content(message.chat_id, message.message_id, content)


def marker(job):
    reason = job.get('status_reason')
    if job['status'] == 'available':
        return {
            'deleted': '♻️ Available — removed from the schedule; ready to schedule again.',
            'replaced': '♻️ Available — replaced in the schedule by another asset; ready to schedule again.',
        }[reason]
    label = {'pending': '⏳ Pending', 'published': '✅ Published'}[job['status']]
    if job['status'] == 'pending' and reason == 'replacement':
        label += ' (replacement for another scheduled asset)'
    stamp = datetime.fromisoformat(job['stamp'])
    if stamp.tzinfo is None:
        raise ValueError('Pool status timestamp must be timezone-aware')
    text = f"{label} — {stamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    return text



async def maintain(app):
    store = app.bot_data['store']
    for job in store.status_jobs():
        if (job['status'] not in ('available', 'pending', 'published')
                or (job['status'] == 'available'
                    and job.get('status_reason') not in ('deleted', 'replaced'))):
            store.status_done(job['id'], job['revision'])
            continue
        text = marker(job)
        if not store.claim_status(job['id'], job['revision'], text):
            continue
        try:
            result = await app.bot.send_message(
                chat_id=int(job['source_chat']), text=text, parse_mode=None,
                rate_limit_args={'priority': -10},
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
    await mark_published_sources(app)
    for job in store.uncertain_notifications():
        if await report_failure(app, job, None):
            store.notification_reported(job['id'], job['revision'])


def marked_content(payload):
    """Append the cleanup tag without changing original text or entity offsets."""
    is_text = payload['kind'] == 'text'
    item = payload if is_text else payload['media'][0]
    original = item.get('text' if is_text else 'caption') or ''
    value = original if original == '#to_delete' or original.endswith('\n\n#to_delete') else (
        original + '\n\n#to_delete' if original else '#to_delete')
    # Telegram entity offsets use UTF-16. Conservatively use the same units for limits.
    if len(value.encode('utf-16-le')) // 2 > (4096 if is_text else 1024):
        raise ValueError('Original content plus tag exceeds the Telegram limit')
    entities = list(item.get('entities' if is_text else 'caption_entities') or [])
    return value, entities


async def mark_published_sources(app):
    """Durable pool-only edits; failures never retry destination publication."""
    store = app.bot_data['store']
    for job in store.marking_jobs():
        if not store.claim_mark(job['id']):
            continue
        try:
            value, entities = marked_content(job['payload'])
            entities = [MessageEntity.de_json(entity, app.bot) for entity in entities]
            kwargs = dict(chat_id=int(job['source_chat']), message_id=job['source_id'],
                          parse_mode=None, rate_limit_args={'priority': -10})
            if job['kind'] == 'text':
                await app.bot.edit_message_text(text=value, entities=entities, **kwargs)
            else:
                await app.bot.edit_message_caption(caption=value, caption_entities=entities, **kwargs)
        except RetryAfter as exc:
            store.mark_result(job['id'], 'pending', type(exc).__name__)
            delay = exc.retry_after
            await asyncio.sleep(delay.total_seconds() if hasattr(delay, 'total_seconds') else delay)
            break
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                store.mark_result(job['id'], 'done')
            else:
                store.mark_result(job['id'], 'failed', type(exc).__name__)
        except (Forbidden, ValueError) as exc:
            store.mark_result(job['id'], 'failed', type(exc).__name__)
        except asyncio.CancelledError:
            store.mark_result(job['id'], 'uncertain', 'CancelledError')
            raise
        except Exception as exc:
            store.mark_result(job['id'], 'uncertain', type(exc).__name__)
        else:
            store.mark_result(job['id'], 'done')
    for state in ('failed', 'uncertain'):
        for job in store.marking_jobs(state):
            if job['reported']:
                continue
            try:
                await app.bot.send_message(
                    app.bot_data['owner_id'],
                    f"Pool source marking {state}: asset #{job['asset_id']}, message #{job['source_id']} "
                    f"({job['error']}). Destination publication succeeded and will not be retried. "
                    'Check the source manually, caption/text length, and channel Edit messages permission. '
                    'The tag edit will not be retried automatically.', parse_mode=None)
            except Exception:
                log.warning('Pool source marking report failed job_id=%s', job['id'])
            else:
                store.mark_reported(job['id'])


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
