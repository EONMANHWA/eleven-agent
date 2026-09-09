"""Browser-free account operations. Undocumented ElevenLabs web-client routes.
Never retry key creation, log provider bodies, or retain Firebase refresh tokens.
"""
import asyncio
import csv
import io
import html
import re
import time
import math
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, parse_qsl

import aiohttp

API = 'https://api.elevenlabs.io'
FIREBASE = 'https://identitytoolkit.googleapis.com/v1/accounts:'
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
KEY_RE = re.compile(r'[A-Za-z0-9_-]{20,256}\Z')


@dataclass(repr=False)
class Row:
    email: str
    name: str
    status: str = 'queued'
    api_key: str = ''
    full_access: str = 'not verified'
    leak_auto_disable: str = 'not verified'
    note: str = ''
    attempts: int = 0
    failure_stage: str = ''
    creation_attempted: bool = False
    retryable: bool = False
    throttled: bool = False
    retry_after: float = 0
    text_delivered: bool = False


class ProviderError(Exception):
    def __init__(self, status, kind, retry_after=0):
        self.status, self.kind = status, kind
        self.retry_after = retry_after
        super().__init__(kind)


def retry_after_seconds(value):
    """Respect provider Retry-After seconds or HTTP dates; never log the header."""
    if not value:
        return 0
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def key_text(row):
    return (f'Email: {row.email}\nAPI key:\n{row.api_key}\n'
            f'Full access: {row.full_access}; leak auto-disable: {row.leak_auto_disable}\n'
            f'Status: {row.status}\n')


def result_chunks(rows, only_undelivered=False):
    """Plain-text result chunks plus the rows whose keys each chunk contains."""
    chunks, text, keys = [], '', []
    for row in rows:
        if only_undelivered and (not row.api_key or row.text_delivered):
            continue
        if row.api_key:
            block = key_text(row)
        else:
            block = (f'Email: {row.email}\nStatus: {row.status}\n'
                     f'Attempts: {row.attempts}; stage: {row.failure_stage or "—"}\n{row.note}\n')
            if row.creation_attempted:
                block += f'Key name to check: {row.name}\n'
        if text and len(text) + len(block) + 1 > 3500:
            chunks.append((text, keys)); text, keys = '', []
        text += block + '\n'
        if row.api_key:
            keys.append(row)
    if text:
        chunks.append((text, keys))
    return chunks


# Extract candidates from prose, rather than requiring a clean address list.
# Boundaries avoid treating the tail of a malformed address as a new address.
LOCAL_CHARS = r"A-Za-z0-9!#$%&'*+/=?^_`{|}~\-"
DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL_FIND = re.compile(
    rf"(?<![{LOCAL_CHARS}.@])"
    rf"[{LOCAL_CHARS}]+(?:\.[{LOCAL_CHARS}]+)*@"
    rf"(?:{DOMAIN_LABEL}\.)+[A-Za-z]{{2,63}}"
    rf"(?![A-Za-z0-9_@-]|\.[A-Za-z0-9])"
)


def message_email_text(message):
    """Visible text/caption plus explicit link targets. Never fetch external URLs.

    Telegram text_link targets can hide a mailto address behind a label. Ordinary
    email entities already occur in text, so UTF-16 offset slicing is unnecessary.
    Inline button URLs are included when Telegram supplies them with a message.
    """
    pieces = [message.get('text', ''), message.get('caption', '')]
    for name in ('entities', 'caption_entities'):
        for entity in message.get(name) or []:
            if isinstance(entity, dict) and isinstance(entity.get('url'), str):
                pieces.append(entity['url'])
    markup = message.get('reply_markup') or {}
    if isinstance(markup, dict):
        for row in markup.get('inline_keyboard') or []:
            for button in row:
                if isinstance(button, dict) and isinstance(button.get('url'), str):
                    pieces.append(button['url'])
    return '\n'.join(value for value in pieces if isinstance(value, str))


def link_email_text(match):
    """Extract URL components without mistaking a URL prefix for a mailbox."""
    try:
        parsed = urlsplit(match.group())
        if parsed.scheme.lower() == 'mailto':
            parts = [unquote(parsed.path)]
        else:
            # Decode after path splitting so encoded characters in the actual
            # mailbox remain intact. Never treat HTTP userinfo as an email.
            parts = [unquote(segment) for segment in parsed.path.split('/')]
        parts.extend(value for _, value in parse_qsl(parsed.query))
        parts.extend(unquote(segment) for segment in re.split(r'[/&=]', parsed.fragment))
        return '\n' + '\n'.join(parts) + '\n'
    except ValueError:
        return '\n'


