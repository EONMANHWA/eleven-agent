"""Browser-free account operations. Undocumented ElevenLabs web-client routes.
Never retry key creation, log provider bodies, or retain Firebase refresh tokens.
"""
import asyncio
import csv
import io
import re
from dataclasses import dataclass

import aiohttp

API = 'https://api.elevenlabs.io'
FIREBASE = 'https://identitytoolkit.googleapis.com/v1/accounts:'
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
KEY_RE = re.compile(r'[A-Za-z0-9_-]{20,256}\Z')
PAUSE_STATUSES = {'verification_required', 'rate_limited', 'service_unavailable',
                  'creation_unknown', 'created_settings_unverified', 'protocol_changed'}


@dataclass(repr=False)
class Row:
    email: str
    name: str
    status: str = 'queued'
    api_key: str = ''
    full_access: str = 'not verified'
    leak_auto_disable: str = 'not verified'
    note: str = ''


class ProviderError(Exception):
    def __init__(self, status, kind):
        self.status, self.kind = status, kind
        super().__init__(kind)


def parse_emails(text, maximum=100):
    if len(text) > 300000:
        raise ValueError('Email list is too large.')
    values = re.split(r'[\s,;]+', text.strip())
    result, seen = [], set()
    for email in values:
        if not email:
            continue
        if len(email) > 254 or not EMAIL_RE.fullmatch(email):
            raise ValueError('Use plain email addresses separated by lines, spaces, commas, or semicolons. No headings.')
        if email.casefold() not in seen:
            seen.add(email.casefold())
            result.append(email)
    if not result:
        raise ValueError('No email addresses found.')
    if len(result) > maximum:
        raise ValueError(f'Maximum {maximum} unique emails per batch.')
    return result


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['email', 'api_key', 'key_name', 'status', 'full_access', 'leak_auto_disable', 'note'])
    def safe(value):
        value = str(value)
        return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r', '\n') else value
    for row in rows:
        writer.writerow([safe(v) for v in (row.email, row.api_key, row.name, row.status,
                                         row.full_access, row.leak_auto_disable, row.note)])
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
                raise ProviderError(response.status, error_kind(response.status, data))
            return data

    async def account(self, row, password, stop):
        token = ''
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
            if row.api_key:
                row.status = 'created_settings_unverified'
                row.note = 'Key saved, but verification failed. Do not create another key automatically.'
            elif row.status == 'creating' and (exc.status >= 500 or exc.status in (200, 201)):
                row.status = 'creation_unknown'
                row.note = 'Creation outcome is uncertain. Check the named key in ElevenLabs before retrying.'
            else:
                row.status = exc.kind
                row.note = f'Provider rejected the request (HTTP {exc.status}). No automatic retry.'
        except asyncio.CancelledError:
            if row.status == 'creating':
                row.status = 'creation_unknown'
                row.note = 'Process interrupted during creation. Check the named key before retrying.'
            elif not row.api_key:
                row.status = 'interrupted'
            raise
        except Exception:
            if row.api_key:
                row.status = 'created_settings_unverified'
                row.note = 'Key saved, but verification did not finish. Do not rerun automatically.'
            elif row.status == 'creating':
                row.status = 'creation_unknown'
                row.note = 'Creation connection failed. It may have succeeded. Check the named key before retrying.'
            else:
                row.status = 'service_unavailable'
                row.note = 'Connection or protocol failure before key creation. No automatic retry.'
        finally:
            password = token = ''
