"""Webhook mode selection and validation from environment variables.

An empty or unset ``WEBHOOK_URL`` selects long polling. A nonempty value selects
webhook mode and must be a complete, publicly reachable HTTPS endpoint.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_WEBHOOK_HOST = '0.0.0.0'
DEFAULT_WEBHOOK_PORT = 8443
DEFAULT_WEBHOOK_PATH = '/webhook'
SUPPORTED_WEBHOOK_PORTS = frozenset({443, 80, 88, 8443})
SECRET_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')


class WebhookConfigError(ValueError):
    """Invalid WEBHOOK_* environment configuration."""


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    secret: str
    host: str
    port: int
    path: str


def _path(value, variable):
    if not value.startswith('/'):
        raise WebhookConfigError(f"{variable} must start with '/'; got {value!r}.")
    if any(ch in value for ch in '?#') or any(ch.isspace() for ch in value):
        raise WebhookConfigError(
            f"{variable} must be a URL path without whitespace, '?', or '#': got {value!r}.")
    return value.rstrip('/') or '/'


def _port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise WebhookConfigError(
            f'WEBHOOK_PORT must be an integer between 1 and 65535; got {value!r}.') from None
    if not 1 <= port <= 65535:
        raise WebhookConfigError(
            f'WEBHOOK_PORT must be between 1 and 65535; got {value!r}.')
    return port


def webhook_config(environ):
    """Return WebhookConfig, or None when WEBHOOK_URL is empty/unset (polling)."""
    url = environ.get('WEBHOOK_URL', '').strip()
    if not url:
        return None

    try:
        parsed = urlsplit(url)
        public_port = parsed.port
    except ValueError as exc:
        raise WebhookConfigError(f'WEBHOOK_URL is not a valid URL: {exc}') from None
    if parsed.scheme.lower() != 'https':
        raise WebhookConfigError(
            'WEBHOOK_URL must use https://; Telegram requires an HTTPS webhook endpoint.')
    if not parsed.hostname:
        raise WebhookConfigError('WEBHOOK_URL must include a public hostname.')
    if parsed.username or parsed.password:
        raise WebhookConfigError('WEBHOOK_URL must not contain credentials.')
    if parsed.query or parsed.fragment:
        raise WebhookConfigError('WEBHOOK_URL must not contain a query or fragment.')
    if public_port is not None and public_port not in SUPPORTED_WEBHOOK_PORTS:
        raise WebhookConfigError(
            'WEBHOOK_URL port must be one of 443, 80, 88, or 8443 per Telegram limits; '
            f'got {public_port}.')

    secret = environ.get('WEBHOOK_SECRET', '')
    if not secret:
        raise WebhookConfigError('WEBHOOK_SECRET is required when WEBHOOK_URL is set.')
    if not SECRET_PATTERN.fullmatch(secret):
        raise WebhookConfigError(
            'WEBHOOK_SECRET must be 1-256 characters using only A-Z, a-z, 0-9, _, and -.')

    listener_path = _path(environ.get('WEBHOOK_PATH', DEFAULT_WEBHOOK_PATH), 'WEBHOOK_PATH')
    public_path = _path(parsed.path or '/', 'WEBHOOK_URL path')
    if public_path != listener_path:
        raise WebhookConfigError(
            f'WEBHOOK_URL path {public_path!r} must match WEBHOOK_PATH {listener_path!r}; '
            'Telegram sends updates to the public URL path.')

    host = environ.get('WEBHOOK_HOST', DEFAULT_WEBHOOK_HOST).strip()
    if not host:
        raise WebhookConfigError('WEBHOOK_HOST must not be empty.')
    port = _port(environ.get('WEBHOOK_PORT', DEFAULT_WEBHOOK_PORT))

    return WebhookConfig(url=url, secret=secret, host=host, port=port, path=listener_path)
