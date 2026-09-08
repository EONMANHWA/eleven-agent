"""Batch tests use invented accounts and a local fixture, never ElevenLabs auth."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import app as module
from batch import Batch, ElevenUI, NeedsInput, Row, parse_emails


@pytest.mark.parametrize('raw', ['', 'not-an-email', 'a@x.test\na@x.test', 'A@x.test\na@x.test', '\n'.join(f'{i}@x.test' for i in range(101))])
def test_invalid_lists(raw):
    with pytest.raises(ValueError):
        parse_emails(raw)


def test_shared_password_list_format():
    assert parse_emails(' a@x.test\n b@x.test; c@x.test ') == ['a@x.test','b@x.test','c@x.test']


def state():
    s = module.State()
    s.session = module.digest('test-session')
    s.expires = time.monotonic() + 1200
    s.last_action = time.monotonic()
    s.dispose_browser = AsyncMock()
    return s


class DummyDriver:
    def __init__(self):
        self.open_count = 0
        self.login_emails = []
        self.creates = 0
        self.collect_calls = 0
        self.submission_timeout = False
        self.identity_ok = True

    async def open(self): self.open_count += 1
    async def login(self, row, password):
        assert password == 'dummy-shared-password'
        self.login_emails.append(row.email)
        row.login_attempted = True
        row.phase = 'auth'
    async def authenticated(self): pass
    async def identity(self, email): return self.identity_ok
    async def keys(self): pass
    async def open_form(self): pass
    async def configure(self, row, disable_leak):
        self.creates += 1
        row.submitted = True
        row.phase = 'collect'
        row.settings = 'pre_submit_ui_verified'
        row.permission_count = 2
        if self.submission_timeout:
            raise RuntimeError('dummy create response lost')
    async def collect(self):
        self.collect_calls += 1
        return 'sk_dummy_key_not_real_' + str(self.collect_calls)*30


@pytest.mark.asyncio
async def test_batch_sequences_isolates_and_scrubs_shared_password():
    s, driver = state(), DummyDriver()
    b = Batch(s, ['a@x.test','b@x.test'], 'dummy-shared-password', True, driver)
    s.batch = b
    b.launch()
    await b.task
    assert b.mode == 'finished'
    assert driver.login_emails == ['a@x.test','b@x.test']
    assert driver.open_count == 2 and driver.creates == 2
    assert b._password == ''
    assert all(r.api_key and r.settings == 'pre_submit_ui_verified' for r in b.rows)
    public = json.dumps(b.public())
    assert 'dummy-shared-password' not in public and 'sk_dummy' not in public
    assert 'dummy-shared-password' not in b.csv()
    assert 'sk_dummy' in b.csv()
    assert s.dispose_browser.await_count == 2
    await s.close_browser()
    assert s.batch is None and not s.session
    assert all(not r.api_key for r in b.rows)


@pytest.mark.asyncio
async def test_ambiguous_create_is_never_retried():
    s, driver = state(), DummyDriver()
    driver.submission_timeout = True
    b = Batch(s, ['a@x.test'], 'dummy-shared-password', False, driver)
    b.launch(); await b.task
    assert b.mode == 'paused' and b.current.phase == 'collect'
    assert driver.creates == 1
    b.launch(); await b.task
    assert b.mode == 'finished' and driver.creates == 1
    assert b.rows[0].api_key


@pytest.mark.asyncio
async def test_identity_pause_and_cancel_clear_password():
    s, driver = state(), DummyDriver()
    driver.identity_ok = False
    b = Batch(s, ['a@x.test'], 'dummy-shared-password', True, driver)
    b.launch(); await b.task
    assert b.mode == 'paused' and b.current.phase == 'identity'
    assert driver.creates == 0
    await b.halt(clear_password=True)
    assert b._password == '' and b.mode == 'cancelled'
    with pytest.raises(ValueError): b.launch()


def test_csv_formula_defense():
    b = Batch(state(), ['=evil@x.test'], 'dummy-shared-password')
    assert "'=evil@x.test" in b.csv()


@pytest.mark.asyncio
async def test_batch_http_authorization_and_risk_consents(aiohttp_client, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL','https://bot.example')
    a = module.create_app(); a.cleanup_ctx.clear()
    s = a['state']; s.session=module.digest('test-session'); s.expires=time.monotonic()+1200; s.last_action=time.monotonic()
    client = await aiohttp_client(a)
    headers={'Origin':'https://bot.example','Authorization':'Bearer test-session'}
    for url in ('/api/batch/start','/api/batch/control','/api/batch/export'):
        assert (await client.post(url,json={})).status == 403
        assert (await client.post(url,headers={'Origin':'https://bot.example'},json={})).status == 401
    assert (await client.get('/api/batch/status')).status == 401
    payload={'emails':'a@x.test','password':'dummy-shared-password','disable_leak_revocation':True}
    r=await client.post('/api/batch/start',headers=headers,json=payload)
    assert r.status==400 and 'full-access' in (await r.json())['error']
    payload['authorize_full_access']=True
    r=await client.post('/api/batch/start',headers=headers,json=payload)
    assert r.status==400 and 'risk' in (await r.json())['error']
    assert s.batch is None
    b=Batch(s,['a@x.test'],'dummy-shared-password',True,DummyDriver());s.batch=b
    b.rows[0].api_key='sk_dummy_not_real_'+'x'*30
    r=await client.get('/api/batch/status',headers=headers)
    assert 'sk_dummy' not in await r.text()
    r=await client.post('/api/batch/export',headers=headers,json={})
    assert r.status==200 and 'sk_dummy' in await r.text()
    assert r.headers['Cache-Control']=='no-store'
    b.mode='running'
    r=await client.post('/api/action',headers=headers,json={'op':'type','text':'dummy'})
    assert r.status==400 and 'Pause' in (await r.json())['error']
    await s.close_browser()


FIXTURE = '''<!doctype html><meta charset="utf-8"><body><script>
const path=location.pathname;
if(path.includes('sign-in')) {
 document.body.innerHTML='<form id="login"><input type="email"><input type="password"><button>Sign in</button></form>';
 document.querySelector('form').onsubmit=e=>{e.preventDefault();localStorage.setItem('email',document.querySelector('input[type=email]').value);location.href='/app/home';};
} else {
 const account=localStorage.getItem('email');
 document.body.innerHTML='<h1>Fixture ElevenLabs dashboard — no real account</h1><p id="identity"></p>';
 document.querySelector('#identity').textContent=account;
 if(path.includes('api-keys')) {
 const create=document.createElement('button');create.textContent='Create Key';document.body.append(create);
 create.onclick=()=>{
 const d=document.createElement('div');d.setAttribute('role','dialog');
 d.innerHTML=`<h2>Create API Key</h2><label>Name<input id="name"></label>
 <button role="switch" aria-label="Restrict Key" aria-checked="false">Restrict Key</button>
 <button role="switch" aria-label="Automatically revoke leaked keys" aria-checked="true">Automatically revoke leaked keys</button>
 <h3>Text to Speech</h3><div class="permission"><button aria-pressed="true">No Access</button><button aria-pressed="false">Access</button></div>
 <h3>Voices</h3><div class="permission"><button aria-pressed="true">No Access</button><button aria-pressed="false">Read</button><button aria-pressed="false">Write</button></div>
 <button id="confirm">Create Key</button>`;
 document.body.append(d);
 d.querySelectorAll('[role=switch]').forEach(b=>b.onclick=()=>b.setAttribute('aria-checked',b.getAttribute('aria-checked')==='true'?'false':'true'));
 d.querySelectorAll('.permission button').forEach(b=>b.onclick=()=>{b.parentElement.querySelectorAll('button').forEach(s=>s.setAttribute('aria-pressed','false'));b.setAttribute('aria-pressed','true');});
 d.querySelector('#confirm').onclick=()=>{
 window.settingsSnapshot={restrict:d.querySelector('[aria-label="Restrict Key"]').getAttribute('aria-checked'),leak:d.querySelector('[aria-label="Automatically revoke leaked keys"]').getAttribute('aria-checked'),choices:[...d.querySelectorAll('.permission [aria-pressed=true]')].map(e=>e.innerText)};
 const key='sk_fixture_not_real_'+account.replace(/[^a-z]/g,'')+'x'.repeat(30);
 d.innerHTML='<h2>API key created. Save it now.</h2><input readonly id="secret">';d.querySelector('input').value=key;
 };
 };
 }
}
</script>'''


@pytest.mark.asyncio
async def test_real_browser_driver_settings_and_collection_on_fixture():
    # Serve fixture HTML at a synthetic intercepted URL. No external login requests.
    async with async_playwright() as pw:
        s=module.State()
        s.browser=await pw.chromium.launch(args=['--no-sandbox'])
        s.context=await s.browser.new_context()
        await s.context.grant_permissions(['clipboard-read','clipboard-write'],origin='https://elevenlabs.io')
        await s.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=FIXTURE))
        s.page=await s.context.new_page()
        await s.page.goto('https://elevenlabs.io/app/sign-in')
        ui=ElevenUI(s);row=Row('fixture@x.test','fixture-batch-key')
        await ui.login(row,'dummy-shared-password')
        assert row.login_attempted and row.phase=='auth'
        await ui.authenticated()
        assert await ui.identity(row.email)
        await ui.keys();await ui.open_form()
        await ui.configure(row,disable_leak=True)
        assert row.submitted and row.phase=='collect'
        assert row.permission_count==2
        assert await s.page.evaluate('settingsSnapshot')=={'restrict':'true','leak':'false','choices':['Access','Write']}
        key=await ui.collect()
        assert key.startswith('sk_fixture_not_real_')
        await s.dispose_browser()


@pytest.mark.asyncio
async def test_unknown_or_disabled_settings_pause_before_create():
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(args=['--no-sandbox'])
        context=await browser.new_context()
        await context.grant_permissions(['clipboard-read','clipboard-write'],origin='https://elevenlabs.io')
        await context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=FIXTURE))
        p=await context.new_page();s=module.State();s.page=p
        await p.goto('https://elevenlabs.io/app/api/api-keys')
        ui=ElevenUI(s);await ui.open_form()
        await p.locator('.permission').last.get_by_role('button',name='Write',exact=True).evaluate('(e)=>e.disabled=true')
        row=Row('fixture@x.test','fixture-key')
        with pytest.raises(NeedsInput): await ui.configure(row,True)
        assert not row.submitted and row.settings=='not_verified'
        await p.locator('.permission').last.get_by_role('button',name='Write',exact=True).evaluate('(e)=>e.disabled=false')
        await p.locator('[aria-label="Automatically revoke leaked keys"]').evaluate('(e)=>e.remove()')
        with pytest.raises(NeedsInput): await ui.configure(row,True)
        assert not row.submitted
        await browser.close()


@pytest.mark.asyncio
async def test_two_real_isolated_browsers_end_to_end_fixture(monkeypatch):
    async def fixture_route(route):
        await route.fulfill(content_type='text/html',body=FIXTURE)
    monkeypatch.setattr(module,'block_local_network',fixture_route)
    async with async_playwright() as pw:
        s=module.State();s.pw=pw
        s.session=module.digest('test-session');s.expires=time.monotonic()+1200;s.last_action=time.monotonic()
        class TrackingUI(ElevenUI):
            contexts=[]
            async def open(self):
                await super().open()
                assert await self.page.evaluate('localStorage.getItem("email")') is None
                self.contexts.append(self.state.context)
        ui=TrackingUI(s)
        b=Batch(s,['first@x.test','second@x.test'],'dummy-shared-password',True,ui);s.batch=b
        b.launch();await b.task
        assert b.mode=='finished', b.message
        assert len(ui.contexts)==2 and ui.contexts[0] is not ui.contexts[1]
        assert all(r.status=='collected_ui_verified' and r.permission_count==2 for r in b.rows)
        assert 'firstxtest' in b.rows[0].api_key and 'secondxtest' in b.rows[1].api_key
        assert b._password=='' and s.page is None and s.session
        await s.close_browser()


def test_accepts_one_hundred_emails_and_rejects_101():
    addresses = [f'owner{i:03d}@example.test' for i in range(100)]
    assert parse_emails('\n'.join(addresses)) == addresses
    with pytest.raises(ValueError):
        parse_emails('\n'.join(addresses + ['extra@example.test']))


def test_hundred_max_length_addresses_fit_text_limit():
    domain = 'a'*63 + '.' + 'b'*63 + '.' + 'c'*57 + '.com'
    emails = [f'user{i:03d}' + 'x'*57 + '@' + domain for i in range(100)]
    assert all(len(email) == 254 for email in emails)
    assert len(parse_emails('\n'.join(emails))) == 100


def test_batch_settings_validation(monkeypatch):
    from batch import bounded_setting
    monkeypatch.setenv('TEST_BATCH_LIMIT', '250')
    assert bounded_setting('TEST_BATCH_LIMIT', 100, 1, 1000) == 250
    for invalid in ['0', '1001', 'not-an-integer']:
        monkeypatch.setenv('TEST_BATCH_LIMIT', invalid)
        with pytest.raises(ValueError):
            bounded_setting('TEST_BATCH_LIMIT', 100, 1, 1000)


@pytest.mark.asyncio
async def test_large_http_batch_and_fixed_extended_session(aiohttp_client, monkeypatch):
    from batch import BATCH_SESSION_SECONDS
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://bot.example')
    # Test acceptance/session lifecycle only, not 100 real account logins.
    monkeypatch.setattr(Batch, 'run', AsyncMock(return_value=None))
    a = module.create_app(); a.cleanup_ctx.clear()
    s = a['state']; s.session = module.digest('test-session')
    s.session_started = time.monotonic(); s.last_action = time.monotonic()
    s.expires = s.session_started + 1200
    headers = {'Origin':'https://bot.example', 'Authorization':'Bearer test-session'}
    client = await aiohttp_client(a)
    body = {'emails':'\n'.join(f'owner{i:03d}@example.test' for i in range(100)),
            'password':'dummy-shared-password', 'authorize_full_access':True,
            'disable_leak_revocation':False}
    response = await client.post('/api/batch/start', headers=headers, json=body)
    assert response.status == 200
    assert len((await response.json())['rows']) == 100
    fixed_deadline = s.session_started + BATCH_SESSION_SECONDS
    assert s.expires == fixed_deadline
    snapshot = await (await client.get('/api/batch/status', headers=headers)).json()
    assert snapshot['session_seconds_remaining'] > 1200
    assert snapshot['idle_seconds_remaining'] <= 600
    assert 'dummy-shared-password' not in json.dumps(snapshot)
    await client.post('/api/batch/control', headers=headers, json={'op':'cancel'})
    await client.post('/api/batch/control', headers=headers, json={'op':'clear','confirm_clear':True})
    response = await client.post('/api/batch/start', headers=headers, json=body)
    assert response.status == 200 and s.expires == fixed_deadline
    await s.close_browser()
