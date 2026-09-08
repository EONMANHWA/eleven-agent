import asyncio
import csv
import io

import pytest
from eleven_http import ElevenClient, ProviderError, Row, csv_bytes, parse_emails

KEY = 'sk_' + 'x' * 40


def prepared(fail=None, override=None, permissions=None, leak=False, verified=True, wrong_email=False):
    client = ElevenClient(None, 'public-test-config')
    calls = []
    row = Row('owner@example.com', 'test-unique-key')
    async def request(url, body=None, token=None):
        calls.append((url, body, token))
        step = ('login' if 'signInWithPassword' in url else 'lookup' if 'lookup?' in url else
                'create' if 'create-api-key' in url else 'list' if '/api-keys' in url else 'workspace')
        if fail and step == fail[0]:
            if isinstance(fail[1], BaseException):
                raise fail[1]
            return fail[1]
        if step == 'login':
            return {'email': 'other@example.com' if wrong_email else row.email,
                    'idToken': 'id-token', 'refreshToken': 'discard-me'}
        if step == 'lookup':
            return {'users': [{'emailVerified': verified}]}
        if step == 'workspace':
            return {'third_party_disable_allowed_override': override}
        if step == 'create':
            return {'xi_api_key': KEY}
        return {'api_keys': [{'name': row.name, 'permissions': permissions, 'third_party_disable_allowed': leak}]}
    client.request = request
    return client, calls, row


def creations(calls):
    return [c for c in calls if '/create-api-key' in c[0]]


@pytest.mark.asyncio
async def test_success_exact_observed_personal_key_payload():
    client, calls, row = prepared()
    await client.account(row, 'synthetic-password', asyncio.Event())
    assert row.status == 'created_verified'
    assert row.api_key == KEY and row.full_access == 'yes' and row.leak_auto_disable == 'off'
    assert len(creations(calls)) == 1
    assert creations(calls)[0][1] == {'name': row.name, 'third_party_disable_allowed': False}
    assert all(c[2] == 'id-token' for c in calls[2:])
    assert sum('/v1/workspace' in c[0] for c in calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['mfa', 'email_unverified', 'wrong_identity', 'workspace_policy'])
async def test_no_key_when_account_needs_attention(case):
    kwargs = {'mfa': {'fail': ('login', {'mfaPendingCredential': 'private'})},
              'email_unverified': {'verified': False}, 'wrong_identity': {'wrong_email': True},
              'workspace_policy': {'override': True}}[case]
    client, calls, row = prepared(**kwargs)
    await client.account(row, 'test', asyncio.Event())
    assert not creations(calls) and not row.api_key
    assert row.status in ('verification_required', 'protocol_changed', 'workspace_policy_blocked')


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [asyncio.TimeoutError(), OSError(), ProviderError(503, 'service_unavailable')])
async def test_unknown_creation_is_never_retried(error):
    client, calls, row = prepared(fail=('create', error))
    await client.account(row, 'test', asyncio.Event())
    assert row.status == 'creation_unknown' and len(creations(calls)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{}, {'xi_api_key': None}, {'xi_api_key': 'not-a-valid-key'}])
async def test_unrecognized_creation_is_never_retried(body):
    client, calls, row = prepared(fail=('create', body))
    await client.account(row, 'test', asyncio.Event())
    assert row.status == 'creation_unknown' and len(creations(calls)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ProviderError(400, 'protocol_changed'), ProviderError(429, 'rate_limited')])
async def test_provider_rejection_no_fallback_mutation(error):
    client, calls, row = prepared(fail=('create', error))
    await client.account(row, 'test', asyncio.Event())
    assert row.status == error.kind and len(creations(calls)) == 1


@pytest.mark.asyncio
async def test_key_retained_when_verification_fails():
    client, calls, row = prepared(fail=('list', asyncio.TimeoutError()))
    await client.account(row, 'test', asyncio.Event())
    assert row.status == 'created_settings_unverified' and row.api_key == KEY
    assert len(creations(calls)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'permissions': ['text_to_speech']}, {'leak': True}])
