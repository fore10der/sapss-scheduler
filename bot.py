"""Owner-only Telegram scheduling UI; persistence and worker live elsewhere.

Public interface:
    register_handlers(application, owner_id)
        Requires application.bot_data['store'] and ['chat_id'].
    build_application(token, owner_id, chat_id, store) -> Application
        Convenience factory; does not start polling or a worker.
    await send_content(bot, chat_id, content) -> list[Message]

All stored datetimes and displayed timestamps are UTC. Store methods are
synchronous. The storage module must also export schedule_times(start, end, count).
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MessageEntity,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
)
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

import storage

logger = logging.getLogger(__name__)
UTC = timezone.utc
MENU = ReplyKeyboardMarkup(
    [["Create schedule", "View schedules"], ["Needs attention"]], resize_keyboard=True
)


def _entities(values, bot):
    return [MessageEntity.de_json(value, bot) for value in (values or [])]


async def send_content(bot, chat_id, content, rate_limit_args=None):
    """Send stored content; previews default to bulk priority unless overridden."""
    if rate_limit_args is None:
        rate_limit_args = {"priority": -10}
    kind = content["kind"]
    if kind == "text":
        message = await bot.send_message(
            chat_id=chat_id,
            text=content["text"],
            entities=_entities(content.get("entities"), bot),
            parse_mode=None,
            rate_limit_args=rate_limit_args,
        )
        return [message]
    media = content.get("media", [])
    if kind == "photo":
        item = media[0]
        message = await bot.send_photo(
            chat_id=chat_id,
            photo=item["file_id"],
            caption=item.get("caption") or None,
            caption_entities=_entities(item.get("caption_entities"), bot),
            parse_mode=None,
            rate_limit_args=rate_limit_args,
        )
        return [message]
    if kind == "album":
        # A lone photo can occur if the draft was cancelled mid-album or Telegram
        # delivered only one item. Telegram does not accept single-item groups.
        if len(media) == 1:
            return await send_content(bot, chat_id, {**content, "kind": "photo"},
                                                  rate_limit_args=rate_limit_args)
        return list(await bot.send_media_group(
            chat_id=chat_id,
            media=[InputMediaPhoto(
                media=item["file_id"],
                caption=item.get("caption") or None,
                caption_entities=_entities(item.get("caption_entities"), bot),
                parse_mode=None,
            ) for item in media],
            rate_limit_args=rate_limit_args,
        ))
    raise ValueError(f"Unsupported post kind: {kind!r}")


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stamp(value):
    return _date(value).strftime("%Y-%m-%d %H:%M UTC")


def _parse(text):
    value = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    if value.strftime("%Y-%m-%d %H:%M") != text:
        raise ValueError("Use YYYY-MM-DD HH:mm (UTC).")
    return value


def _keyboard(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
    ])


def _draft_keyboard(draft):
    key = draft["key"]
    return _keyboard([
        [("Undo last", f"draft:{key}:undo"), ("Preview", f"draft:{key}:preview")],
        [("Commit", f"draft:{key}:commit"), ("Cancel", f"draft:{key}:cancel")],
    ])


def _clear(context):
    draft = context.user_data.pop("draft", None)
    if draft:
        for pending in draft["pending"].values():
            pending["task"].cancel()
    context.user_data.pop("step", None)


def _pool_source(origin, message_ids):
    return {"chat_id": str(origin.chat.id), "message_ids": list(message_ids)}


def _draft_source_ids(draft):
    ids = set()
    for post in draft["posts"]:
        ids.update((post.get("source") or {}).get("message_ids", []))
    for pending in draft["pending"].values():
        ids.update(pending.get("items", {}))
    return ids


def _cancel_replacement_pending(replacement):
    for pending in replacement.get("pending", {}).values():
        task = pending.get("task")
        if task is not None:
            task.cancel()
    replacement["pending"] = {}


async def _summary(context, chat_id):
    draft = context.user_data.get("draft")
    if not draft or "end" not in draft:
        return
    count = len(draft["posts"])
    lines = [f"Draft: {count} post(s)",
             f"Window: {_stamp(draft['start'])} → {_stamp(draft['end'])}"]
    if count:
        try:
            times = storage.schedule_times(draft["start"], draft["end"], count)
            if count > 1:
                gaps = [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])]
                lines.append(f"Spacing: {min(gaps)}–{max(gaps)} minutes")
            lines.extend(f"{index}. {_stamp(due)}" for index, due in enumerate(times, 1))
        except ValueError:
            lines.append("Too many posts: spacing must be at least one minute. Undo a post or create a wider window.")
    if draft["pending"]:
        lines.append("Receiving album… Commit is blocked until it finishes.")
    # Avoid Telegram's message-size limit for large drafts.
    text = "\n".join(lines)
    for offset in range(0, len(text), 3500):
        await context.bot.send_message(
            chat_id, text[offset:offset + 3500],
            reply_markup=_draft_keyboard(draft) if offset + 3500 >= len(text) else None,
        )


async def _album_flush(context, chat_id, draft, group_id):
    try:
        await asyncio.sleep(1.5)
        if context.user_data.get("draft") is not draft:
            return
        pending = draft["pending"].pop(group_id)
        items = sorted(pending["items"].items())
        pending["post"]["media"] = [item for _, item in items]
        if "source_chat" in pending:
            pending["post"]["source"] = {
                "chat_id": str(pending["source_chat"]),
                "message_ids": [source_id for source_id, _ in items],
            }
            detail = ("Forwarded pool album accepted. Source tracking starts only when you Commit; "
                      "only received members will be scheduled.")
        else:
            detail = "Standalone album; no pool source will be tracked."
        await context.bot.send_message(
            chat_id, f"Accepted album: {len(items)} photos. {detail}")
        await _summary(context, chat_id)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Failed to finish album")
        try:
            await context.bot.send_message(chat_id, "Could not update the album summary. Use Preview to inspect your draft.")
        except Exception:
            logger.exception("Failed to notify owner about album error")


async def _replacement_album_flush(context, chat_id, replacement, group_id):
    try:
        await asyncio.sleep(1.5)
        if context.user_data.get("replacement") is not replacement:
            return
        pending = replacement["pending"].pop(group_id, None)
        if pending is None:
            return
        items = sorted(pending["items"].items())
        content = {"kind": "album", "text": "", "entities": [],
                   "media": [item for _, item in items]}
        replacement["content"] = content
        replacement["source"] = {
            "chat_id": str(pending["source_chat"]),
            "message_ids": [source_id for source_id, _ in items],
        }
        await send_content(context.bot, chat_id, content)
        await context.bot.send_message(
            chat_id,
            "Replace with this forwarded album? Only received members will be scheduled. "
            "Posting time stays unchanged.",
            reply_markup=_keyboard([
                [("Confirm replacement", f"replace_yes:{replacement['key']}"),
                 ("Cancel", f"replace_cancel:{replacement['key']}")]]))
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Failed to finish replacement album")
        try:
            await context.bot.send_message(
                chat_id, "Could not update the replacement preview. Forward again or cancel.")
        except Exception:
            logger.exception("Failed to notify owner about replacement album error")


async def _show_posts(context, chat_id, posts, times=None):
    for index, post in enumerate(posts):
        due = times[index] if times is not None else post["due"]
        content = post if times is not None else post["content"]
        status = "" if times is not None else f" · {post['status']}"
        markup = None
        if times is None and post['status'] == 'pending':
            markup = _keyboard([[("Delete post", f"post_delete:{post['id']}"),
                                 ("Replace post", f"post_replace:{post['id']}")]])
        await context.bot.send_message(chat_id, f"{_stamp(due)}{status}", reply_markup=markup)
        await send_content(context.bot, chat_id, content)


async def _list(context, chat_id):
    schedules = context.application.bot_data["store"].list_schedules()
    if not schedules:
        await context.bot.send_message(chat_id, "No schedules.", reply_markup=MENU)
    for schedule in schedules:
        sid = schedule["id"]
        await context.bot.send_message(
            chat_id,
            f"Schedule #{sid} · {schedule['count']} post(s)\n"
            f"{_stamp(schedule['start'])} → {_stamp(schedule['end'])}\n"
            f"Destination: {schedule['chat_id']}",
            reply_markup=_keyboard([[("Details", f"details:{sid}"), ("Delete", f"delete:{sid}")]]),
        )


async def _attention(context, chat_id):
    posts = context.application.bot_data["store"].attention()
    if not posts:
        await context.bot.send_message(chat_id, "Nothing needs attention.", reply_markup=MENU)
    for post in posts:
        await context.bot.send_message(
            chat_id,
            f"Schedule #{post['schedule_id']} · post #{post['id']}\n"
            f"{_stamp(post['due'])} · {post['status']}\n"
            "Delivery may be uncertain. Retrying could create a duplicate.",
            reply_markup=_keyboard([[
                ("Dismiss", f"dismiss:{post['id']}"),
                ("Retry", f"retry:{post['id']}"),
            ]]),
        )
        await send_content(context.bot, chat_id, post["content"])


async def _permission(context):
    target = context.application.bot_data["chat_id"]
    try:
        chat = await context.bot.get_chat(target)
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        status = member.status
        if status in ("left", "kicked"):
            return "The bot is not a member of the destination chat. Add it first."
        if chat.type == "channel" and not (
            status == "creator" or (status == "administrator" and member.can_post_messages)
        ):
            return "The bot needs administrator permission to post messages in the destination channel."
        if status == "restricted":
            if not member.is_member or not member.can_send_messages:
                return "The bot is restricted from sending messages in the destination chat."
            if not getattr(member, "can_send_photos", True):
                return "The bot is restricted from sending photos in the destination chat."
        if chat.type in ("group", "supergroup") and status == "member" and chat.permissions:
            if chat.permissions.can_send_messages is False or chat.permissions.can_send_photos is False:
                return "The destination's default permissions do not allow the bot to send text and photos."
    except Exception:
        logger.exception("Could not verify destination permissions")
        return "Cannot verify destination access. Check CHAT_ID, bot membership, and posting permissions, then try Commit again."
    return None


async def _message(update, context):
    message = update.effective_message
    chat_id = message.chat_id
    text = message.text or ""
    forwarded = getattr(message, 'forward_origin', None) is not None
    if not forwarded and text == "/start":
        await message.reply_text("Choose an action. Dates and times are UTC.", reply_markup=MENU)
        return
    if not forwarded and text == "Create schedule":
        if context.user_data.get('replacement'):
            await message.reply_text('Confirm or cancel the active replacement first.')
            return
        if context.user_data.get("draft"):
            await message.reply_text("A draft already exists. Commit or Cancel it first.")
            draft = context.user_data["draft"]
            await message.reply_text("Draft controls", reply_markup=_draft_keyboard(draft))
            return
        # Per-user monotonic generation makes buttons from previous drafts stale.
        key = uuid.uuid4().hex[:12]
        context.user_data["draft"] = {"key": key, "posts": [], "pending": {}}
        context.user_data["step"] = "start"
        await message.reply_text("Enter the future start in UTC: YYYY-MM-DD HH:mm", reply_markup=_draft_keyboard(context.user_data["draft"]))
        return
    if not forwarded and text == "View schedules":
        await _list(context, chat_id)
        return
    if not forwarded and text == "Needs attention":
        await _attention(context, chat_id)
        return
    replacement = context.user_data.get('replacement')
    if replacement:
        origin = getattr(message, 'forward_origin', None)
        pool_id = context.application.bot_data.get('pool_chat_id')
        if not isinstance(origin, MessageOriginChannel):
            await message.reply_text(
                'Forward an original post from the configured pool channel with attribution intact.')
            return
        if pool_id is None:
            await message.reply_text(
                'Pool forwarding is disabled because POOL_CHAT_ID is not configured. '
                'Set it before replacing pool-sourced posts.')
            return
        if str(origin.chat.id) != str(pool_id):
            await message.reply_text(
                'Forward an original post from the configured pool channel with attribution intact.')
            return
        from pool import content_of
        incoming = content_of(message)
        if incoming is None:
            await message.reply_text('Only text and photos are supported.')
            return
        gid = message.media_group_id
        if gid is not None and gid in replacement.get('rejected_albums', ()):
            await message.reply_text(
                'This forwarded album includes a pool source already published by this '
                'scheduler; the album cannot be used as a replacement.')
            return
        store = context.application.bot_data["store"]
        if store.published_source_ids(str(origin.chat.id), [origin.message_id]):
            if gid is not None:
                replacement.setdefault('rejected_albums', set()).add(gid)
            _cancel_replacement_pending(replacement)
            replacement['content'] = None
            replacement['source'] = None
            await message.reply_text(
                'This pool source was already published by this scheduler; it cannot be '
                'used as a replacement.' + (
                    ' The forwarded album was not added.' if gid is not None else ''))
            return
        if gid is not None:
            if incoming['kind'] != 'photo':
                await message.reply_text('Only photo albums are supported.')
                return
            pending = replacement['pending'].get(gid)
            if pending is None:
                _cancel_replacement_pending(replacement)
                replacement['content'] = None
                replacement['source'] = None
                pending = {'items': {}, 'source_chat': str(origin.chat.id)}
                replacement['pending'][gid] = pending
            if pending['source_chat'] != str(origin.chat.id):
                await message.reply_text('This album is already receiving posts from another source.')
                return
            pending['items'][origin.message_id] = incoming['media'][0]
            task = pending.get('task')
            if task is not None:
                task.cancel()
            pending['task'] = asyncio.create_task(
                _replacement_album_flush(context, chat_id, replacement, gid))
            return
        _cancel_replacement_pending(replacement)
        replacement['content'] = incoming
        replacement['source'] = _pool_source(origin, [origin.message_id])
        await send_content(context.bot, chat_id, incoming)
        await message.reply_text('Replace with this forwarded post? Posting time stays unchanged.',
            reply_markup=_keyboard([[("Confirm replacement", f"replace_yes:{replacement['key']}"),
                                      ("Cancel", f"replace_cancel:{replacement['key']}")]]))
        return
    draft = context.user_data.get("draft")
    if not draft:
        await message.reply_text("Choose Create schedule to begin.", reply_markup=MENU)
        return
    step = context.user_data.get("step")
    if step in ("start", "end"):
        try:
            value = _parse(text)
            if step == "start" and value <= datetime.now(UTC):
                raise ValueError("Start must be in the future.")
            if step == "end" and value <= draft["start"]:
                raise ValueError("End must be later than start.")
        except ValueError as exc:
            await message.reply_text(f"Invalid {step}. {exc}\nUse YYYY-MM-DD HH:mm (UTC).")
            return
        draft[step] = value
        if step == "start":
            context.user_data["step"] = "end"
            await message.reply_text("Enter the end in UTC: YYYY-MM-DD HH:mm")
        else:
            context.user_data["step"] = "posts"
            await message.reply_text("Send text, photos, or photo albums. Formatting and captions are preserved.")
            await _summary(context, chat_id)
        return
    origin = getattr(message, 'forward_origin', None)
    pool_id = context.application.bot_data.get('pool_chat_id')
    gid = getattr(message, 'media_group_id', None)
    pending = draft['pending'].get(gid) if gid else None
    if isinstance(origin, MessageOriginChannel):
        if pool_id is None:
            await message.reply_text(
                'Not added: pool forwarding is disabled because POOL_CHAT_ID is not configured. '
                'Send an original upload for a standalone post.')
            return
        if str(origin.chat.id) != str(pool_id):
            await message.reply_text(
                'Not added: this forward is not attributed to the configured pool channel. '
                'Forward the original from POOL_CHAT_ID with attribution intact.')
            return
        from pool import content_of
        incoming = content_of(message)
        if incoming is None:
            await message.reply_text('Not added: only text and photos are supported.')
            return
        source_chat = str(origin.chat.id)
        if gid is not None and gid in draft.get('rejected_albums', ()):
            await message.reply_text(
                'Not added: this forwarded album includes a pool source already published '
                'by this scheduler.')
            return
        if pending is not None and 'source_chat' not in pending:
            await message.reply_text(
                'Not added: this album already contains standalone uploads. '
                'Cancel the draft album before mixing forwarded pool posts.')
            return
        store = context.application.bot_data["store"]
        if store.published_source_ids(source_chat, [origin.message_id]):
            if gid is not None:
                draft.setdefault('rejected_albums', set()).add(gid)
            discard = list(draft.get('album_posts', {}).pop(gid, [])) if gid is not None else []
            if pending is not None:
                pending['task'].cancel()
                draft['pending'].pop(gid, None)
                if pending['post'] not in discard:
                    discard.append(pending['post'])
            for post in discard:
                if post in draft['posts']:
                    draft['posts'].remove(post)
            await message.reply_text(
                'Not added: this pool source was already published by this scheduler. '
                + ('The forwarded album was not added.' if gid is not None
                   else 'Choose an unpublished original.'))
            await _summary(context, chat_id)
            return
        if pending is not None:
            if incoming['kind'] != 'photo':
                await message.reply_text('Not added: pool album sources must be forwarded as photos.')
                return
            pending['items'][origin.message_id] = incoming['media'][0]
            pending['post']['media'] = [item for _, item in sorted(pending['items'].items())]
            pending['task'].cancel()
            pending['task'] = asyncio.create_task(_album_flush(context, chat_id, draft, gid))
            return
        if origin.message_id in _draft_source_ids(draft):
            await message.reply_text('This pool source is already in the draft; not added again.')
            return
        try:
            storage.schedule_times(draft['start'], draft['end'], len(draft['posts']) + 1)
        except ValueError:
            await message.reply_text("Not accepted: posts must be at least one minute apart. Undo a post or use a wider window.")
            return
        if gid is not None:
            if incoming['kind'] != 'photo':
                await message.reply_text("Only photo albums are supported; non-photo album items were not added.")
                return
            post = {"kind": "album", "text": "", "entities": [], "media": []}
            draft['posts'].append(post)
            draft.setdefault('album_posts', {}).setdefault(gid, []).append(post)
            pending = {"post": post, "items": {origin.message_id: incoming['media'][0]},
                       "source_chat": source_chat}
            draft['pending'][gid] = pending
            pending['post']['media'] = [item for _, item in sorted(pending['items'].items())]
            pending['task'] = asyncio.create_task(_album_flush(context, chat_id, draft, gid))
            await message.reply_text(
                'Accepted forwarded pool album member(s). Receiving the album; only forwarded members will be scheduled.')
            return
        post = {**incoming, 'source': _pool_source(origin, [origin.message_id])}
        draft['posts'].append(post)
        await message.reply_text(
            'Accepted pool forward. Source tracking starts only when you Commit. '
            'Preview to inspect the content and formatting.')
        await _summary(context, chat_id)
        return
    if origin is not None:
        await message.reply_text(
            'Not added: Telegram did not attribute this forward to the configured pool channel. '
            'Forward the original from POOL_CHAT_ID, or send an original upload for a standalone post.')
        return
    if pending is not None and 'source_chat' in pending:
        await message.reply_text(
            'This album is receiving forwarded pool posts; unattributed items were not added. '
            'Forward every album member from the pool channel.')
        return
    if not (gid and gid in draft["pending"]):
        try:
            storage.schedule_times(draft["start"], draft["end"], len(draft["posts"]) + 1)
        except ValueError:
            await message.reply_text("Not accepted: posts must be at least one minute apart. Undo a post or use a wider window.")
            return
    if message.photo:
        item = {
            "file_id": message.photo[-1].file_id,
            "caption": message.caption or "",
            "caption_entities": [entity.to_dict() for entity in (message.caption_entities or [])],
        }
        if gid:
            pending = draft["pending"].get(gid)
            if pending:
                pending["task"].cancel()
            else:
                post = {"kind": "album", "text": "", "entities": [], "media": []}
                draft["posts"].append(post)
                pending = {"post": post, "items": {}}
                draft["pending"][gid] = pending
            pending["items"][message.message_id] = item
            pending["post"]["media"] = [entry for _, entry in sorted(pending["items"].items())]
            pending["task"] = asyncio.create_task(_album_flush(context, chat_id, draft, gid))
            return
        post = {"kind": "photo", "text": "", "entities": [], "media": [item]}
    elif gid:
        await message.reply_text("Only photo albums are supported; non-photo album items were not added.")
        return
    elif message.text:
        post = {"kind": "text", "text": message.text,
                "entities": [entity.to_dict() for entity in (message.entities or [])], "media": []}
    else:
        await message.reply_text("Please send text, a photo, or a photo album. This message was not added.")
        return
    draft["posts"].append(post)
    await message.reply_text(("Accepted text post." if post["kind"] == "text" else "Accepted photo post.") +
                             ' Standalone post; no pool source is tracked. '
                             'Forward a pool original to track its status follow-up.')
    await _summary(context, chat_id)


async def _callback(update, context):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":")
    action = parts[0]
    store = context.application.bot_data["store"]
    if action in ('asset', 'release', 'release_yes'):
        await context.bot.send_message(
            chat_id, 'Asset pool selection was removed. Forward an original from the pool channel '
                     'into your draft instead.')
        return
    if action in ('post_delete', 'post_delete_yes', 'post_replace'):
        pid = int(parts[1])
        if action == 'post_delete':
            await context.bot.send_message(chat_id, 'Remove this post from its schedule? Its tracked source, if any, becomes available again.',
                reply_markup=_keyboard([[("Delete post", f"post_delete_yes:{pid}"), ("Keep", 'noop')]]))
        elif action == 'post_delete_yes':
            changed = store.delete_post(pid)
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id, 'Post removed.' if changed else 'Post is no longer pending. Refresh schedules.')
        else:
            if context.user_data.get('draft'):
                await context.bot.send_message(chat_id, 'Commit or cancel your draft before replacing a scheduled post.')
                return
            pending = next((p for s in store.list_schedules() for p in store.get_posts(s['id'])
                            if p['id'] == pid and p['status'] == 'pending'), None)
            if pending is None:
                await context.bot.send_message(chat_id, 'Post is no longer pending. Refresh schedules.')
                return
            key = uuid.uuid4().hex[:12]
            context.user_data['replacement'] = {
                'post_id': pid, 'key': key, 'pending': {},
                'content': None, 'source': None}
            await context.bot.send_message(chat_id, f"Forward a replacement from the pool channel. Posting: {_stamp(pending['due'])}.",
                reply_markup=_keyboard([[("Cancel replacement", f"replace_cancel:{key}")]]))
        return
    if action in ('replace_yes', 'replace_cancel'):
        replacement = context.user_data.get('replacement')
        if not replacement or replacement['key'] != parts[1]:
            await context.bot.send_message(chat_id, 'This replacement is no longer active.')
            return
        if action == 'replace_cancel':
            _cancel_replacement_pending(replacement)
            context.user_data.pop('replacement', None)
            await context.bot.send_message(chat_id, 'Replacement cancelled.')
        else:
            content = replacement.get('content')
            source = replacement.get('source')
            if not content or not source:
                await context.bot.send_message(chat_id, 'Forward a complete replacement first.')
                return
            try:
                changed = store.replace_post(
                    replacement['post_id'], {**content, 'source': source})
            except ValueError as exc:
                await context.bot.send_message(chat_id, str(exc))
                return
            _cancel_replacement_pending(replacement)
            context.user_data.pop('replacement', None)
            await context.bot.send_message(chat_id, 'Post replaced; tracked source saved. Posting time unchanged.' if changed else 'Post is no longer pending; nothing changed.')
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if action == "draft":
        draft = context.user_data.get("draft")
        if not draft or str(draft["key"]) != parts[1]:
            await context.bot.send_message(chat_id, "This draft is no longer active.")
            return
        operation = parts[2]
        if operation == "cancel":
            _clear(context)
            await context.bot.send_message(chat_id, "Draft cancelled.", reply_markup=MENU)
            return
        if "end" not in draft:
            await context.bot.send_message(chat_id, "Enter both window timestamps first.")
            return
        if draft["pending"]:
            await context.bot.send_message(chat_id, "An album is still arriving. Wait a moment before using draft actions.")
            return
        if operation == "undo":
            if draft["posts"]:
                draft["posts"].pop()
            await _summary(context, chat_id)
            return
        if not draft["posts"]:
            await context.bot.send_message(chat_id, "Add at least one post first.")
            return
        try:
            times = storage.schedule_times(draft["start"], draft["end"], len(draft["posts"]))
        except ValueError:
            await context.bot.send_message(chat_id, "Posts need at least one minute between them. Undo posts or create a wider window.")
            return
        if operation == "preview":
            await _show_posts(context, chat_id, draft["posts"], times)
        elif operation == "commit":
            failure = await _permission(context)
            if failure:
                await context.bot.send_message(chat_id, failure)
                return
            # Recheck after network permission checks, immediately before storage.
            if draft["start"] <= datetime.now(UTC):
                await context.bot.send_message(chat_id, "Start is no longer in the future. Cancel and create a new window.")
                return
            try:
                sid = store.create_schedule(context.application.bot_data["chat_id"], draft["start"], draft["end"], draft["posts"])
            except ValueError as exc:
                await context.bot.send_message(chat_id, str(exc))
                return
            _clear(context)
            await context.bot.send_message(chat_id, f"Schedule #{sid} committed.", reply_markup=MENU)
        return
    if action == "noop":
        await query.edit_message_reply_markup(reply_markup=None)
        return
    identifier = int(parts[1])
    if action == "details":
        posts = store.get_posts(identifier)
        if not posts:
            await context.bot.send_message(chat_id, "Schedule is empty or no longer exists.")
        await _show_posts(context, chat_id, posts)
    elif action == "delete":
        await context.bot.send_message(
            chat_id,
            f"Delete schedule #{identifier}? Unsent posts and uncertain posts will be discarded. In-flight sends cannot be stopped and their rows will be retained.",
            reply_markup=_keyboard([[("Delete schedule", f"delete_yes:{identifier}"), ("Keep", "noop")]]),
        )
    elif action == "delete_yes":
        retained = store.delete_schedule(identifier)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id, "Schedule deleted. In-flight sending rows were retained." if retained else "Schedule deleted (or already absent).")
    elif action in ("dismiss", "retry", "retry_yes"):
        # Old attention buttons must not resolve a post now queued or sending.
        post = next((post for post in store.attention() if post["id"] == identifier), None)
        if post is None:
            await context.bot.send_message(chat_id, "This post no longer needs attention.")
            return
        if action == "retry":
            await context.bot.send_message(
                chat_id, f"Retry post #{identifier}? It may already have been delivered. Retrying can create a duplicate.",
                reply_markup=_keyboard([[("Retry anyway", f"retry_yes:{identifier}"), ("Keep unresolved", "noop")]]),
            )
        else:
            store.resolve(identifier, retry=action == "retry_yes")
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id, "Post queued for retry." if action == "retry_yes" else "Post discarded; it will not be retried.")


def register_handlers(application: Application, owner_id: int) -> None:
    """Register owner-guarded UI handlers on a sequential-update Application.

    Set bot_data['store'] and bot_data['chat_id'] before polling. The parent can
    configure its own builder post_init/post_shutdown hooks to manage a worker.
    Do not enable concurrent_updates: draft operations rely on sequential updates.
    """
    owner_id = int(owner_id)
    application.bot_data["owner_id"] = owner_id

    def guarded(handler):
        @wraps(handler)
        async def wrapper(update, context):
            if (not update.effective_user or update.effective_user.id != owner_id
                    or not update.effective_chat or update.effective_chat.type != "private"):
                if update.callback_query:
                    await update.callback_query.answer("Owner only.", show_alert=True)
                return
            try:
                await handler(update, context)
            except Exception:
                logger.exception("Scheduling UI operation failed")
                if update.effective_chat:
                    await context.bot.send_message(
                        update.effective_chat.id,
                        "That operation failed. Check the bot logs and destination permissions. "
                        "Refresh schedules or Needs attention before retrying a state-changing action.",
                    )
        return wrapper

    application.add_handler(CallbackQueryHandler(guarded(_callback)))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE, guarded(_message)))
    from pool import ingest
    application.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST, ingest))


def build_application(token: str, owner_id: int, chat_id: Any, store: Any) -> Application:
    """Optional convenience factory. No environment reads and no worker startup."""
    from rate_limiter import TelegramRateLimiter
    application = (Application.builder().token(token).concurrent_updates(False)
                   .rate_limiter(TelegramRateLimiter()).build())
    application.bot_data.update(store=store, chat_id=chat_id)
    from error_reporting import application_error
    application.add_error_handler(application_error)
    register_handlers(application, owner_id)
    return application
