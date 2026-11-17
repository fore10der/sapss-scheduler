"""Safe application-error diagnostics; no requests, retries, or owner messages."""
import logging
import ssl
import time

from telegram.error import BadRequest, NetworkError, TimedOut

log = logging.getLogger(__name__)
NETWORK_LOG_INTERVAL = 60


def network_reason(error):
    """Classify wrapped transport errors without exposing their raw messages."""
    seen = set()
    pending = [error]
    certificate = weak_key = timeout = False
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        text = str(current).lower()
        weak_key |= 'ee certificate key too weak' in text or 'certificate key too weak' in text
        certificate |= (isinstance(current, ssl.SSLCertVerificationError)
                        or 'certificate_verify_failed' in text
                        or 'certificate verify failed' in text)
        timeout |= isinstance(current, (TimedOut, TimeoutError))
        pending.extend(exc for exc in (current.__cause__, current.__context__) if exc is not None)
    if weak_key:
        return 'certificate key too weak'
    if certificate:
        return 'certificate verification failed'
    return 'timeout' if timeout else 'connection failure'


async def application_error(update, context):
    error = context.error
    if isinstance(error, NetworkError) and not isinstance(error, BadRequest):
        reason = network_reason(error)
        error_type = type(error).__name__
        state = context.application.bot_data.setdefault('network_error_logs', {})
        key = (error_type, reason)
        now = time.monotonic()
        previous = state.get(key)
        if previous is not None and now - previous['last'] < NETWORK_LOG_INTERVAL:
            previous['suppressed'] += 1
            return
        suppressed = previous['suppressed'] if previous else 0
        state[key] = {'last': now, 'suppressed': 0}
        operation = 'polling' if update is None else 'request'
        if reason in ('certificate key too weak', 'certificate verification failed'):
            advice = ('Check proxy/VPN HTTPS interception and certificate configuration; '
                      'keep TLS verification enabled.')
            message = f'Telegram {operation} TLS verification failed ({reason}). {advice}'
        elif reason == 'timeout':
            message = f'Telegram {operation} timed out. Check network connectivity.'
        else:
            message = f'Telegram {operation} connection failed. Check network/proxy connectivity.'
        action = ('Polling will retry automatically.' if update is None else
                  'Check the operation state before retrying; it may have reached Telegram.')
        log.warning('%s %s error_type=%s suppressed=%s', message, action, error_type, suppressed)
        return

    # Keep stack frames for debugging, but not raw exception messages/chains,
    # which may contain Telegram URLs, bot tokens, or private message content.
    safe_error = RuntimeError(f'{type(error).__name__}: exception details omitted for privacy')
    log.error('Unexpected Telegram application error error_type=%s', type(error).__name__,
              exc_info=(RuntimeError, safe_error, error.__traceback__))
