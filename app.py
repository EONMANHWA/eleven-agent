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

from eleven_http import ElevenClient, Row, key_text, result_chunks, retry_after_seconds, csv_bytes, parse_emails, extract_emails, message_email_text

log = logging.getLogger('bot')
HELP = ('Telegram-only ElevenLabs bot — no browser or panel.\n\n'
        '/batch — start: shared password, then emails\n'
        '/run — review and approve collected emails\n'
        '/emails — review detected addresses\n'
        '/status — progress / automatic-wait countdown\n/results — keys and outcomes as text\n'
        '/csv — optional CSV backup\n'
        '/cancel — stop safely and clear the password\n'
        '/forget — discard idle input and saved results\n\n'
        'Each approved batch creates ONE NEW key per account; existing keys are not changed. '
        'Use only accounts you own or are authorized to manage. '
        'CAPTCHA, 2FA, and rate limits are not bypassed. '
        'Bot chats are not end-to-end encrypted. Input deletion is best effort, '
        'not a guarantee that all copies disappear. Keys are sent here as plain text as they are collected. '
        'Failures are recorded and skipped automatically; transient pre-creation failures get one retry after a wait. '
        'No manual /resume is needed. Free-host restarts can interrupt long runs and clear RAM.')


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
    skipped_messages: int = 0
    wait_until: float = 0
    touched: float = field(default_factory=time.monotonic)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class Bot:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.client = ElevenClient(http, settings.firebase_key)
        self.session = Session()
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.last_send_at = float('-inf')
        self.seen = set()
        self.worker = None
        self.input_tasks = set()
        self.progress_id = None
        self.intake_notice_at = float('-inf')
        self.ready = False
        self.shutting_down = False

    @property
    def active(self):
        return self.worker is not None and not self.worker.done()

    async def telegram(self, method, data=None, form=None):
        # Serialize outgoing chat messages and respect Telegram's own flood waits.
        # Never retry an uncertain network result; an explicit 429 is safe to retry.
        async with self.send_lock:
            for attempt in range(2):
                try:
                    if method in ('sendMessage', 'sendDocument', 'editMessageText'):
                        await asyncio.sleep(max(0, 1.05 - (time.monotonic() - self.last_send_at)))
                    async with self.http.post('https://api.telegram.org/bot' + self.settings.bot_token + '/' + method,
                                              json=data if form is None else None, data=form,
                                              timeout=aiohttp.ClientTimeout(total=12), allow_redirects=False) as response:
                        result = await response.json()
                        if method in ('sendMessage', 'sendDocument', 'editMessageText'):
                            self.last_send_at = time.monotonic()
                        if response.status == 200 and result.get('ok'):
                            return result.get('result')
                        if response.status == 429 and attempt == 0 and form is None:
                            delay = retry_after_seconds(str(result.get('parameters', {}).get('retry_after', 1)))
                            await asyncio.sleep(max(1, delay))
                            continue
                except Exception:
                    pass  # No tokens, URLs, payloads, or exception details in logs.
                break
        return None

    async def say(self, text, **extra):
        return await self.telegram('sendMessage', {'chat_id': self.settings.owner, 'text': text, **extra})

    async def delete_input(self, message):
        deleted = await self.telegram('deleteMessage', {'chat_id': self.settings.owner,
                                                        'message_id': message['message_id']})
        if not deleted:
            await self.say('I could not delete your input message. Please delete it manually if it contains private information.')

    async def deliver_key(self, row):
        if not row.api_key or row.text_delivered:
            return True
        if await self.say(key_text(row)):
            row.text_delivered = True
            self.session.delivered_keys = max(self.session.delivered_keys,
                                              sum(r.text_delivered for r in self.session.rows))
            return True
        return False

    async def export(self, caption='Current results as text. Keep keys private.', only_undelivered=False):
        s = self.session
        if not s.rows:
            await self.say('No results yet. Use /batch to begin.')
            return False
        chunks = result_chunks(s.rows, only_undelivered)
        if not chunks:
            return True
        if not await self.say(caption):
            return False
        for text, key_rows in chunks:
            if not await self.say(text):
                await self.say('Text delivery failed. Saved keys remain in RAM: use /results soon. Do not recreate keys to recover results.')
                return False
            for row in key_rows:
                row.text_delivered = True
            s.delivered_keys = max(s.delivered_keys, sum(r.text_delivered for r in s.rows))
        return True

    async def export_csv(self, caption='Optional CSV backup. Keep it private.'):
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
            raise ValueError('Upload a UTF-8 .txt file containing text with email addresses, or forward the messages directly.')
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
            raise ValueError('Could not read the UTF-8 file. Paste or forward messages containing emails instead.') from None

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
                               'Key creation is attempted once per email. Failures do not pause the batch. Transient errors before creation get one automatic retry after a wait. '
                               '/cancel stops safely; /results sends keys as text.')
                self.worker = asyncio.create_task(self.run())
                return
            text = message.get('text', '')
            stripped = text.strip()
            forwarded = any(message.get(name) for name in ('forward_origin', 'forward_date', 'forward_from', 'forward_from_chat'))
            command = stripped.split(maxsplit=1)[0].split('@')[0].lower() if stripped and not forwarded else ''
            if command in ('/start', '/help'):
                await self.say(HELP)
                return
            if command in ('/status',):
                keys = sum(bool(r.api_key) for r in s.rows)
                await self.say(f'State: {s.mode}. Emails collected: {len(s.emails)}/{self.settings.maximum}. '
                               f'Accounts finished: {s.position}/{len(s.rows)}. Keys saved: {keys}.\n'
                               f'Messages without readable email addresses skipped: {s.skipped_messages}.\n'
                               f'Automatic wait remaining: {max(0, int(s.wait_until - time.monotonic()))} seconds.\n'
                               'Password and results are RAM-only. /results sends keys as text. '
                               'No manual /resume is needed; /cancel stops the batch.')
                return
            if command == '/emails':
                if not s.emails:
                    await self.say('No email addresses collected yet. Use /batch, send the shared password, then forward your messages.')
                else:
                    chunk = f'Detected addresses ({len(s.emails)}):\n'
                    for index, email in enumerate(s.emails, 1):
                        line = f'{index}. {email}\n'
                        if len(chunk) + len(line) > 3500:
                            await self.say(chunk)
                            chunk = ''
                        chunk += line
                    if chunk:
                        await self.say(chunk)
                return
            if command == '/csv':
                await self.export_csv()
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
                if self.active:
                    await self.say('A batch is running or waiting automatically. Use /status or /cancel first.')
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
                await self.say('This version continues automatically, including waits for rate limits. No /resume is needed. '
                               'Use /status to check progress or /cancel to stop. Completed batches do not automatically restart.')
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
                               'Keys will be delivered here as plain text. Account failures will be recorded and processing will continue automatically. Use /emails to review every detected address before approving. '
                               'All detected addresses are included, even addresses in signatures or footers.\n\n'
                               'By approving, you confirm you own/control these accounts and accept these settings and Telegram delivery. '
                               '/cancel stops without starting.', reply_markup={'inline_keyboard': [[
                                   {'text': 'Approve full access + leak auto-disable OFF', 'callback_data': 'approve:' + s.nonce}]]})
                return
            if s.mode == 'password':
                if forwarded:
                    await self.say('Send the shared password as a new plain-text message first, then forward the channel posts. This forward was not used as a password or added to the batch.')
                    return
                await self.delete_input(message)
                password = text[len('/password '):] if text.startswith('/password ') else text
                if not password or len(password) > 1024 or '\x00' in password:
                    await self.say('Send a nonempty password of at most 1024 characters as text, not a file.')
                    return
                s.password = password
                s.mode = 'emails'
                await self.say(f'Password received. Forward the channel messages here, paste any text containing emails, '
                               f'or upload a UTF-8 .txt file. I will extract up to {self.settings.maximum} unique emails '
                               'from message text, captions, and email links, ignoring surrounding words and duplicates. '
                               'Use /emails to review the list, then /run once when all forwards are sent. '
                               'No channel admin access is needed. I will try to delete input copies from this private chat, not the channel originals.')
                return
            if s.mode in ('emails', 'confirm'):
                try:
                    text = message_email_text(message)
                    if message.get('document') and str(message['document'].get('file_name', '')).lower().endswith('.txt'):
                        try:
                            text += '\n' + await self.email_document(message['document'])
                        finally:
                            await self.delete_input(message)
                    else:
                        await self.delete_input(message)
                    added = extract_emails(text, self.settings.maximum)
                    if not added:
                        s.skipped_messages += 1
                        if not forwarded:
                            await self.say('No email address found in this message. Send or forward another message containing the address. '
                                           'If the address is only inside an image, paste its text or add a caption; image-only text is not read.')
                        return
                    merged = parse_emails('\n'.join(s.emails + added), self.settings.maximum)
                    existing = {old.casefold() for old in s.emails}
                    new = [email for email in merged if email.casefold() not in existing]
                    s.emails = merged
                    if new:
                        s.mode, s.nonce = 'emails', ''
                    preview = '\n'.join(new[:5])
                    if len(new) > 5:
                        preview += f'\n… and {len(new) - 5} more (/emails to review).'
                    # Bulk forwarding can generate many updates in a burst. Keep
                    # intake sequential but avoid one outgoing reply per forward.
                    if not forwarded or time.monotonic() - self.intake_notice_at >= 3:
                        self.intake_notice_at = time.monotonic()
                        await self.say(f'{len(new)} new emails found; {len(s.emails)} unique emails collected.\n'
                                       f'{preview}\nForward more messages, or /run once when finished.')
                except ValueError as exc:
                    await self.say(str(exc))
                return
            # Unexpected private text might be a password. Delete rather than echo/store it.
            if text or message.get('document'):
                await self.delete_input(message)
            await self.say('Use /batch to begin, /status for progress, or /help for instructions. No credentials were added.')

    async def automatic_wait(self, seconds, reason):
        s = self.session
        s.wait_until = time.monotonic() + seconds
        s.mode = 'waiting' if seconds >= 5 else 'running'
        if seconds >= 5:
            await self.progress(f'Automatic wait: {int(seconds)} seconds. {reason}\n'
                                f'{s.position}/{len(s.rows)} accounts finished. No /resume needed; /cancel stops safely.')
        try:
            remaining = max(0, s.wait_until - time.monotonic())
            if remaining:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(s.stop.wait(), timeout=remaining)
            return not s.stop.is_set()
        finally:
            s.wait_until = 0
            s.mode = 'running'

    async def run(self):
        s = self.session
        rate_streak = 0
        next_delay = 0
        final_note = ''
        try:
            while s.position < len(s.rows) and not s.stop.is_set():
                if next_delay and not await self.automatic_wait(next_delay, 'Respecting the provider cooldown before the next account.'):
                    break
                next_delay = 0
                row = s.rows[s.position]
                for attempt in range(2):
                    await self.client.account(row, s.password, s.stop)
                    if row.throttled or row.status == 'rate_limited':
                        rate_streak += 1
                        next_delay = max(row.retry_after, min(600, 60 * 2 ** min(rate_streak - 1, 4)))
                    elif row.status == 'service_unavailable':
                        next_delay = max(row.retry_after, 15)
                    else:
                        rate_streak = 0
                        next_delay = row.retry_after
                    if not (attempt == 0 and row.retryable and not row.creation_attempted
                            and not row.api_key and not s.stop.is_set()):
                        break
                    if not await self.automatic_wait(max(15, next_delay),
                            'Retrying this transient failure once. Key creation has not started.'):
                        break
                    next_delay = 0
                s.position += 1
                if row.retryable and row.attempts >= 2:
                    row.note += ' Automatic retry exhausted; moving to the next email.'
                if row.api_key:
                    await self.deliver_key(row)
                if row.status != 'created_verified' or s.position == 1 or s.position % 5 == 0:
                    await self.progress(f'{s.position}/{len(s.rows)} accounts finished. '
                                        f'{sum(bool(r.api_key) for r in s.rows)} keys saved.\n'
                                        f'Last account: {row.email}\nStatus: {row.status}\n{row.note}\n'
                                        'Continuing automatically. /results sends keys and outcomes as text.')
                # Retry text delivery periodically, without recreating keys or
                # interrupting the remaining account queue if Telegram is down.
                if s.position % 10 == 0 and s.position < len(s.rows):
                    await self.export('Previously undelivered keys (text checkpoint).', only_undelivered=True)
                if s.position < len(s.rows) and not next_delay:
                    if not await self.automatic_wait(2, 'Normal sequential pacing.'):
                        break
        except asyncio.CancelledError:
            s.stop.set()
            final_note = 'Host shutdown interrupted the batch. Review uncertain rows before starting any new batch.'
        except Exception:
            s.stop.set()
            final_note = 'An internal error stopped the worker. Saved results follow; uncertain creations must be checked manually.'
            log.warning('Batch stopped; sensitive error details suppressed.')
        finally:
            s.mode = 'done'
            s.wait_until = 0
            s.password = ''
            s.touched = time.monotonic()
            if s.stop.is_set():
                for row in s.rows:
                    if row.status == 'queued':
                        row.status = 'cancelled'
            await self.export('Saved keys not yet delivered, as text.', only_undelivered=True)
            failures = [row for row in s.rows if not row.api_key and row.status != 'queued']
            await self.say(f'Finished/stopped: {s.position}/{len(s.rows)} accounts processed; '
                           f'{sum(bool(r.api_key) for r in s.rows)} keys saved. {final_note}\n'
                           'Shared password cleared. Keys are plain-text messages above. /results resends all saved results; /csv is optional. '
                           'Retry only accounts without a key after resolving their errors. Never blindly rerun an uncertain creation.')
            for text, _ in result_chunks(failures):
                await self.say(text)

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
            ('status', 'Check progress'), ('emails', 'Review extracted email addresses'), ('results', 'Send saved keys as plain text'), ('csv', 'Optional CSV backup'),
            ('cancel', 'Stop safely'),
            ('forget', 'Discard idle RAM data'), ('help', 'Instructions and privacy')]]
        while not self.shutting_down:
            result = await self.telegram('setWebhook', {'url': self.settings.public_url.rstrip('/') + '/telegram',
                'secret_token': self.settings.webhook_secret, 'allowed_updates': ['message', 'callback_query'],
                'max_connections': 1, 'drop_pending_updates': False})
            if result:
                self.ready = True
                await self.telegram('setMyCommands', {'commands': commands})
                await self.telegram('setMyDescription', {'description': 'Private, owner-only ElevenLabs key manager. Automatic batch continuation and plain-text keys; no browser. Use /batch.'})
                return
            log.warning('Webhook registration not ready; retrying without logging credentials.')
            await asyncio.sleep(20)


BOT_KEY = web.AppKey('bot', Bot)


def create_app(bot):
    app = web.Application(client_max_size=1024 * 1024)
    app[BOT_KEY] = bot

    async def health(request):
        return web.json_response({'ok': True, 'mode': 'telegram-http', 'browser': False, 'webhook_ready': bot.ready,
                                  'automatic_continuation': True, 'key_delivery': 'plain_text'})

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
