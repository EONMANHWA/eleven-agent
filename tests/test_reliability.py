import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app as module
from resource_guard import unnecessary_request, chromium_args


@pytest.fixture
async def client(aiohttp_client, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL','https://bot.example')
    a=module.create_app();a.cleanup_ctx.clear()
    return await aiohttp_client(a)


def session(client):
    s=client.app['state']; token=s.boot_id+'.test-token'
    s.session=module.digest(token);s.expires=time.monotonic()+1200;s.last_action=time.monotonic()
    return s,token,{'Origin':'https://bot.example','Authorization':'Bearer '+token}


@pytest.mark.asyncio
async def test_owner_new_link_preserves_account_and_claim_reattaches(client):
    s,token,headers=session(client)
    old_page=object();s.page=old_page
    s.open_browser=AsyncMock()
    ticket=await s.new_ticket()
    assert s.page is old_page and s.authorized(token)
    r=await client.post('/api/claim',headers={'Origin':'https://bot.example'},json={'ticket':ticket})
    data=await r.json()
    assert r.status==200 and data['resumed']
    assert s.page is old_page
    s.open_browser.assert_not_awaited()
    assert not s.authorized(token) and s.authorized(data['token'])
    cookie=r.cookies[module.SESSION_COOKIE]
    assert cookie['secure'] and cookie['httponly'] and cookie['samesite']=='Strict' and cookie['path']=='/'
    # Simulate a refreshed browser that has only its HttpOnly cookie, not the JS token.
    r=await client.get('/api/session',headers={'Cookie':module.SESSION_COOKIE+'='+data['token']})
    assert r.status==200 and (await r.json())['browser_open']


@pytest.mark.asyncio
async def test_restart_is_distinguished_from_timeout(client):
    s,token,headers=session(client)
    s.boot_id='different-boot';s.session=''
    r=await client.get('/api/session',headers=headers)
    assert r.status==401 and (await r.json())['code']=='server_restarted'


@pytest.mark.asyncio
async def test_screenshot_timeout_preserves_auth_and_cached_frame(client):
    s,token,headers=session(client)
    s.page=object();s.context=AsyncMock();s.cdp=AsyncMock()
    s.cdp.send.side_effect=asyncio.TimeoutError()
    s.last_frame=b'dummy-cached-jpeg';s.frame_time=0
    r=await client.get('/api/screenshot',headers=headers)
    assert r.status==200 and await r.read()==b'dummy-cached-jpeg'
    assert r.headers['X-Frame-Stale']=='1' and s.authorized(token)


@pytest.mark.asyncio
async def test_busy_screenshot_does_not_queue_behind_login(client):
    s,token,headers=session(client)
    await s.lock.acquire()
    try:
        r=await client.get('/api/screenshot',headers=headers)
        assert r.status==503 and (await r.json())['code']=='browser_busy'
        assert s.authorized(token)
    finally:s.lock.release()


@pytest.mark.asyncio
async def test_memory_pause_keeps_panel_results_and_login_checkpoint(client):
    s,token,headers=session(client)
    s.browser=AsyncMock();s.context=AsyncMock();s.page=SimpleNamespace(url='https://elevenlabs.io/app/api/api-keys')
    storage={'cookies':[{'name':'dummy-auth','value':'not-real'}],'origins':[]}
    s.context.storage_state.return_value=storage
    row=SimpleNamespace(api_key='dummy-collected-key',message='')
    batch=SimpleNamespace(running=True,halt=AsyncMock(),message='',current=row)
    s.batch=batch
    await s.pause_browser_for_memory()
    assert s.authorized(token) and row.api_key=='dummy-collected-key'
    assert s.page is None and s.saved_storage==storage
    assert 'memory' in s.browser_notice
    batch.halt.assert_awaited_once()
    await s.dispose_browser()
    assert s.saved_storage is None  # New account boundary cannot reuse old account state.


def test_low_memory_mode_preserves_authentication_and_captcha_requests():
    assert '--disable-gpu' in chromium_args()
    assert unnecessary_request('www.googletagmanager.com','script')
    assert unnecessary_request('fonts.example.test','font')
    for host in ['elevenlabs.io','identitytoolkit.googleapis.com','www.google.com','www.gstatic.com','challenges.cloudflare.com']:
        assert not unnecessary_request(host,'script')
        assert not unnecessary_request(host,'image')
