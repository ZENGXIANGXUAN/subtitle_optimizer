import asyncio
import os
import sys
import tempfile
import types
import unittest


class _FormData:
    def __init__(self):
        self.fields = []

    def add_field(self, *args, **kwargs):
        self.fields.append((args, kwargs))


class _ClientTimeout:
    def __init__(self, total=None):
        self.total = total


class _ClientError(Exception):
    pass


class _Response:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self.payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, *args, **kwargs):
        return self.payload

    async def text(self):
        return self._text


class _PostContext:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, _DelayedOutcome):
            await asyncio.sleep(self.outcome.delay)
            self.outcome = self.outcome.outcome
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    outcomes = []
    attempts = 0
    request_timeouts = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        type(self).attempts += 1
        timeout = kwargs.get("timeout")
        type(self).request_timeouts.append(getattr(timeout, "total", None))
        return _PostContext(type(self).outcomes.pop(0))


class _DelayedOutcome:
    def __init__(self, delay, outcome):
        self.delay = delay
        self.outcome = outcome


AIOHTTP_STUB = types.SimpleNamespace(
    FormData=_FormData,
    ClientTimeout=_ClientTimeout,
    ClientSession=_Session,
    ClientError=_ClientError,
)
sys.modules["aiohttp"] = AIOHTTP_STUB

import core.client as client_module

MistralClient = client_module.MistralClient


class TranscriptionRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        client_module.aiohttp = AIOHTTP_STUB
        _Session.outcomes = []
        _Session.attempts = 0
        _Session.request_timeouts = []
        fd, self.audio_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.audio_path)
        except OSError:
            pass

    async def test_transcription_retries_sequentially_after_retryable_failure(self):
        _Session.outcomes = [
            asyncio.TimeoutError("network timeout"),
            _DelayedOutcome(0.01, _Response(200, {"segments": [{"text": "ok"}], "text": "ok"})),
            _DelayedOutcome(0.05, _Response(200, {"segments": [], "text": ""})),
        ]
        logs = []

        client = MistralClient("key", transcription_model="voxtral")
        result = await client.transcribe_audio(
            self.audio_path,
            retry_delay_seconds=0,
            on_log=logs.append,
        )

        self.assertEqual("ok", result["text"])
        self.assertEqual(2, _Session.attempts)
        self.assertTrue(any("Starting transcription API request sequence" in line for line in logs))
        self.assertTrue(any("Transcription API request 1/3 failed" in line for line in logs))
        self.assertTrue(any("Transcription API request 2/3 returned usable transcript" in line for line in logs))
        self.assertFalse(any("request 3/3 starting" in line for line in logs))

    async def test_empty_result_is_success_and_stops_sequence(self):
        _Session.outcomes = [
            _DelayedOutcome(0.01, _Response(200, {"segments": [], "text": ""})),
            _DelayedOutcome(0.05, _Response(200, {"segments": [{"text": "late"}], "text": "late"})),
            _DelayedOutcome(0.05, _Response(200, {"segments": [{"text": "late"}], "text": "late"})),
        ]
        logs = []

        client = MistralClient("key", transcription_model="voxtral")
        result = await client.transcribe_audio(
            self.audio_path,
            retry_delay_seconds=0,
            on_log=logs.append,
        )

        self.assertEqual("", result["text"])
        self.assertEqual(1, _Session.attempts)
        self.assertTrue(any("returned empty transcript" in line for line in logs))
        self.assertFalse(any("request 2/3 starting" in line for line in logs))

    async def test_client_error_status_fails_without_more_requests(self):
        _Session.outcomes = [
            _DelayedOutcome(0.01, _Response(400, text="bad request")),
            _DelayedOutcome(0.05, _Response(200, {"segments": [{"text": "late"}], "text": "late"})),
            _DelayedOutcome(0.05, _Response(200, {"segments": [{"text": "late"}], "text": "late"})),
        ]

        client = MistralClient("key", transcription_model="voxtral")
        with self.assertRaisesRegex(RuntimeError, "API错误 400"):
            await client.transcribe_audio(self.audio_path, retry_delay_seconds=0)

        self.assertEqual(1, _Session.attempts)

    async def test_chat_error_expands_proxy_details(self):
        _Session.outcomes = [
            _Response(
                502,
                text=(
                    '{"error":"All Retries Failed","details":[{'
                    '"node":"Mistral-Pool",'
                    '"attempt":1,'
                    '"max_retries":1,'
                    '"endpoint":"chat/completions",'
                    '"type":"PoolTimeout",'
                    '"message":"connection pool exhausted",'
                    '"hint":"Local proxy HTTP connection pool was exhausted",'
                    '"body":"upstream body text"'
                    '}]}'
                ),
            )
        ]

        client = MistralClient("key", model="mistral-medium")
        session = _Session()
        with self.assertRaises(RuntimeError) as ctx:
            await client.chat([{"role": "user", "content": "hi"}], session)

        msg = str(ctx.exception)
        self.assertIn("All Retries Failed", msg)
        self.assertIn("node=Mistral-Pool", msg)
        self.assertIn("type=PoolTimeout", msg)
        self.assertIn("Local proxy HTTP connection pool was exhausted", msg)
        self.assertIn("upstream body text", msg)

    async def test_local_proxy_chat_uses_one_request_and_175_second_budget(self):
        for base_url in (
            "http://127.0.0.1:8000/v1",
            "http://localhost:8000/v1",
            "http://[::1]:8000/v1",
        ):
            with self.subTest(base_url=base_url):
                _Session.attempts = 0
                _Session.request_timeouts = []
                _Session.outcomes = [
                    asyncio.TimeoutError("proxy total budget exhausted"),
                    _Response(
                        200,
                        {"choices": [{"message": {"content": "late duplicate"}}]},
                    ),
                ]
                client = MistralClient(
                    "key",
                    model="mistral-medium",
                    base_url=base_url,
                )

                with self.assertRaisesRegex(
                    RuntimeError, "proxy total budget exhausted"
                ):
                    await client.chat(
                        [{"role": "user", "content": "hi"}],
                        _Session(),
                        base_retry_delay_seconds=0,
                    )

                self.assertEqual(1, _Session.attempts)
                self.assertEqual([175.0], _Session.request_timeouts)

    async def test_remote_chat_keeps_client_retry_policy(self):
        _Session.outcomes = [
            asyncio.TimeoutError("temporary upstream failure"),
            _Response(
                200,
                {"choices": [{"message": {"content": "recovered"}}]},
            ),
        ]
        client = MistralClient("key", model="mistral-medium")

        result = await client.chat(
            [{"role": "user", "content": "hi"}],
            _Session(),
            base_retry_delay_seconds=0,
        )

        self.assertEqual("recovered", result)
        self.assertEqual(2, _Session.attempts)
        self.assertEqual([180.0, 180.0], _Session.request_timeouts)

    async def test_chat_cancellation_is_not_retried_or_wrapped(self):
        _Session.outcomes = [asyncio.CancelledError()]
        client = MistralClient(
            "key",
            model="mistral-medium",
            base_url="http://127.0.0.1:8000/v1",
        )

        with self.assertRaises(asyncio.CancelledError):
            await client.chat(
                [{"role": "user", "content": "hi"}],
                _Session(),
            )

        self.assertEqual(1, _Session.attempts)


if __name__ == "__main__":
    unittest.main()