async def test_incorrect_settings_are_not_claimed_verified(kwargs):
    client, calls, row = prepared(**kwargs)
    await client.account(row, 'test', asyncio.Event())
    assert row.status == 'created_settings_unverified' and row.api_key


@pytest.mark.asyncio
async def test_workspace_override_off_is_respected():
    client, calls, row = prepared(override=False, leak=True)
    await client.account(row, 'test', asyncio.Event())
    assert row.status == 'created_verified'


@pytest.mark.asyncio
async def test_cancellation_before_creation():
    client, calls, row = prepared()
    stop = asyncio.Event()
    base = client.request
    async def request(url, body=None, token=None):
        result = await base(url, body, token)
        if '/v1/workspace' in url:
            stop.set()
        return result
    client.request = request
    await client.account(row, 'test', stop)
    assert not creations(calls) and row.status == 'cancelled'


@pytest.mark.asyncio
async def test_cancel_during_creation_marks_unknown():
    client, calls, row = prepared(fail=('create', asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await client.account(row, 'test', asyncio.Event())
    assert row.status == 'creation_unknown'


@pytest.mark.parametrize('count', [1, 5, 100, 1000])
def test_batch_capacity(count):
    assert len(parse_emails('\n'.join(f'a{i}@example.com' for i in range(count)), count)) == count


def test_merge_deduplicate_and_limits():
    assert parse_emails('A@example.com,a@example.com; b@example.com') == ['A@example.com', 'b@example.com']
    with pytest.raises(ValueError):
        parse_emails('a@example.com\nb@example.com', 1)
    assert parse_emails('Heading\na@example.com') == ['a@example.com']


def test_csv_formula_safety_and_no_password_fields():
    row = Row('=SUM(1)@example.com', 'tg-test', api_key=KEY)
    content = csv_bytes([row]).decode('utf-8-sig')
    parsed = list(csv.reader(io.StringIO(content)))
    assert parsed[1][0].startswith("'=")
    assert 'password' not in parsed[0]
    assert parsed[1][1] == KEY
    assert 'owner@' not in repr(row) and KEY not in repr(row)


@pytest.mark.asyncio
async def test_http_transport_reads_chunked_json_and_does_not_follow_redirects():
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    visited = []
    async def chunked(request):
        visited.append(request.path)
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        await response.write(b'{"value":')
        await asyncio.sleep(0)
        await response.write(b'"complete"}')
        await response.write_eof()
        return response
    async def redirect(request):
        raise web.HTTPFound('/forbidden')
    async def forbidden(request):
        visited.append('/forbidden')
        return web.json_response({})
    app = web.Application()
    app.router.add_post('/chunked', chunked)
    app.router.add_post('/redirect', redirect)
    app.router.add_get('/forbidden', forbidden)
    async with TestServer(app) as server, aiohttp.ClientSession() as http:
        client = ElevenClient(http, 'public-config')
        assert await client.request(str(server.make_url('/chunked')), {'test': True}) == {'value': 'complete'}
        with pytest.raises(ProviderError):
            await client.request(str(server.make_url('/redirect')), {'test': True}, 'synthetic-token')
        assert '/forbidden' not in visited


@pytest.mark.parametrize('status,body,expected', [
    (400, {'error': {'message': 'INVALID_LOGIN_CREDENTIALS'}}, 'login_rejected'),
    (400, {'error': {'message': 'MISSING_RECAPTCHA_TOKEN'}}, 'verification_required'),
    (400, {'error': {'message': 'TOO_MANY_ATTEMPTS_TRY_LATER'}}, 'rate_limited'),
    (429, {}, 'rate_limited'), (503, {}, 'service_unavailable'),
    (403, {'detail': {'message': 'private provider text'}}, 'access_denied')])
def test_error_classification_never_returns_raw_provider_text(status, body, expected):
    from eleven_http import error_kind
    assert error_kind(status, body) == expected
