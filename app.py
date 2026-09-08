"""Single-owner, Telegram-only ElevenLabs key collection. No browser or panel.
One process/instance. Credentials and results are held in RAM only.
"""
import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from eleven_http import ElevenClient, Row, PAUSE_STATUSES, csv_bytes, parse_emails

log = logging.getLogger('bot')
HELP = ('Telegram-only ElevenLabs bot — no browser or panel.\n\n'
        '/batch — start: shared password, then emails\n'
        '/run — review and approve collected emails\n'
        '/status — progress\n/results — download current CSV\n'
        '/resume — continue with the next queued account after a pause\n'
        '/cancel — stop safely and clear the password\n'
        '/forget — discard idle input and saved results\n\n'
        'Each approved batch creates ONE NEW key per account; existing keys are not changed. '
        'Use only accounts you own or are authorized to manage. '
        'CAPTCHA, 2FA, and rate limits are not bypassed. '
        'Bot chats are not end-to-end encrypted. Input deletion is best effort, '
        'not a guarantee that all copies disappear. Results are sent here as private CSV files. '
        'Free-host restarts clear RAM; download your results.')


@dataclass(repr=False)
class Settings:
    bot_token: str
    webhook_secret: str
    owner: int
    firebase_key: str
    public_url: str = ''
    maximum: int = 100

    @classmethod
    def env(cls):
        return cls(os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_WEBHOOK_SECRET', ''),
                   int(os.getenv('ADMIN_CHAT_ID', '0')), os.getenv('ELEVENLABS_FIREBASE_API_KEY', ''),
                   os.getenv('PUBLIC_BASE_URL') or os.getenv('RENDER_EXTERNAL_URL', ''),
                   max(1, min(1000, int(os.getenv('MAX_BATCH_ACCOUNTS', '100')))))

    def validate(self):
        if not self.bot_token or len(self.webhook_secret) < 32 or self.owner <= 0 or not self.firebase_key:
            raise RuntimeError('Required Telegram owner/secrets or Firebase public client configuration are missing.')
        parsed = urlparse(self.public_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise RuntimeError('PUBLIC_BASE_URL / RENDER_EXTERNAL_URL must be an HTTPS service URL.')


@dataclass(repr=False)
class Session:
    mode: str = 'idle'
    password: str = ''
    emails: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    nonce: str = ''
    batch_id: str = ''
    position: int = 0
    delivered_keys: int = 0
    touched: float = field(default_factory=time.monotonic)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class Bot:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.client = ElevenClient(http, settings.firebase_key)
        self.session = Session()
        self.lock = asyncio.Lock()
        self.seen = set()
        self.worker = None
        self.input_tasks = set()
        self.progress_id = None
        self.ready = False
        self.shutting_down = False

    @property
    def active(self):
        return self.worker is not None and not self.worker.done()

    async def telegram(self, method, data=None, form=None):
        try:
            async with self.http.post('https://api.telegram.org/bot' + self.settings.bot_token + '/' + method,
                                      json=data if form is None else None, data=form,
                                      timeout=aiohttp.ClientTimeout(total=12), allow_redirects=False) as response:
                result = await response.json()
                if response.status == 200 and result.get('ok'):
                    return result.get('result')
        except Exception:
            pass  # No URL, token, payload, exception detail, or message content in logs.
        return None

    async def say(self, text, **extra):
        return await self.telegram('sendMessage', {'chat_id': self.settings.owner, 'text': text, **extra})

    async def delete_input(self, message):
        deleted = await self.telegram('deleteMessage', {'chat_id': self.settings.owner,
                                                        'message_id': message['message_id']})
        if not deleted:
            await self.say('I could not delete your input message. Please delete it manually if it contains private information.')

    async def export(self, caption='Current results. Keep this CSV private.'):
        s = self.session
        if not s.rows:
            await self.say('No results yet. Use /batch to begin.')
            return False
        content = csv_bytes(s.rows)
        count = sum(bool(row.api_key) for row in s.rows)
        form = aiohttp.FormData()
        form.add_field('chat_id', str(self.settings.owner))
        form.add_field('caption', caption[:1000])
        form.add_field('document', content, filename=f'elevenlabs-keys-{s.batch_id}-{s.position}.csv', content_type='text/csv')
        result = await self.telegram('sendDocument', form=form)
        if result:
            s.delivered_keys = max(s.delivered_keys, count)
            return True
        await self.say('CSV delivery failed. Results are still in RAM: use /results soon. Do not rerun the accounts to recover a file.')
        return False

    async def progress(self, text):
        if self.progress_id:
            result = await self.telegram('editMessageText', {'chat_id': self.settings.owner,
                    'message_id': self.progress_id, 'text': text})
            if result:
                return
        result = await self.say(text)
        if isinstance(result, dict):
            self.progress_id = result.get('message_id')

    def enqueue(self, update):
        task = asyncio.create_task(self.handle(update))
        self.input_tasks.add(task)
        def done(finished):
            self.input_tasks.discard(finished)
            if not finished.cancelled() and finished.exception():
                log.warning('Telegram update processing failed; details suppressed.')
        task.add_done_callback(done)

    async def email_document(self, document):
        if not str(document.get('file_name', '')).lower().endswith('.txt'):
            raise ValueError('Upload a UTF-8 .txt file containing only emails, one per line.')
        if not isinstance(document.get('file_size'), int) or not 0 < document['file_size'] <= 300000:
            raise ValueError('The .txt file must be no larger than 300 KB.')
        info = await self.telegram('getFile', {'file_id': document.get('file_id')})
        path = info.get('file_path', '') if isinstance(info, dict) else ''
        if not path or '..' in path or path.startswith('/') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/.-' for c in path):
            raise ValueError('Could not safely download the email file. Paste emails in messages instead.')
        try:
            async with self.http.get('https://api.telegram.org/file/bot' + self.settings.bot_token + '/' + path,
                                     timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('File download failed. Paste emails instead.')
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > 300000:
                        raise ValueError('File exceeds 300 KB.')
                return data.decode('utf-8-sig')
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            raise ValueError('Could not read the UTF-8 file. Paste plain emails instead.') from None

    async def handle(self, update):
        async with self.lock:
            if self.shutting_down:
                return
            message = update.get('message')
            callback = update.get('callback_query')
            source = callback.get('message', {}) if isinstance(callback, dict) else message
            sender = callback.get('from', {}) if isinstance(callback, dict) else (message or {}).get('from', {})
            if not isinstance(source, dict):
                return
            chat = source.get('chat', {})
            if chat.get('type') != 'private' or chat.get('id') != self.settings.owner or sender.get('id') != self.settings.owner:
                return
            update_id = update.get('update_id')
            if not isinstance(update_id, int) or update_id in self.seen:
                return
            self.seen.add(update_id)
            if len(self.seen) > 5000:
                self.seen = set(sorted(self.seen)[-3000:])
            s = self.session
            s.touched = time.monotonic()
            if callback:
                await self.telegram('answerCallbackQuery', {'callback_query_id': callback.get('id')})
                if s.mode != 'confirm' or callback.get('data') != 'approve:' + s.nonce:
                    await self.say('This approval is expired or already used. Use /status or /batch.')
                    return
                s.nonce = ''
                s.batch_id = secrets.token_hex(5)
                s.rows = [Row(email, f'tg-{s.batch_id}-{index + 1:04d}') for index, email in enumerate(s.emails)]
                s.mode = 'running'
                await self.say('Approved: full access, leak auto-disable OFF. Processing sequentially. '
                               'Each creation is attempted once. /cancel stops safely; /results downloads current results.')
                self.worker = asyncio.create_task(self.run())
                return
            text = message.get('text', '')
            stripped = text.strip()
            command = stripped.split(maxsplit=1)[0].split('@')[0].lower() if stripped else ''
            if command in ('/start', '/help'):
                await self.say(HELP)
                return
            if command in ('/status',):
                keys = sum(bool(r.api_key) for r in s.rows)
                await self.say(f'State: {s.mode}. Emails collected: {len(s.emails)}/{self.settings.maximum}. '
                               f'Accounts finished: {s.position}/{len(s.rows)}. Keys saved: {keys}.\n'
                               'Password and results are RAM-only. /results downloads saved keys. '
                               'After a pause, /resume processes ONLY the next queued account, not failed/uncertain rows.')
                return
            if command == '/results':
                await self.export()
                return
            if command == '/cancel':
                s.stop.set()
                s.nonce = ''
                if self.active:
                    await self.say('Stopping after the current in-flight request finishes. A key already being created may still complete. Results will follow.')
                else:
                    s.password = ''
                    s.mode = 'done' if s.rows else 'idle'
                    for row in s.rows:
                        if row.status == 'queued':
                            row.status = 'cancelled'
                    if s.rows:
                        await self.export('Batch stopped. Password cleared from active state; saved results attached.')
                    else:
                        s.emails.clear()
                    await self.say('Stopped. Shared password cleared from active state. /batch starts a new batch.')
                return
            if command == '/forget':
                if self.active:
                    await self.say('Use /cancel and wait for it to finish before /forget.')
                    return
                self.session = Session()
                await self.say('Input and results discarded from active RAM state. This does not revoke keys or delete Telegram copies.')
                return
            if command in ('/batch', '/login'):
                if self.active or s.mode == 'paused':
                    await self.say('A batch is active or paused. Use /status, /resume, or /cancel first.')
                    return
                count = sum(bool(row.api_key) for row in s.rows)
                if count > s.delivered_keys and not await self.export('Previous batch results, before starting a new batch.'):
                    return
                self.session = Session(mode='password')
                await self.say('Send the ONE shared password as your next plain-text message (not here in a group). '
                               'I will try to delete that message after reading it. If it looks like a bot command, send /password followed by a space and the password.\n\n'
                               'Bot chats are not end-to-end encrypted; deletion cannot erase every copy. '
                               'Only use accounts you own/control. No key will be created until you approve the batch. '
                               'Input expires after 15 minutes of inactivity.')
                return
            if command in ('/resume', '/skip'):
                if s.mode != 'paused' or self.active or not s.password:
                    await self.say('No resumable batch. Use /status. After a restart or password expiry, start a new /batch for unprocessed emails only.')
                    return
                s.mode = 'running'
                s.stop.clear()
                await self.say('Continuing with the next queued email. Failed, blocked, and uncertain accounts will NOT be retried.')
                self.worker = asyncio.create_task(self.run())
                return
            if command == '/run':
                if s.mode not in ('emails', 'confirm') or not s.password or not s.emails:
                    await self.say('First use /batch, send the password, and send at least one email.')
                    return
                s.mode = 'confirm'
                s.nonce = secrets.token_urlsafe(12)
                await self.say(f'Ready: {len(s.emails)} unique accounts.\n\n'
                               'Approve ONE NEW unrestricted/full-access key per account, with leak auto-disable OFF. '
                               'Leaked keys can remain usable until you revoke them; full access increases the damage a leak can cause. '
                               'Plan limits and enforced workspace policies still apply. Existing keys will not be modified. '
                               'Keys will be delivered here in a CSV.\n\n'
                               'By approving, you confirm you own/control these accounts and accept these settings and Telegram delivery. '
                               '/cancel stops without starting.', reply_markup={'inline_keyboard': [[
                                   {'text': 'Approve full access + leak auto-disable OFF', 'callback_data': 'approve:' + s.nonce}]]})
                return
            if s.mode == 'password':
                await self.delete_input(message)
                password = text[len('/password '):] if text.startswith('/password ') else text
                if not password or len(password) > 1024 or '\x00' in password:
                    await self.say('Send a nonempty password of at most 1024 characters as text, not a file.')
                    return
                s.password = password
                s.mode = 'emails'
                await self.say(f'Password received. Send up to {self.settings.maximum} emails, one per line, '
                               'in one or several messages, or upload a UTF-8 .txt file. '
                               'Duplicates are removed. Send /run when finished. I will try to delete email input messages too.')
                return
            if s.mode in ('emails', 'confirm'):
                try:
                    if message.get('document'):
                        try:
                            text = await self.email_document(message['document'])
                        finally:
                            await self.delete_input(message)
                    else:
                        await self.delete_input(message)
                    added = parse_emails(text, self.settings.maximum)
                    merged = parse_emails('\n'.join(s.emails + added), self.settings.maximum)
                    s.emails = merged
                    s.mode, s.nonce = 'emails', ''
                    await self.say(f'{len(s.emails)} unique emails collected. Send more, or /run to review and approve.')
                except ValueError as exc:
                    await self.say(str(exc))
                return
            # Unexpected private text might be a password. Delete rather than echo/store it.
            if text or message.get('document'):
                await self.delete_input(message)
            await self.say('Use /batch to begin, /status for progress, or /help for instructions. No credentials were added.')

    async def run(self):
        s = self.session
        start = time.monotonic()
        failures = 0
        pause_note = ''
        try:
            while s.position < len(s.rows) and not s.stop.is_set():
                row = s.rows[s.position]
                await self.client.account(row, s.password, s.stop)
                s.position += 1
                failures = failures + 1 if row.status == 'login_rejected' else 0
                if row.status != 'created_verified' or s.position == 1 or s.position % 5 == 0:
                    await self.progress(f'{s.position}/{len(s.rows)} accounts finished. '
                                        f'{sum(bool(r.api_key) for r in s.rows)} keys saved.\n'
                                        f'Last account: {row.email}\nStatus: {row.status}\n{row.note}\n/results for the current CSV.')
                if row.status in PAUSE_STATUSES or failures >= 3:
                    s.mode = 'paused'
                    pause_note = ('Verification, rate limit, uncertain outcome, or protocol error needs attention. '
                                  'No browser will open and no failed account will be automatically retried.')
                    break
                if time.monotonic() - start >= 480 and s.position < len(s.rows):
                    s.mode = 'paused'
                    pause_note = 'Paused at the eight-minute work window to keep free-host runs bounded.'
                    break
                if s.position % 10 == 0 and s.position < len(s.rows):
                    if not await self.export('Checkpoint: partial results. More accounts may still be running.'):
                        s.mode = 'paused'
                        pause_note = 'Paused because CSV delivery failed. Use /results before continuing.'
                        break
                if s.position < len(s.rows):
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(s.stop.wait(), timeout=1)
        except asyncio.CancelledError:
            s.stop.set()
            pause_note = 'Host shutdown interrupted the batch. Review uncertain rows before starting any new batch.'
        except Exception:
            s.stop.set()
            pause_note = 'Batch stopped after an internal error; no automatic retry. Check saved results.'
            log.warning('Batch stopped; sensitive error details suppressed.')
        finally:
            if s.stop.is_set():
                s.mode = 'done'
                for row in s.rows:
                    if row.status == 'queued':
                        row.status = 'cancelled'
            elif s.position >= len(s.rows):
                s.mode = 'done'
            if s.mode != 'paused':
                s.password = ''
            s.touched = time.monotonic()
            await self.export('Partial results: batch paused.' if s.mode == 'paused' else 'Batch results. Shared password cleared from active state.')
            await self.say((f'Paused after {s.position}/{len(s.rows)} accounts. {pause_note}\n'
                            'Use /resume ONLY to move to the next queued email, /results to save keys, or /cancel. '
                            'The password expires after 15 minutes of inactivity.') if s.mode == 'paused' else
                           (f'Finished/stopped: {s.position}/{len(s.rows)} accounts processed, '
                            f'{sum(bool(r.api_key) for r in s.rows)} keys saved. {pause_note}\n'
                            'Download the CSV. /batch creates a new batch; never blindly rerun uncertain accounts.'))

    async def housekeeping(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                s = self.session
                idle = time.monotonic() - s.touched
                if not self.active and s.password and idle > 900:
                    s.password = ''
                    s.nonce = ''
                    s.mode = 'done' if s.rows else 'idle'
                    for row in s.rows:
                        if row.status == 'queued':
                            row.status = 'not_processed_password_expired'
                    if s.rows:
                        await self.export('Input expired. Password cleared; unprocessed accounts were not attempted.')
                    else:
                        s.emails.clear()
                    await self.say('Shared password expired and was cleared from active state. Start /batch again for unprocessed emails only.')
                if not self.active and idle > 3600:
                    self.session = Session()

    async def register(self):
        commands = [{'command': name, 'description': desc} for name, desc in [
            ('batch', 'Start a Telegram-only batch'), ('run', 'Review emails and approve'),
            ('status', 'Check progress'), ('results', 'Download current keys CSV'),
            ('resume', 'Continue with next queued account'), ('cancel', 'Stop safely'),
            ('forget', 'Discard idle RAM data'), ('help', 'Instructions and privacy')]]
        while not self.shutting_down:
            result = await self.telegram('setWebhook', {'url': self.settings.public_url.rstrip('/') + '/telegram',
                'secret_token': self.settings.webhook_secret, 'allowed_updates': ['message', 'callback_query'],
                'max_connections': 1, 'drop_pending_updates': False})
            if result:
                self.ready = True
                await self.telegram('setMyCommands', {'commands': commands})
                await self.telegram('setMyDescription', {'description': 'Private, owner-only ElevenLabs key manager. Telegram-only input and CSV results; no browser. Use /batch.'})
                return
            log.warning('Webhook registration not ready; retrying without logging credentials.')
            await asyncio.sleep(20)


BOT_KEY = web.AppKey('bot', Bot)


def create_app(bot):
    app = web.Application(client_max_size=1024 * 1024)
    app[BOT_KEY] = bot

    async def health(request):
        return web.json_response({'ok': True, 'mode': 'telegram-http', 'browser': False, 'webhook_ready': bot.ready})

    async def root(request):
        return web.Response(text='Telegram-only bot. No browser or control panel. Open your Telegram bot and send /batch.\n')

    async def webhook(request):
        supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not supplied or not hmac.compare_digest(supplied.encode(), bot.settings.webhook_secret.encode()):
            raise web.HTTPForbidden()
        if bot.shutting_down:
            raise web.HTTPServiceUnavailable()
        try:
            update = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(update, dict):
            raise web.HTTPBadRequest()
        # Acknowledge immediately; never keep the webhook open for account processing.
        bot.enqueue(update)
        return web.json_response({'ok': True})

    app.router.add_get('/', root)
    app.router.add_get('/health', health)
    app.router.add_post('/telegram', webhook)
    return app


def main():
    settings = Settings.env()
    settings.validate()

    async def factory():
        http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8), trust_env=False)
        bot = Bot(settings, http)
        app = create_app(bot)
        house = asyncio.create_task(bot.housekeeping())
        registration = asyncio.create_task(bot.register())

        async def shutdown(app):
            bot.shutting_down = True
            house.cancel()
            registration.cancel()
            bot.session.stop.set()
            for task in list(bot.input_tasks):
                task.cancel()
            await asyncio.gather(*bot.input_tasks, return_exceptions=True)
            if bot.active:
                try:
                    await asyncio.wait_for(asyncio.shield(bot.worker), timeout=22)
                except asyncio.TimeoutError:
                    bot.worker.cancel()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(bot.worker, timeout=5)
            bot.session.password = ''
            await asyncio.gather(house, registration, return_exceptions=True)
            await http.close()
        app.on_cleanup.append(shutdown)
        return app

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    web.run_app(factory(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')), access_log=None,
                print=None, shutdown_timeout=5)


if __name__ == '__main__':
    main()
