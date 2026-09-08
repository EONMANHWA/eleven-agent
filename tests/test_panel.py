"""Front-end smoke test with mock API responses; no real accounts or services."""
import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_panel_claim_controls_clipboard_and_close():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        page = await browser.new_page()
        fixture = await browser.new_page()
        await fixture.set_content('<h1>Dummy remote page, not ElevenLabs</h1>')
        image = await fixture.screenshot(type='jpeg')
        await fixture.close()
        ops, errors = [], []
        page.on('pageerror', lambda exc: errors.append(str(exc)))

        async def route_handler(route):
            url = route.request.url
            if url.endswith('/api/claim'):
                assert route.request.post_data_json == {'ticket': 'dummy-ticket'}
                return await route.fulfill(json={'token': 'dummy-session', 'solver_available': False})
            if url.endswith('/api/screenshot'):
                assert route.request.headers['authorization'] == 'Bearer dummy-session'
                return await route.fulfill(content_type='image/jpeg', body=image)
            if url.endswith('/api/action'):
                data = route.request.post_data_json
                ops.append(data)
                if data['op'] == 'clipboard':
                    reply = {'clipboard': 'sk_dummy_not_a_real_key_123456', 'message': 'Dummy clipboard retrieved.'}
                elif data['op'] == 'close':
                    reply = {'closed': True}
                else:
                    reply = {'message': 'Done.', 'url': 'https://elevenlabs.io/app/sign-in'}
                return await route.fulfill(json=reply)
            if '/assets/' in url:
                name = url.rsplit('/', 1)[1]
                kind = 'text/css' if name.endswith('.css') else 'application/javascript'
                return await route.fulfill(content_type=kind, body=(ROOT / 'web' / name).read_text())
            return await route.fulfill(content_type='text/html', body=(ROOT / 'web/index.html').read_text())

        await page.route('https://bot.example/**', route_handler)
        await page.goto('https://bot.example/#dummy-ticket')
        assert await page.evaluate('location.hash') == ''
        await page.get_by_role('button', name='Open private browser', exact=True).click()
        await page.locator('#workspace').wait_for(state='visible')
        await page.wait_for_function('document.getElementById("screen").naturalWidth > 0')
        assert await page.locator('#solver').is_disabled()
        await page.locator('#email').fill('dummy@example.com')
        await page.locator('#password').fill('not-a-real-password')
        await page.get_by_role('button', name='Attempt email sign-in').click()
        await page.wait_for_function('document.getElementById("status").textContent === "Done."')
        assert await page.locator('#password').input_value() == ''
        assert ops[-1]['op'] == 'login'
        await page.get_by_role('button', name='Read remote clipboard', exact=True).click()
        await page.locator('#keybox').wait_for(state='visible')
        assert await page.locator('#key').input_value() == 'sk_dummy_not_a_real_key_123456'
        async with page.expect_download() as download:
            await page.get_by_role('button', name='Download as .env').click()
        assert (await download.value).suggested_filename == 'elevenlabs.env'
        await page.get_by_role('button', name='Close session & clear local display').click()
        await page.locator('#workspace').wait_for(state='hidden')
        assert await page.locator('#key').input_value() == ''
        assert not errors
        await browser.close()


@pytest.mark.asyncio
async def test_batch_panel_shared_password_once_and_private_download():
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(args=['--no-sandbox'])
        page=await browser.new_page()
        starts=[]
        snapshot={'mode':'none','rows':[]}
        csv='email,api_key,status\nfirst@x.test,sk_dummy_not_real_123456789,collected_ui_verified\n'
        async def handler(route):
            nonlocal snapshot
            url=route.request.url
            if url.endswith('/api/claim'):
                return await route.fulfill(json={'token':'dummy-session','solver_available':False})
            if url.endswith('/api/screenshot'):
                return await route.fulfill(status=204)
            if url.endswith('/api/batch/status'):
                return await route.fulfill(json=snapshot)
            if url.endswith('/api/batch/start'):
                starts.append(route.request.post_data_json)
                snapshot={'mode':'finished','current_index':2,'message':'Fixture finished.', 'rows':[
                    {'email':email,'phase':'done','status':'collected_ui_verified','key_collected':True,'identity':'visible_email_verified'}
                    for email in ['first@x.test','second@x.test']]}
                return await route.fulfill(json=snapshot)
            if url.endswith('/api/batch/export'):
                assert route.request.headers['authorization']=='Bearer dummy-session'
                return await route.fulfill(content_type='text/csv',body=csv)
            if '/assets/' in url:
                name=url.rsplit('/',1)[1]
                return await route.fulfill(content_type='text/css' if name.endswith('.css') else 'application/javascript',body=(ROOT/'web'/name).read_text())
            return await route.fulfill(content_type='text/html',body=(ROOT/'web/index.html').read_text())
        await page.route('https://bot.example/**',handler)
        await page.goto('https://bot.example/#dummy-ticket')
        await page.get_by_role('button',name='Open private browser',exact=True).click()
        await page.locator('#workspace').wait_for(state='visible')
        await page.locator('#batchEmails').fill('first@x.test\nsecond@x.test')
        await page.locator('#batchPassword').fill('dummy-shared-password')
        await page.locator('#authorizeFullAccess').check()
        assert await page.locator('#disableLeak').is_checked()
        # Native form validation refuses to start without the separate leak-risk consent.
        await page.locator('#startBatch').click()
        assert starts==[]
        await page.locator('#acceptLeakRisk').check()
        await page.locator('#startBatch').click()
        await page.locator('#batchPanel').wait_for(state='visible')
        assert len(starts)==1
        assert starts[0]['password']=='dummy-shared-password'
        assert starts[0]['disable_leak_revocation'] is True
        assert await page.locator('#batchPassword').input_value()==''
        assert await page.locator('#batchEmails').input_value()==''
        assert await page.locator('#batchRows tr').count()==2
        assert 'sk_dummy' not in await page.locator('#batchRows').inner_text()
        assert await page.locator('#login button').is_disabled()
        async with page.expect_download() as download:
            await page.locator('#exportBatch').click()
        assert (await download.value).suggested_filename=='elevenlabs-batch-keys.csv'
        await browser.close()