def extract_emails(text, maximum=100):
    if len(text) > 300000:
        raise ValueError('Message or file is too large (maximum 300 KB of text).')
    text = html.unescape(text)
    text = re.sub(r'(?:https?://|mailto:)[^\s<>]+', link_email_text, text, flags=re.IGNORECASE)
    # Pasted formatting wrappers are not part of the mailbox name. Internal
    # apostrophes and plus-addressing remain intact.
    for marker in ('**', '__', '~~', '`', '"', "'", '*'):
        escaped = re.escape(marker)
        text = re.sub(escaped + r'([^\s<>]{1,64}@[^\s<>]{1,253}?)' + escaped, r'\1', text)
    result, seen = [], set()
    for match in EMAIL_FIND.finditer(text):
        email = match.group()
        if len(email) > 254 or len(email.split('@')[0]) > 64:
            continue
        if email.casefold() not in seen:
            seen.add(email.casefold())
            result.append(email)
    if len(result) > maximum:
        raise ValueError(f'Maximum {maximum} unique emails per batch. Nothing from this message was added.')
    return result


def parse_emails(text, maximum=100):
    result = extract_emails(text, maximum)
    if not result:
        raise ValueError('No email address found in that message. Send or forward another message containing an address. Images need a text caption; image-only addresses cannot be read.')
    return result


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['email', 'api_key', 'key_name', 'status', 'full_access', 'leak_auto_disable', 'note', 'attempts', 'failure_stage'])
    def safe(value):
        value = str(value)
        return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r', '\n') else value
    for row in rows:
        writer.writerow([safe(v) for v in (row.email, row.api_key, row.name, row.status,
                                         row.full_access, row.leak_auto_disable, row.note, row.attempts, row.failure_stage)])
    return stream.getvalue().encode('utf-8-sig')


def error_kind(status, body):
    # Inspect only for classification; never return arbitrary provider text.
    detail = body.get('error') or body.get('detail') or {}
    text = str(detail).upper()
    if status == 429 or any(t in text for t in ('TOO_MANY_ATTEMPTS', 'RATE_LIMIT', 'QUOTA_EXCEEDED')):
        return 'rate_limited'
    if any(t in text for t in ('CAPTCHA', 'MFA_REQUIRED', 'SECOND_FACTOR', 'TWO_FACTOR', 'EMAIL_NOT_VERIFIED', 'EMAIL_UNVERIFIED')):
        return 'verification_required'
    if any(t in text for t in ('INVALID_LOGIN_CREDENTIALS', 'INVALID_PASSWORD', 'EMAIL_NOT_FOUND', 'USER_DISABLED')):
        return 'login_rejected'
    if status >= 500:
        return 'service_unavailable'
    if status in (400, 404, 405, 422):
        return 'protocol_changed'
    return 'access_denied'


