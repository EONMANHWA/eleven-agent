"""Encrypted, leased, compare-and-swap checkpoints. Management tokens are NOT used."""
import json
import time
import uuid
from datetime import datetime, timezone

import aiohttp
from cryptography.fernet import Fernet, InvalidToken


class StorageError(Exception):
    pass


class LeaseLost(StorageError):
    pass


class JobExpired(StorageError):
    pass


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class CheckpointStore:
    def __init__(self, http, url, key, encryption_key, owner):
        self.http, self.url, self.key, self.owner = http, url.rstrip('/'), key, owner
        self.cipher = Fernet(encryption_key.encode())
        self.token = str(uuid.uuid4())
        self.version = 0
        self.owned = False
        self.cancel_requested = False

    async def rpc(self, name, **body):
        try:
            async with self.http.post(self.url + '/rest/v1/rpc/bot_job_' + name,
                    json={'p_owner': self.owner, **body},
                    headers={'apikey': self.key, 'Authorization': 'Bearer ' + self.key,
                             'User-Agent': 'eleven-telegram-bot/1.0'},
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise StorageError('Checkpoint service unavailable.')
                data = bytearray()
                async for part in response.content.iter_chunked(65536):
                    data.extend(part)
                    if len(data) > 3000000:
                        raise StorageError('Checkpoint exceeds size limit.')
                return json.loads(data)
        except StorageError:
            raise
        except Exception:
            raise StorageError('Checkpoint connection failed.') from None

    def encode(self, payload, version):
        data = {'schema': 1, 'owner': self.owner, 'version': version, 'session': payload}
        return self.cipher.encrypt(json.dumps(data, separators=(',', ':')).encode()).decode()

    def decode(self, record):
        try:
            data = json.loads(self.cipher.decrypt(record['payload'].encode()))
            if data['schema'] != 1 or data['owner'] != self.owner or data['version'] != record['version']:
                raise ValueError()
            return data['session']
        except (ValueError, KeyError, InvalidToken, TypeError):
            raise StorageError('Checkpoint could not be authenticated. No work was replayed.') from None

    async def read(self):
        return await self.rpc('get')

    async def claim(self):
        record = await self.rpc('claim', p_token=self.token)
        if record:
            self.version = record['version']; self.owned = True
            self.cancel_requested = record['cancel_requested']
        return record

    async def save(self, payload, state, due, expires, new=False):
        # Initial insert is atomic. Existing rows require an owned, unexpired lease.
        if self.version and not self.owned:
            raise LeaseLost('Checkpoint lease is not owned.')
        encrypted = self.encode(payload, self.version + 1)
        record = await self.rpc('save', p_token=self.token, p_version=self.version, p_payload=encrypted,
                                p_state=state, p_due=iso(due), p_expires=iso(expires), p_new=new)
        if not record:
            self.owned = False
            raise LeaseLost('Checkpoint changed or its lease expired; stopped safely.')
        self.version = record['version']; self.owned = True
        self.cancel_requested = record['cancel_requested']

    async def renew(self):
        record = await self.rpc('renew', p_token=self.token)
        if not record:
            self.owned = False
            raise LeaseLost('Checkpoint lease expired.')
        self.cancel_requested = record['cancel_requested']

    async def release(self):
        if self.owned:
            await self.rpc('release', p_token=self.token)
        self.owned = False

    async def request_cancel(self):
        return await self.rpc('cancel')

    async def forget(self):
        record = await self.read()
        if record:
            if record['state'] == 'pending' or not await self.claim():
                raise StorageError('A saved job is still active. Cancel it first.')
            if not await self.rpc('forget', p_token=self.token):
                raise StorageError('Could not remove checkpoint.')
        self.version = 0; self.owned = False
