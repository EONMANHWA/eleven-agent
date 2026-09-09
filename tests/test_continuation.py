import asyncio
from collections import Counter
from unittest.mock import AsyncMock

import pytest
from app import Session
from eleven_http import Row, ProviderError, result_chunks, retry_after_seconds
from test_bot import FakeBot, message
from test_http import prepared, creations, KEY


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status', [('rate_limited', 400), ('rate_limited', 429), ('service_unavailable', 503)])
async def test_precreation_transient_is_retryable(kind, status):
    client, calls, row = prepared(fail=('login', ProviderError(status, kind, 85)))
    await client.account(row, 'synthetic', asyncio.Event())
    assert row.retryable and not row.creation_attempted and not creations(calls)
    assert row.failure_stage == 'signing_in' and row.retry_after == 85 and row.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ProviderError(503, 'service_unavailable'), ProviderError(429, 'rate_limited'), asyncio.TimeoutError()])
async def test_creation_never_repeated_even_if_client_called_again(error):
    client, calls, row = prepared(fail=('create', error))
    await client.account(row, 'synthetic', asyncio.Event())
    assert not row.retryable and row.creation_attempted
    count = len(calls)
    await client.account(row, 'synthetic', asyncio.Event())
    assert len(calls) == count and len(creations(calls)) == 1


@pytest.mark.asyncio
async def test_rate_limit_after_creation_does_not_retry_mutation():
    client, calls, row = prepared(fail=('list', ProviderError(429, 'rate_limited', 121)))
    await client.account(row, 'synthetic', asyncio.Event())
    assert row.api_key == KEY and row.throttled and row.retry_after == 121
    assert row.status == 'created_settings_unverified' and not row.retryable


@pytest.mark.asyncio
async def test_known_10_account_failure_pattern_can_recover_transients_without_pauses():
    bot = FakeBot()
    bot.automatic_wait = AsyncMock(return_value=True)
    rows = [Row(f'account{i}@example.invalid', str(i)) for i in range(10)]
    bot.session = Session(mode='running', password='synthetic', rows=rows)
    calls = Counter()
    creations_seen = Counter()
    async def account(row, password, stop):
        calls[row.name] += 1; row.attempts += 1
        row.retryable = row.throttled = False
        if row.name == '0':
            row.status = 'login_rejected'
        elif row.name in ('1', '7', '8', '9') and row.attempts == 1:
            row.status = 'service_unavailable' if row.name == '1' else 'rate_limited'
            row.retryable = True
            row.throttled = row.name != '1'
        else:
            row.status = 'created_verified'; row.api_key = KEY
            row.creation_attempted = True
            creations_seen[row.name] += 1
    bot.client.account = AsyncMock(side_effect=account)
    await bot.run()
    assert bot.session.position == 10 and bot.session.mode == 'done' and not bot.session.password
    assert sum(bool(r.api_key) for r in rows) == 9
    assert calls['0'] == 1 and calls['1'] == 2 and calls['7'] == 2
    assert all(count == 1 for count in creations_seen.values())
    assert any(c.args[0] >= 60 for c in bot.automatic_wait.await_args_list)
    assert not any(method == 'sendDocument' for method, _, _ in bot.sent)


@pytest.mark.asyncio
async def test_persistent_rate_limits_get_one_retry_then_automatic_next_account():
    bot = FakeBot()
    bot.automatic_wait = AsyncMock(return_value=True)
    rows = [Row('a@example.invalid', 'a'), Row('b@example.invalid', 'b')]
    bot.session = Session(mode='running', password='synthetic', rows=rows)
    async def account(row, password, stop):
        row.attempts += 1; row.status = 'rate_limited'; row.retryable = row.throttled = True
        row.retry_after = 300
    bot.client.account = AsyncMock(side_effect=account)
    await bot.run()
    assert [r.attempts for r in rows] == [2, 2]
    assert bot.session.position == 2 and bot.session.mode == 'done'
    assert all(call.args[0] >= 300 for call in bot.automatic_wait.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['verification_required', 'creation_unknown', 'protocol_changed', 'workspace_policy_blocked', 'login_rejected'])
async def test_account_failure_does_not_block_next_email(status):
    bot = FakeBot(); bot.automatic_wait = AsyncMock(return_value=True)
    rows = [Row('a@example.invalid', 'a'), Row('b@example.invalid', 'b')]
    bot.session = Session(mode='running', password='synthetic', rows=rows)
    async def account(row, password, stop):
        row.attempts += 1; row.status = status
        if status == 'creation_unknown':row.creation_attempted = True
    bot.client.account = AsyncMock(side_effect=account)
    await bot.run()
    assert bot.session.position == 2 and bot.client.account.await_count == 2
    assert bot.session.mode == 'done'