class ElevenClient:
    def __init__(self, http, firebase_key):
        self.http = http
        self.firebase_key = firebase_key

    async def request(self, url, body=None, token=None):
        headers = {'Origin': 'https://elevenlabs.io', 'Referer': 'https://elevenlabs.io/',
                   'User-Agent': 'Mozilla/5.0'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        async with self.http.request('POST' if body is not None else 'GET', url,
                                     json=body, headers=headers, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=20)) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 2000000:
                    raise ProviderError(response.status, 'protocol_changed')
            import json
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            if response.status not in (200, 201):
                raise ProviderError(response.status, error_kind(response.status, data),
                                    retry_after_seconds(response.headers.get('Retry-After')))
            return data

    async def account(self, row, password, stop):
        token = ''
        # Even a mistakenly repeated call must never submit a second creation.
        if row.creation_attempted or row.api_key:
            return
        row.attempts += 1
        row.retryable = row.throttled = False
        row.retry_after = 0
        row.failure_stage = ''
        try:
            if stop.is_set():
                row.status = 'cancelled'
                return
            row.status = 'signing_in'
            auth = await self.request(FIREBASE + 'signInWithPassword?key=' + self.firebase_key,
                                      {'email': row.email, 'password': password,
                                       'returnSecureToken': True, 'clientType': 'CLIENT_TYPE_WEB'})
            password = ''
            if auth.get('mfaPendingCredential') or not auth.get('idToken'):
                row.status = 'verification_required'
                row.note = 'Additional sign-in verification is required. No key created.'
                return
            if auth.get('email', '').casefold() != row.email.casefold():
                row.status = 'protocol_changed'
                row.note = 'Sign-in identity did not match. No key created.'
                return
            token = auth.pop('idToken')
            auth.clear()  # Discard refresh token and account metadata immediately.
            lookup = await self.request(FIREBASE + 'lookup?key=' + self.firebase_key, {'idToken': token})
            users = lookup.get('users', [])
            if len(users) != 1 or users[0].get('emailVerified') is not True:
                row.status = 'verification_required'
                row.note = 'Verify the account email with ElevenLabs. No key created.'
                return
            lookup.clear()
            row.status = 'checking_workspace'
            workspace = await self.request(API + '/v1/workspace', token=token)
            if workspace.get('third_party_disable_allowed_override') is True:
                row.status = 'workspace_policy_blocked'
                row.note = 'Workspace forces leak auto-disable ON. No key created; workspace policy was not changed.'
                return
            if stop.is_set():
                row.status = 'cancelled'
                return
            row.status = 'creating'
            row.creation_attempted = True
            # Exact personal-key UI semantics: omitted permissions = unrestricted.
            # This call is attempted ONCE. No fallback payloads or automatic retries.
            created = await self.request(API + '/v1/user/create-api-key',
                                         {'name': row.name, 'third_party_disable_allowed': False}, token)
            key = created.get('xi_api_key', '')
            if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                row.status = 'creation_unknown'
                row.note = 'Creation returned an unrecognized result. Check the named key before any new batch.'
                return
            row.api_key = key
            row.status = 'created_settings_unverified'
            row.note = 'Key created; settings verification pending. Do not create a replacement automatically.'
            listed = await self.request(API + '/v1/user/api-keys', token=token)
            matches = [item for item in listed.get('api_keys', []) if item.get('name') == row.name]
            metadata = matches[0] if len(matches) == 1 else {}
            # Recheck current workspace policy, not just the pre-creation snapshot.
            workspace = await self.request(API + '/v1/workspace', token=token)
            if 'permissions' in metadata and metadata['permissions'] is None:
                row.full_access = 'yes'
            override = workspace.get('third_party_disable_allowed_override')
            if override is False or (override is None and metadata.get('third_party_disable_allowed') is False):
                row.leak_auto_disable = 'off'
            if row.full_access == 'yes' and row.leak_auto_disable == 'off':
                row.status = 'created_verified'
                row.note = 'Full access and leak auto-disable OFF verified; plan/workspace limits still apply.'
            else:
                row.note = 'Key created, but requested settings could not all be confirmed. Check this key; do not rerun blindly.'
        except ProviderError as exc:
            row.failure_stage = row.status
            row.retry_after = exc.retry_after
            row.throttled = exc.kind == 'rate_limited'
            row.retryable = (not row.creation_attempted and not row.api_key
                             and exc.kind in ('rate_limited', 'service_unavailable'))
            if row.api_key:
                row.status = 'created_settings_unverified'
                row.note = 'Key saved, but verification failed. Do not create another key automatically.'
            elif row.status == 'creating' and (exc.status >= 500 or exc.status in (200, 201)):
                row.status = 'creation_unknown'
                row.note = 'Creation outcome is uncertain. Check the named key in ElevenLabs before retrying.'
            else:
                row.status = exc.kind
                reasons = {
                    'login_rejected': 'Sign-in rejected. Check the email/password and whether the account exists or is disabled.',
                    'rate_limited': 'Provider rate limit. Automatic waiting applies; success is not guaranteed.',
                    'service_unavailable': 'Temporary provider service failure.',
                    'verification_required': 'Provider requires verification (such as email verification, CAPTCHA, or 2FA). Resolve it directly with ElevenLabs.',
                    'protocol_changed': 'Provider rejected the request format or endpoint; the HTTP interface may have changed.',
                    'access_denied': 'Provider denied account/workspace access.'}
                row.note = f'{reasons.get(exc.kind, "Request rejected.")} HTTP {exc.status}; stage: {row.failure_stage}.'
        except asyncio.CancelledError:
            row.failure_stage = row.status
            if row.status == 'creating':
                row.status = 'creation_unknown'
                row.note = 'Process interrupted during creation. Check the named key before retrying.'
            elif not row.api_key:
                row.status = 'interrupted'
            raise
        except Exception as exc:
            row.failure_stage = row.status
            row.retryable = (not row.creation_attempted and not row.api_key
                             and isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError)))
            if row.api_key:
                row.status = 'created_settings_unverified'
                row.note = 'Key saved, but verification did not finish. Do not rerun automatically.'
            elif row.status == 'creating':
                row.status = 'creation_unknown'
                row.note = 'Creation connection failed. It may have succeeded. Check the named key before retrying.'
            else:
                row.status = 'service_unavailable' if row.retryable else 'protocol_changed'
                row.note = 'Connection or protocol failure before key creation; stage: ' + row.failure_stage + '.'
        finally:
            password = token = ''
