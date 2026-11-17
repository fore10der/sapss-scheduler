import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram.error import NetworkError, TimedOut

from error_reporting import application_error


class ErrorReportingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={}), error=None)

    async def emit(self, error, update=None):
        self.context.error = error
        await application_error(update, self.context)

    async def test_repeated_network_errors_are_throttled_and_counted(self):
        error = NetworkError('SECRET https://api.telegram.org/botSECRET/getUpdates')
        with patch('error_reporting.time.monotonic', side_effect=[0, 1, 59, 60, 61, 120]):
            with self.assertLogs('error_reporting', level='WARNING') as logs:
                for _ in range(6):
                    await self.emit(error)
        self.assertEqual(len(logs.records), 3)
        self.assertIn('suppressed=0', logs.output[0])
        self.assertIn('suppressed=2', logs.output[1])
        self.assertIn('suppressed=1', logs.output[2])
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        output = '\n'.join(logs.output)
        self.assertNotIn('SECRET', output)
        self.assertNotIn('api.telegram.org', output)
        self.assertIn('Polling will retry automatically', output)

    async def test_wrapped_weak_certificate_error_is_safe_and_actionable(self):
        import httpx
        certificate = ssl.SSLCertVerificationError(
            '[SSL: CERTIFICATE_VERIFY_FAILED] EE certificate key too weak SECRET')
        connection = httpx.ConnectError('SECRET connection URL')
        connection.__cause__ = certificate
        error = NetworkError('SECRET outer request')
        error.__cause__ = connection
        with self.assertLogs('error_reporting', level='WARNING') as logs:
            await self.emit(error)
        self.assertEqual(len(logs.records), 1)
        self.assertIn('TLS verification failed (certificate key too weak)', logs.output[0])
        self.assertIn('proxy/VPN HTTPS interception', logs.output[0])
        self.assertIn('keep TLS verification enabled', logs.output[0])
        self.assertNotIn('SECRET', logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)

    async def test_distinct_reason_and_error_class_log_immediately(self):
        class OtherNetworkError(NetworkError):
            pass
        with patch('error_reporting.time.monotonic', return_value=5):
            with self.assertLogs('error_reporting', level='WARNING') as logs:
                await self.emit(NetworkError('connect'))
                await self.emit(NetworkError('certificate verify failed SECRET'))
                await self.emit(TimedOut('SECRET timeout'))
                await self.emit(OtherNetworkError('connect'))
        self.assertEqual(len(logs.records), 4)
        self.assertIn('certificate verification failed', logs.output[1])
        self.assertIn('timed out', logs.output[2])
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    async def test_request_failure_does_not_promise_automatic_resend(self):
        with self.assertLogs('error_reporting', level='WARNING') as logs:
            await self.emit(NetworkError('SECRET'), update=object())
        self.assertIn('Telegram request connection failed', logs.output[0])
        self.assertIn('may have reached Telegram', logs.output[0])
        self.assertNotIn('retry automatically', logs.output[0])

    async def test_unexpected_errors_keep_frames_without_private_messages_or_chain(self):
        nested_message = 'SECRET nested token'
        private_message = 'SECRET private content'
        try:
            try:
                raise ValueError(nested_message)
            except ValueError as cause:
                raise RuntimeError(private_message) from cause
        except RuntimeError as error:
            with self.assertLogs('error_reporting', level='ERROR') as logs:
                await self.emit(error)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIn('Traceback', logs.output[0])
        self.assertIn('error_type=RuntimeError', logs.output[0])
        self.assertNotIn('SECRET', logs.output[0])
        self.assertNotIn('RuntimeError: SECRET private content', logs.output[0])
        self.assertNotIn('ValueError: SECRET nested token', logs.output[0])
        self.assertNotIn('direct cause', logs.output[0])
        self.assertIn('exception details omitted for privacy', logs.output[0])

    async def test_factory_registers_handler_and_process_error_uses_it(self):
        from bot import build_application
        app = build_application('123:TEST_ONLY', 42, 123, None)
        self.assertIn(application_error, app.error_handlers)
        with self.assertLogs(level='WARNING') as logs:
            await app.process_error(update=None, error=NetworkError('SECRET'))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].name, 'error_reporting')
        self.assertNotIn('No error handlers', '\n'.join(logs.output))
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    async def test_main_registers_handler_without_loading_credentials_or_polling(self):
        import main
        app = SimpleNamespace(bot_data={}, add_error_handler=Mock(), run_polling=Mock())
        with tempfile.TemporaryDirectory() as directory:
            env = {'BOT_TOKEN': '123:TEST_ONLY', 'OWNER_ID': '42', 'CHAT_ID': '123',
                   'DATABASE_PATH': str(Path(directory) / 'test.db')}
            with (patch.object(main, 'load_dotenv'), patch.dict(main.os.environ, env, clear=True),
                  patch.object(main, 'Application') as application,
                  patch.object(main, 'register_handlers'),
                  patch.object(main.asyncio, 'new_event_loop', return_value=None),
                  patch.object(main.asyncio, 'set_event_loop')):
                application.builder.return_value.token.return_value.concurrent_updates.return_value.post_init.return_value.post_stop.return_value.build.return_value = app
                main.main()
        app.add_error_handler.assert_called_once_with(application_error)
        app.run_polling.assert_called_once()
