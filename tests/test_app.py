import time
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import app as module


@pytest.fixture
async def client(aiohttp_client, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://bot.example')
    monkeypatch.setenv('TELEGRAM_WEBHOOK_SECRET', 's' * 48)
    monkeypatch.setenv('ADMIN_CHAT_ID', '123')
    app = module.create_app()
    app.cleanup_ctx.clear()  # No Telegram, ElevenLabs, solver, or Supabase requests.
    return await aiohttp_client(app)


def auth_session(client):
    s = client.app['state']
    s.session = module.digest('test-session')
    s.expires = time.monotonic() + 1200
    s.last_action = time.monotonic()
    return {'Origin': 'https://bot.example', 'Authorization': 'Bearer test-session'}


@pytest.mark.asyncio
async def test_security_headers_and_health(client):
    r = await client.get('/health')
    assert r.status == 200
    assert await r.json() == {'ok': True}
    assert r.headers['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']


@pytest.mark.asyncio
async def test_reject_forged_webhook(client):
    r = await client.post('/telegram/webhook', json={})
    assert r.status == 403


@pytest.mark.asyncio
async def test_owner_allowlist_and_id(client, monkeypatch):
    telegram = AsyncMock()
    monkeypatch.setattr(module, 'telegram', telegram)
    headers = {'X-Telegram-Bot-Api-Secret-Token': 's' * 48}
    def update(uid, chat, command, kind='private'):
        return {'update_id': uid, 'message': {'chat': {'id': chat, 'type': kind}, 'text': command, 'from': {'is_bot': False}}}
    await client.post('/telegram/webhook', headers=headers, json=update(1, 999, '/login'))
    await client.post('/telegram/webhook', headers=headers, json=update(2, 123, '/login', 'group'))
    assert telegram.await_count == 0
    await client.post('/telegram/webhook', headers=headers, json=update(3, 999, '/id'))
    assert '999' in telegram.call_args.args[2]['text']
    await client.post('/telegram/webhook', headers=headers, json=update(4, 123, '/login'))
    assert 'https://bot.example/#' in telegram.call_args.args[2]['text']
    assert client.app['state'].ticket
    count = telegram.await_count
    await client.post('/telegram/webhook', headers=headers, json=update(4, 123, '/login'))
    assert telegram.await_count == count


@pytest.mark.asyncio
async def test_origin_and_auth(client):
    assert (await client.post('/api/action', json={'op': 'close'})).status == 403
    assert (await client.post('/api/action', headers={'Origin': 'https://evil.example'}, json={'op': 'close'})).status == 403
    assert (await client.post('/api/action', headers={'Origin': 'https://bot.example'}, json={'op': 'close'})).status == 401
    assert (await client.get('/api/screenshot')).status == 401
    assert (await client.post('/api/claim', headers={'Origin': 'https://bot.example'}, json={'ticket': 'wrong'})).status == 401


@pytest.mark.asyncio
async def test_expiry_and_revocation(client):
    headers = auth_session(client)
    s = client.app['state']
    s.last_action -= 601
    assert (await client.post('/api/action', headers=headers, json={'op': 'close'})).status == 401
    auth_session(client)
    s.expires -= 1201
    assert not s.authorized('test-session')
    auth_session(client)
    first = await s.new_ticket()
    assert not s.authorized('test-session')
    assert s.ticket == module.digest(first)
    second = await s.new_ticket()
    assert first != second


@pytest.mark.asyncio
async def test_manual_fallback_and_input_validation(client, monkeypatch):
    monkeypatch.delenv('NOPECHA_API_KEY', raising=False)
    headers = auth_session(client)
    client.app['state'].page = AsyncMock()
    r = await client.post('/api/action', headers=headers, json={'op': 'solver', 'consent': True})
    assert r.status == 400
    assert 'Manual solving is free' in (await r.json())['error']
    r = await client.post('/api/action', headers=headers, json={'op': 'solver'})
    assert r.status == 400
    assert 'consent' in (await r.json())['error']
    r = await client.post('/api/action', headers=headers, json={'op': 'key', 'key': 'F12'})
    assert r.status == 400
    r = await client.post('/api/action', headers=headers, json={'op': 'click', 'x': 5000, 'y': 1})
    assert r.status == 400


@pytest.mark.asyncio
async def test_real_chromium_controls_without_external_accounts(client):
    headers = auth_session(client)
    s = client.app['state']
    async with async_playwright() as pw:
        s.browser = await pw.chromium.launch(args=['--no-sandbox'])
        s.context = await s.browser.new_context(viewport={'width': 1100, 'height': 780})
        s.page = await s.context.new_page()
        await s.page.set_content('<input id="field"><button onclick="document.body.dataset.clicked=1">Test</button>')
        await s.page.locator('input').focus()
        r = await client.post('/api/action', headers=headers, json={'op': 'type', 'text': 'dummy text'})
        assert r.status == 200
        assert await s.page.locator('input').input_value() == 'dummy text'
        r = await client.get('/api/screenshot', headers=headers)
        assert r.status == 200 and r.content_type == 'image/jpeg'
        assert len(await r.read()) > 1000
        r = await client.post('/api/action', headers=headers, json={'op': 'login', 'email': 'dummy@example.com', 'password': 'dummy'})
        assert r.status == 400  # Never fill credentials on a non-ElevenLabs origin.
        r = await client.post('/api/action', headers=headers, json={'op': 'close'})
        assert (await r.json())['closed'] is True
        assert s.browser is None and not s.session


@pytest.mark.asyncio
async def test_ticket_claim_once_with_mock_browser(client):
    s = client.app['state']
    page = AsyncMock()
    page.set_default_timeout = lambda _: None
    context = AsyncMock()
    context.new_page.return_value = page
    browser = AsyncMock()
    browser.new_context.return_value = context
    s.pw = AsyncMock()
    s.pw.chromium.launch.return_value = browser
    ticket = await s.new_ticket()
    headers = {'Origin': 'https://bot.example'}
    r = await client.post('/api/claim', headers=headers, json={'ticket': ticket})
    assert r.status == 200
    token = (await r.json())['token']
    assert s.authorized(token)
    r = await client.post('/api/claim', headers=headers, json={'ticket': ticket})
    assert r.status == 401
    await s.close_browser()
