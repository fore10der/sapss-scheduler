import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from telegram import Bot
from telegram.ext._utils.webhookhandler import WebhookAppClass
from tornado.testing import AsyncHTTPTestCase

from config import (
    DEFAULT_WEBHOOK_HOST,
    DEFAULT_WEBHOOK_PATH,
    DEFAULT_WEBHOOK_PORT,
    WebhookConfig,
    WebhookConfigError,
    webhook_config,
)
from main import ALLOWED_UPDATES, open_database_lock, run_application


def webhook_env(**overrides):
    values = {
        'WEBHOOK_URL': 'https://bot.example.com/webhook',
        'WEBHOOK_SECRET': 'abc_123-XYZ',
    }
    values.update(overrides)
    return values


class WebhookConfigTests(unittest.TestCase):
    def test_empty_or_unset_url_selects_polling(self):
        self.assertIsNone(webhook_config({}))
        self.assertIsNone(webhook_config({'WEBHOOK_URL': ''}))
        self.assertIsNone(webhook_config({'WEBHOOK_URL': '   '}))

    def test_valid_url_selects_webhook_with_listener_defaults(self):
        self.assertEqual(
            webhook_config(webhook_env()),
            WebhookConfig(url='https://bot.example.com/webhook', secret='abc_123-XYZ',
                          host=DEFAULT_WEBHOOK_HOST, port=DEFAULT_WEBHOOK_PORT,
                          path=DEFAULT_WEBHOOK_PATH))

    def test_custom_listener_does_not_change_public_url(self):
        config = webhook_config(webhook_env(
            WEBHOOK_URL='https://bot.example.com:8443/hook',
            WEBHOOK_SECRET='s',
            WEBHOOK_HOST='127.0.0.1',
            WEBHOOK_PORT='9000',
            WEBHOOK_PATH='/hook/'))
        self.assertEqual((config.host, config.port, config.path), ('127.0.0.1', 9000, '/hook'))
        self.assertEqual(config.url, 'https://bot.example.com:8443/hook')

    def test_supported_telegram_public_ports(self):
        for port in (443, 80, 88, 8443):
            with self.subTest(port=port):
                config = webhook_config(webhook_env(
                    WEBHOOK_URL=f'https://bot.example.com:{port}/webhook'))
                self.assertEqual(config.url, f'https://bot.example.com:{port}/webhook')

    def test_invalid_configuration_is_rejected_with_actionable_errors(self):
        cases = {
            'http://bot.example.com/webhook': 'https',
            'https://bot.example.com:8080/webhook': '443, 80, 88, or 8443',
            'https://bot.example.com/wrong': 'must match WEBHOOK_PATH',
            'https://bot.example.com/webhook?token=x': 'query or fragment',
            'https://bot.example.com/webhook#fragment': 'query or fragment',
            'https://user:pass@bot.example.com/webhook': 'credentials',
        }
        for url, message in cases.items():
            with self.subTest(url=url):
                with self.assertRaisesRegex(WebhookConfigError, message):
                    webhook_config(webhook_env(WEBHOOK_URL=url))

    def test_secret_is_required_and_validated(self):
        with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_SECRET is required'):
            webhook_config(webhook_env(WEBHOOK_SECRET=''))
        with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_SECRET is required'):
            webhook_config({k: v for k, v in webhook_env().items() if k != 'WEBHOOK_SECRET'})
        for secret in ('bad space', 'bad!', 'a' * 257):
            with self.subTest(secret=secret):
                with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_SECRET must be 1-256'):
                    webhook_config(webhook_env(WEBHOOK_SECRET=secret))

    def test_listener_validation(self):
        with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_PORT must be an integer'):
            webhook_config(webhook_env(WEBHOOK_PORT='https'))
        with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_PORT must be between'):
            webhook_config(webhook_env(WEBHOOK_PORT='70000'))
        with self.assertRaisesRegex(WebhookConfigError, 'WEBHOOK_HOST must not be empty'):
            webhook_config(webhook_env(WEBHOOK_HOST=' '))
        with self.assertRaisesRegex(WebhookConfigError, "WEBHOOK_PATH must start with '/'"):
            webhook_config(webhook_env(WEBHOOK_PATH='hook'))

    def test_polling_needs_no_webhook_or_secret(self):
        self.assertIsNone(webhook_config({'WEBHOOK_SECRET': ''}))
        self.assertIsNone(webhook_config({'WEBHOOK_PATH': '', 'WEBHOOK_PORT': 'nope'}))


class RecordingApplication:
    def __init__(self):
        self.calls = []

    def run_polling(self, **kwargs):
        self.calls.append(('polling', kwargs))

    def run_webhook(self, **kwargs):
        self.calls.append(('webhook', kwargs))


class ModeDispatchTests(unittest.TestCase):
    def test_empty_url_runs_polling_without_dropping_updates(self):
        app = RecordingApplication()
        run_application(app, None)
        self.assertEqual(app.calls, [('polling', {
            'drop_pending_updates': False,
            'allowed_updates': list(ALLOWED_UPDATES),
        })])

    def test_nonempty_url_runs_webhook_with_listener_and_secret(self):
        config = webhook_config(webhook_env(
            WEBHOOK_URL='https://bot.example.com:8443/hook',
            WEBHOOK_SECRET='s3cret',
            WEBHOOK_HOST='0.0.0.0',
            WEBHOOK_PORT='8443',
            WEBHOOK_PATH='/hook'))
        app = RecordingApplication()
        run_application(app, config)
        self.assertEqual(app.calls, [('webhook', {
            'listen': '0.0.0.0',
            'port': 8443,
            'url_path': '/hook',
            'webhook_url': 'https://bot.example.com:8443/hook',
            'secret_token': 's3cret',
            'drop_pending_updates': False,
            'allowed_updates': list(ALLOWED_UPDATES),
        })])


class WebhookSecretTests(AsyncHTTPTestCase):
    def get_app(self):
        self.bot = Bot('123:TEST_ONLY')
        self.update_queue = asyncio.Queue()
        return WebhookAppClass('/webhook', self.bot, self.update_queue, secret_token='s3cret')

    def post(self, secret=None):
        headers = {'Content-Type': 'application/json'}
        if secret is not None:
            headers['X-Telegram-Bot-Api-Secret-Token'] = secret
        return self.fetch('/webhook', method='POST', headers=headers,
                          body=json.dumps({'update_id': 1}))

    def test_correct_secret_is_accepted(self):
        self.assertEqual(self.post('s3cret').code, 200)

    def test_missing_secret_is_rejected(self):
        self.assertEqual(self.post().code, 403)

    def test_incorrect_secret_is_rejected(self):
        self.assertEqual(self.post('wrong').code, 403)


class DatabaseLockTests(unittest.TestCase):
    def test_second_process_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'scheduler.sqlite3'
            first = open_database_lock(path)
            try:
                with self.assertRaisesRegex(SystemExit, 'Another scheduler is already using'):
                    open_database_lock(path)
            finally:
                first.close()


if __name__ == '__main__':
    unittest.main()