@pytest.mark.asyncio
async def test_cancel_interrupts_automatic_provider_wait():
    bot = FakeBot()
    bot.session = Session(mode='running', password='synthetic', rows=[Row('a@example.invalid', 'a'), Row('b@example.invalid', 'b')])
    waiting = asyncio.Event()
    async def account(row, password, stop):
        row.attempts += 1; row.status = 'rate_limited'; row.retryable = row.throttled = True
    async def progress(text):
        if text.startswith('Automatic wait:'):waiting.set()
    bot.client.account = AsyncMock(side_effect=account)
    bot.progress = progress
    bot.worker = asyncio.create_task(bot.run())
    await asyncio.wait_for(waiting.wait(), timeout=1)
    await bot.handle(message(55, '/cancel'))
    await asyncio.wait_for(bot.worker, timeout=1)
    assert bot.client.account.await_count == 1 and bot.session.password == ''
    assert bot.session.rows[1].status == 'cancelled'


@pytest.mark.asyncio
async def test_results_default_is_text_and_csv_is_opt_in():
    bot = FakeBot()
    bot.session.rows = [Row('a@example.invalid', 'test', api_key=KEY)]
    await bot.handle(message(1, '/results'))
    assert any(method == 'sendMessage' and KEY in data['text'] for method, data, _ in bot.sent)
    assert not any(method == 'sendDocument' for method, _, _ in bot.sent)
    await bot.handle(message(2, '/csv'))
    assert any(method == 'sendDocument' for method, _, _ in bot.sent)


@pytest.mark.asyncio
async def test_resume_command_does_not_restart_completed_batch():
    bot = FakeBot(); bot.session.mode = 'done'
    await bot.handle(message(1, '/resume'))
    assert bot.worker is None and bot.client.account.await_count == 0


def test_text_results_chunking_and_no_password_fields():
    rows = [Row('x' * 64 + '@example.invalid', f'name{i}', api_key='sk_' + 'x' * 250) for i in range(100)]
    chunks = result_chunks(rows)
    assert len(chunks) > 1
    assert all(len(text) <= 3500 for text, _ in chunks)
    assert sum(len(group) for _, group in chunks) == 100
    rows[0].text_delivered = True
    assert sum(len(group) for _, group in result_chunks(rows, True)) == 99
    assert all('password' not in text for text, _ in chunks)


@pytest.mark.parametrize('value,expected', [(None,0), ('120',120), ('-4',0), ('nonsense',0), ('nan',0), ('inf',0)])
def test_retry_after_numeric_or_invalid(value, expected):
    assert retry_after_seconds(value) == expected


def test_retry_after_http_date(monkeypatch):
    monkeypatch.setattr('eleven_http.time.time', lambda: 1000000000)
    assert retry_after_seconds('Sun, 09 Sep 2001 01:48:40 GMT') == 120


@pytest.mark.asyncio
async def test_transport_preserves_provider_retry_after():
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from eleven_http import ElevenClient
    async def throttled(request):
        return web.json_response({'error': {'message': 'TOO_MANY_ATTEMPTS_TRY_LATER'}}, status=400, headers={'Retry-After': '181'})
    app = web.Application(); app.router.add_post('/test', throttled)
    async with TestServer(app) as server, aiohttp.ClientSession() as http:
        client = ElevenClient(http, 'synthetic-public-config')
        with pytest.raises(ProviderError) as error:
            await client.request(str(server.make_url('/test')), {'synthetic': True})
        assert error.value.kind == 'rate_limited' and error.value.retry_after == 181


@pytest.mark.asyncio
async def test_telegram_explicit_flood_wait_is_respected(monkeypatch):
    from app import Bot, Settings
    sleeps = []
    async def sleep(seconds):sleeps.append(seconds)
    monkeypatch.setattr('app.asyncio.sleep', sleep)
    class Response:
        def __init__(self, status, body):self.status, self.body = status, body
        async def __aenter__(self):return self
        async def __aexit__(self, *args):return False
        async def json(self):return self.body
    class HTTP:
        calls = 0
        def post(self, *args, **kwargs):
            self.calls += 1
            return Response(429, {'ok': False, 'parameters': {'retry_after': 61}}) if self.calls == 1 else Response(200, {'ok': True, 'result': {'message_id': 1}})
    http = HTTP(); bot = Bot(Settings('synthetic', 's'*40, 123, 'public'), http)
    assert await bot.say('Synthetic text') == {'message_id': 1}
    assert http.calls == 2 and 61 in sleeps
