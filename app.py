"""Single-owner Telegram launcher for an ephemeral ElevenLabs browser.
Never put credentials in Telegram. Run one instance / one Python worker only.
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from batch import Batch, KEY_RE, parse_emails, MAX_ACCOUNTS, MAX_EMAIL_TEXT_CHARS, BATCH_SESSION_MINUTES, BATCH_SESSION_SECONDS

ROOT = Path(__file__).parent
LOGIN_URL = 'https://elevenlabs.io/app/sign-in'
KEYS_URL = 'https://elevenlabs.io/app/api/api-keys'
WIDTH, HEIGHT = 1100, 780
log = logging.getLogger('bot')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Problem(Exception):
    pass


class State:
    def __init__(self):
        self.http = None
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        self.bot_lock = asyncio.Lock()
        self.ticket = ''
        self.ticket_until = 0
        self.session = ''
        self.expires = 0
        self.session_started = time.monotonic()
        self.last_action = 0
        self.seen = set()
        self.batch = None

    def authorized(self, token):
        now = time.monotonic()
        return bool(token and self.session and hmac.compare_digest(digest(token), self.session)
                    and now < self.expires and now - self.last_action < 600)

    async def close_browser(self):
        self.session = ''
        if self.batch:
            await self.batch.halt(clear_password=True)
            for row in self.batch.rows:
                row.api_key = ''
            self.batch = None
        await self.dispose_browser()

    async def dispose_browser(self):
        # Destroy only the current account browser; keep the panel/batch session.
        if self.context:
            with contextlib.suppress(Exception):
                await self.context.close()
        if self.browser:
            with contextlib.suppress(Exception):
                await self.browser.close()
        self.context = self.browser = self.page = None

    async def open_browser(self, url=LOGIN_URL):
        await self.dispose_browser()
        try:
            self.browser = await self.pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            self.context = await self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT}, locale='en-US', accept_downloads=False, service_workers='block')
            await self.context.route('**/*', block_local_network)
            await self.context.grant_permissions(['clipboard-read', 'clipboard-write'], origin='https://elevenlabs.io')
            self.page = await self.context.new_page()
            self.page.set_default_timeout(7000)
            await self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except Exception:
            await self.dispose_browser()
            raise Problem('Browser could not start or the site could not load. Render may need more memory.')

    async def new_ticket(self):
        await self.close_browser()
        value = secrets.token_urlsafe(32)
        self.ticket, self.ticket_until = digest(value), time.monotonic() + 300
        return value


@web.middleware
async def guard(request, handler):
    try:
        if request.path.startswith('/api/') and request.method == 'POST':
            if request.headers.get('Origin') != request.app['origin']:
                raise web.HTTPForbidden(text='Invalid origin')
            if request.content_type != 'application/json':
                raise web.HTTPUnsupportedMediaType()
        response = await handler(request)
    except Problem as exc:
        response = web.json_response({'error': str(exc)}, status=400)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, text=exc.text, content_type='text/plain')
    except Exception as exc:
        # Only the error class is safe to log; Playwright details can contain typed secrets.
        log.warning('Request failed (%s); sensitive details suppressed', type(exc).__name__)
        response = web.json_response({'error': 'Action failed. Refresh the screenshot; try manual controls. If the browser closed, send /login again.'}, status=500)
    response.headers.update({
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        'Strict-Transport-Security': 'max-age=31536000',
    })
    return response


async def telegram(app, method, payload):
    async with app['state'].http.post(
        f"https://api.telegram.org/bot{app['token']}/{method}", json=payload,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        data = await response.json()
        if not data.get('ok'):
            raise Problem('Telegram request failed. Check your bot settings in Render.')
        return data['result']


async def audit(app, event):
    # Only fixed event names, never request bodies, browser state, or API keys.
    url, key = os.getenv('SUPABASE_URL', '').rstrip('/'), os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url or not key:
        return
    try:
        async with app['state'].http.post(
            url + '/rest/v1/bot_events',
            headers={'apikey': key, 'Authorization': f'Bearer {key}', 'Prefer': 'return=minimal'},
            json={'event': event}, timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status >= 300:
                log.warning('Optional audit write failed')
    except Exception:
        log.warning('Optional audit unavailable')


async def webhook(request):
    app, s = request.app, request.app['state']
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not supplied or not hmac.compare_digest(supplied, app['webhook_secret']):
        raise web.HTTPForbidden()
    update = await request.json()
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    if chat.get('type') != 'private' or message.get('from', {}).get('is_bot'):
        return web.json_response({'ok': True})
    chat_id = chat.get('id')
    if not isinstance(chat_id, int):
        return web.json_response({'ok': True})
    cmd = (message.get('text') or '').split(' ', 1)[0].split('@', 1)[0]
    async with s.bot_lock:
        uid = update.get('update_id')
        if uid in s.seen:
            return web.json_response({'ok': True})
        if cmd == '/id':
            text = f'Your private chat ID: {chat_id}\nSet ADMIN_CHAT_ID to this value in Render, then redeploy. Never send passwords to this bot.'
        elif chat_id != app['admin']:
            # Quietly ignore non-owners; /id is the sole public command.
            return web.json_response({'ok': True})
        elif cmd in ('/login', '/start', '/batch'):
            async with s.lock:
                ticket = await s.new_ticket()
            link = app['origin'] + '/#' + ticket
            await telegram(app, 'sendMessage', {
                'chat_id': chat_id,
                'text': 'Open your private browser within 5 minutes. Anyone holding this link can use it: do not forward it. Use your normal browser if Telegram cannot open the page. Passwords and keys are NOT requested in this chat.\n\n' + link,
                'link_preview_options': {'is_disabled': True},
            })
            await audit(app, 'session_link_created')
            text = None
        elif cmd == '/stop':
            async with s.lock:
                s.ticket = ''
                await s.close_browser()
            text = 'Browser closed and in-memory access revoked. A created ElevenLabs key remains valid until you revoke it in ElevenLabs.'
        else:
            text = '/login or /batch — open the private single-account/batch panel\n/stop — close it\n/id — show your chat ID\n\nDo not send passwords, verification codes, or API keys here.'
        if text:
            await telegram(app, 'sendMessage', {'chat_id': chat_id, 'text': text})
        s.seen.add(uid)
        if len(s.seen) > 1000:
            s.seen = {uid}
    return web.json_response({'ok': True})


async def block_local_network(route):
    parsed = urlparse(route.request.url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('https', 'http'):
        await route.abort()
        return
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        await route.abort()
        return
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            await route.abort()
            return
    except ValueError:
        pass
    await route.continue_()


async def claim(request):
    app, s = request.app, request.app['state']
    data = await request.json()
    value = data.get('ticket', '')
    if not isinstance(value, str) or len(value) > 100:
        raise web.HTTPUnauthorized()
    async with s.lock:
        if not s.ticket or time.monotonic() >= s.ticket_until or not hmac.compare_digest(digest(value), s.ticket):
            raise web.HTTPUnauthorized(text='Link expired or already used. Send /login again.')
        s.ticket = ''  # One use, before doing expensive browser work.
        try:
            await s.open_browser()
        except Exception:
            await s.close_browser()
            raise Problem('Browser could not start or ElevenLabs could not load. Try /login again. Render may need more memory.')
        token = secrets.token_urlsafe(32)
        s.session = digest(token)
        s.session_started = time.monotonic()
        s.expires = s.session_started + 1200
        s.last_action = time.monotonic()
    await audit(app, 'browser_opened')
    return web.json_response({'token': token, 'solver_available': bool(os.getenv('NOPECHA_API_KEY')),
                              'max_accounts': MAX_ACCOUNTS, 'max_email_chars': MAX_EMAIL_TEXT_CHARS,
                              'batch_session_minutes': BATCH_SESSION_MINUTES})


def authorize(request):
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if not request.app['state'].authorized(token):
        raise web.HTTPUnauthorized(text='Session expired. Send /login again.')


def eleven_page(page):
    return urlparse(page.url).hostname == 'elevenlabs.io'


async def screenshot(request):
    s = request.app['state']
    async with s.lock:
        authorize(request)
        if s.page is None:
            return web.Response(status=204)
        image = await s.page.screenshot(type='jpeg', quality=65, timeout=12000)
    return web.Response(body=image, content_type='image/jpeg')


async def click_named(page, pattern):
    for role in ('button', 'link', 'tab'):
        target = page.get_by_role(role, name=re.compile(pattern, re.I)).first
        if await target.count() and await target.is_visible():
            await target.click()
            return True
    return False


async def nopecha_once(app):
    """One visible static reCAPTCHA image-grid round; user reviews & verifies.
    No interception of session cookies and no promises of CAPTCHA acceptance.
    """
    key = os.getenv('NOPECHA_API_KEY', '')
    if not key:
        raise Problem('No solver configured. Manual solving is free; NopeCHA free access excludes datacenter IPs such as Render.')
    page = app['state'].page
    frame = next((f for f in page.frames if '/recaptcha/' in f.url and '/bframe' in f.url), None)
    if frame is None:
        raise Problem('No supported visible reCAPTCHA grid. Solve manually. Turnstile, hCaptcha and other types are not integrated.')
    table = frame.locator('.rc-imageselect-table-33, .rc-imageselect-table-44').first
    task = frame.locator('.rc-imageselect-desc-wrapper').first
    if not await table.count() or not await table.is_visible() or not await task.count():
        raise Problem('Open the image challenge first, or use manual solving.')
    cells = table.locator('td')
    count = await cells.count()
    if count not in (9, 16):
        raise Problem('Unsupported grid; use manual solving.')
    if await table.locator('.rc-imageselect-tileselected').count():
        raise Problem('Start with an unselected grid, or continue solving manually.')
    instruction = await task.inner_text()
    if re.search(r'(once there are none|skip|fade away)', instruction, re.I):
        raise Problem('Dynamic challenge: use manual solving. This integration supports static image grids only.')
    image = base64.b64encode(await table.screenshot(type='png')).decode()
    async with app['state'].http.post('https://api.nopecha.com/', json={
        'key': key, 'type': 'recaptcha', 'task': instruction,
        'grid': '3x3' if count == 9 else '4x4', 'image_data': [image],
    }) as r:
        result = await r.json()
    job = result.get('data')
    if result.get('error') or not isinstance(job, str):
        raise Problem('Solver rejected the request. Check API access/credits. Manual solving is still available.')
    for _ in range(20):
        await asyncio.sleep(2)
        async with app['state'].http.get('https://api.nopecha.com/', params={'key': key, 'id': job}) as r:
            result = await r.json()
        if result.get('error') == 14:
            continue
        choices = result.get('data')
        if result.get('error') or not isinstance(choices, list) or len(choices) != count or not all(type(x) is bool for x in choices):
            raise Problem('Solver did not return a valid grid. Continue manually.')
        # Guard against stale results; don't click an image that changed meanwhile.
        current = base64.b64encode(await table.screenshot(type='png')).decode()
        if current != image:
            raise Problem('Challenge changed during solving. No automated clicks made; continue manually.')
        for i, selected in enumerate(choices):
            if selected:
                await cells.nth(i).click()
        return 'Suggested squares selected. Review them in the screenshot, then click Verify yourself. Acceptance is not guaranteed.'
    raise Problem('Solver timed out. Continue manually.')


async def action(request):
    app, s = request.app, request.app['state']
    data = await request.json()
    op = data.get('op')
    async with s.lock:
        authorize(request)
        s.last_action = time.monotonic()
        if s.batch and s.batch.running and op != 'close':
            raise Problem('Pause the batch before using manual browser controls.')
        if s.batch and op in ('login', 'signin', 'keys'):
            raise Problem('Use the batch controls while a batch exists. Manual navigation can mix up account identity.')
        p = s.page
        if p is None and op != 'close':
            raise Problem('No account browser is open. Start a batch or open a fresh /login session.')
        message = 'Done. Review the browser below.'
        if op == 'click':
            x, y = float(data['x']), float(data['y'])
            if not (0 <= x <= WIDTH and 0 <= y <= HEIGHT):
                raise Problem('Click is outside the browser.')
            await p.mouse.click(x, y)
        elif op == 'type':
            text = data.get('text', '')
            if not isinstance(text, str) or not 0 < len(text) <= 2000:
                raise Problem('Enter 1–2000 characters.')
            if data.get('replace'):
                await p.keyboard.press('ControlOrMeta+A')
            await p.keyboard.insert_text(text)
        elif op == 'key':
            key = data.get('key')
            if key not in ('Tab', 'Shift+Tab', 'Enter', 'Backspace', 'Escape', 'ArrowDown', 'ArrowUp'):
                raise Problem('Unsupported keyboard control.')
            await p.keyboard.press(key)
        elif op == 'scroll':
            await p.mouse.move(WIDTH // 2, HEIGHT // 2)
            await p.mouse.wheel(0, max(-650, min(650, int(data.get('dy', 0)))))
        elif op == 'login':
            if not eleven_page(p):
                raise Problem('Not on elevenlabs.io. Do not enter credentials here. Use the Sign-in page button.')
            email, password = data.get('email', ''), data.get('password', '')
            if not isinstance(email, str) or not isinstance(password, str) or not 0 < len(email) < 320 or not 0 < len(password) <= 2000:
                raise Problem('Enter your email and password on this private page only.')
            e = p.locator('input[type=email]').first
            pw = p.locator('input[type=password]').first
            if not await e.count() or not await pw.count():
                raise Problem('Email/password form not visible. Choose email sign-in manually in the screenshot first.')
            await e.fill(email)
            await pw.fill(password)
            if not await click_named(p, r'^(sign in|log in|continue)$'):
                raise Problem('Fields filled; click the sign-in button manually below.')
            message = 'Sign-in attempted, not yet verified. Complete any CAPTCHA or email verification below.'
        elif op == 'signin':
            await p.goto(LOGIN_URL, wait_until='domcontentloaded')
        elif op == 'keys':
            await p.goto(KEYS_URL, wait_until='domcontentloaded')
            message = 'Review the page. If the URL has changed, use Developers → API Keys in the screenshot.'
        elif op == 'clipboard':
            if not eleven_page(p):
                raise Problem('Only the ElevenLabs clipboard can be read.')
            value = await p.evaluate('navigator.clipboard.readText()')
            if not value or len(value) > 4096:
                raise Problem('Clipboard is empty or too large. First click the key’s Copy button inside the browser screenshot.')
            # Returned once; neither stored in application state nor logged.
            await p.evaluate('navigator.clipboard.writeText("")')
            return web.json_response({'clipboard': value, 'message': 'Remote clipboard copied below and cleared. Check that this is the newly created key before downloading.'})
        elif op == 'solver':
            if data.get('consent') is not True:
                raise Problem('Explicit consent required to send CAPTCHA images to NopeCHA.')
            message = await nopecha_once(app)
        elif op == 'close':
            await s.close_browser()
            await audit(app, 'browser_closed')
            return web.json_response({'closed': True})
        else:
            raise Problem('Unknown action.')
        current = p.url
    return web.json_response({'message': message, 'url': current})


async def batch_start(request):
    s = request.app['state']
    data = await request.json()
    async with s.lock:
        authorize(request)
        if s.batch is not None:
            raise Problem('Download and clear the existing batch before starting another.')
        if data.get('authorize_full_access') is not True:
            raise Problem('Explicit authorization for full-access key creation is required.')
        if type(data.get('disable_leak_revocation', False)) is not bool:
            raise Problem('Invalid leaked-key protection option.')
        if data.get('disable_leak_revocation') and data.get('accept_leak_risk') is not True:
            raise Problem('Confirm the separate risk warning before disabling leaked-key revocation.')
        try:
            emails = parse_emails(data.get('emails'))
        except ValueError as exc:
            raise Problem(str(exc))
        password = data.get('password')
        if not isinstance(password, str) or not 1 <= len(password) <= 2000:
            raise Problem('Enter the shared password once on the private panel.')
        # The worker destroys the current browser before logging into its first account.
        s.batch = Batch(s, emails, password, data.get('disable_leak_revocation', False))
        # Fixed ceiling measured from claim; a fresh batch cannot perpetually extend access.
        s.expires = max(s.expires, s.session_started + BATCH_SESSION_SECONDS)
        s.last_action = time.monotonic()
        s.batch.launch()
        result = s.batch.public()
    return web.json_response(result)


async def batch_status(request):
    s = request.app['state']
    authorize(request)
    # Snapshot contains no password/key. No lock wait during a long browser step.
    snapshot = s.batch.public() if s.batch else {'mode': 'none', 'rows': []}
    snapshot['session_seconds_remaining'] = max(0, int(s.expires - time.monotonic()))
    snapshot['idle_seconds_remaining'] = max(0, int(600 - (time.monotonic() - s.last_action)))
    return web.json_response(snapshot)


async def batch_control(request):
    s = request.app['state']
    data = await request.json()
    async with s.lock:
        authorize(request)
        b = s.batch
        if not b:
            raise Problem('No batch exists.')
        s.last_action = time.monotonic()
        op = data.get('op')
        if op == 'pause':
            if b.mode in ('finished', 'cancelled'):
                raise Problem('This batch has already ended.')
            await b.halt()
            b.message = 'Paused. Manual controls are available; Resume continues the current phase.'
        elif op == 'resume':
            if b.running:
                raise Problem('Batch is already running.')
            if data.get('confirm_identity') is True:
                if not b.current or b.current.phase != 'identity' or data.get('expected_email') != b.current.email:
                    raise Problem('Identity confirmation does not match the current row.')
                b.current.identity = 'user_confirmed'
            try:
                b.launch()
            except ValueError as exc:
                raise Problem(str(exc))
        elif op == 'skip':
            if b.running or not b.current or b.mode in ('cancelled', 'finished'):
                raise Problem('Pause an active row before skipping it.')
            if data.get('confirm_skip') is not True:
                raise Problem('Confirm skipping. A possibly created key will NOT be revoked.')
            await b.halt()
            row = b.current
            row.status = 'skipped_possible_key' if row.submitted else 'skipped'
            row.message = 'User skipped this row; any created key must be reviewed/revoked in ElevenLabs.'
            row.phase = 'done'
            await s.dispose_browser()
            b.index += 1
            b.launch()
        elif op == 'capture':
            if b.running or not b.current or b.mode in ('cancelled', 'finished'):
                raise Problem('Pause the current row before manually collecting a copied key.')
            row = b.current
            if row.identity == 'not_verified':
                raise Problem('Verify the signed-in account identity before collecting a key.')
            if not s.page or not eleven_page(s.page):
                raise Problem('No ElevenLabs account page is available.')
            if data.get('confirm_manual_key') is not True:
                raise Problem('Confirm the copied value is the newly created key for the current account.')
            value = (await s.page.evaluate('navigator.clipboard.readText()')).strip()
            if not KEY_RE.fullmatch(value):
                raise Problem('Remote clipboard does not contain a recognized API key format.')
            row.api_key = value
            # A manual recovery does not assert the automatic submission produced this key.
            row.settings = 'manual_settings_unverified'
            row.status = 'collected_settings_unverified'
            row.message = 'User collected a copied key. Requested permissions and leak protection were not verified automatically.'
            row.phase = 'done'
            await s.page.evaluate('navigator.clipboard.writeText("")')
            await s.dispose_browser()
            b.index += 1
            b.launch()
        elif op == 'cancel':
            await b.halt(clear_password=True)
            if b.current:
                for row in b.rows[b.index:]:
                    if row.status == 'queued':
                        row.status = 'cancelled'
            b.message = 'Cancelled. Shared password cleared; collected results remain until you clear/close the session.'
            await s.dispose_browser()
        elif op == 'clear':
            if b.running or data.get('confirm_clear') is not True:
                raise Problem('Stop the batch and confirm you saved the results before clearing.')
            await b.halt(clear_password=True)
            for row in b.rows:
                row.api_key = ''
            s.batch = None
            await s.dispose_browser()
            return web.json_response({'mode': 'none', 'rows': []})
        else:
            raise Problem('Unknown batch control.')
        return web.json_response(b.public())


async def batch_export(request):
    s = request.app['state']
    authorize(request)
    if not s.batch:
        raise Problem('No batch results exist.')
    return web.Response(text=s.batch.csv(), content_type='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="elevenlabs-batch-keys.csv"'})


async def cleanup_loop(app):
    s = app['state']
    while True:
        await asyncio.sleep(30)
        async with s.lock:
            now = time.monotonic()
            if s.session and (now >= s.expires or now - s.last_action >= 600):
                await s.close_browser()


async def lifecycle(app):
    s = app['state']
    s.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    s.pw = await async_playwright().start()
    sweeper = asyncio.create_task(cleanup_loop(app))
    async def register():
        for delay in (0, 5, 15, 30, 60):
            await asyncio.sleep(delay)
            try:
                await telegram(app, 'setWebhook', {'url': app['origin'] + '/telegram/webhook', 'secret_token': app['webhook_secret'], 'allowed_updates': ['message'], 'max_connections': 1})
                log.info('Telegram webhook registered')
                return
            except Exception:
                log.warning('Webhook registration failed; check Render bot token and public URL')
    registration = asyncio.create_task(register())
    yield
    for task in (registration, sweeper):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await s.close_browser()
    await s.pw.stop()
    await s.http.close()


def create_app():
    app = web.Application(middlewares=[guard], client_max_size=2 * 1024 * 1024)
    app['origin'] = os.getenv('PUBLIC_BASE_URL', os.getenv('RENDER_EXTERNAL_URL', '')).rstrip('/')
    app['token'] = os.getenv('TELEGRAM_BOT_TOKEN', '')
    app['webhook_secret'] = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
    app['admin'] = int(os.getenv('ADMIN_CHAT_ID', '0'))
    app['state'] = State()
    async def health(request):
        return web.json_response({'ok': True})
    async def asset(request):
        name = request.match_info.get('name') or 'index.html'
        if name not in ('index.html', 'app.js', 'style.css'):
            raise web.HTTPNotFound()
        content_type = {'index.html': 'text/html', 'app.js': 'application/javascript', 'style.css': 'text/css'}[name]
        return web.Response(text=(ROOT / 'web' / name).read_text(), content_type=content_type)
    app.router.add_get('/health', health)
    app.router.add_get('/', asset)
    app.router.add_get('/assets/{name}', asset)
    app.router.add_post('/telegram/webhook', webhook)
    app.router.add_post('/api/claim', claim)
    app.router.add_get('/api/screenshot', screenshot)
    app.router.add_post('/api/action', action)
    app.router.add_post('/api/batch/start', batch_start)
    app.router.add_post('/api/batch/control', batch_control)
    app.router.add_get('/api/batch/status', batch_status)
    app.router.add_post('/api/batch/export', batch_export)
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Access logs disabled, including URLs carrying Telegram's bot token.
    app = create_app()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', app['token']):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN in Render environment settings.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', app['webhook_secret']):
        raise SystemExit('Set TELEGRAM_WEBHOOK_SECRET to 32–256 random URL-safe characters.')
    parsed = urlparse(app['origin'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
        raise SystemExit('PUBLIC_BASE_URL / RENDER_EXTERNAL_URL must be a public HTTPS origin, without a path.')
    web.run_app(app, host='0.0.0.0', port=int(os.getenv('PORT', '10000')), access_log=None)
