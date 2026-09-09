import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from app import Session, Settings, Bot, create_app
from checkpoint import CheckpointStore, StorageError, LeaseLost
from eleven_http import Row
from test_bot import FakeBot, message
from test_http import prepared, creations, KEY


class MemoryStore:
    def __init__(self, db=None):
        self.db = db if db is not None else {}
        self.owned = False; self.version = 0; self.cancel_requested = False
    async def read(self):return copy.deepcopy(self.db.get('record'))
    async def claim(self):
        record = await self.read()
        if record:
            self.owned = True; self.version = record['version']; self.cancel_requested = record['cancel_requested']
        return record
    async def save(self, payload, state, due, expires, new=False):
        if self.version and not self.owned:raise LeaseLost('test')
        self.version += 1; self.owned = True
        self.db['record'] = {'payload':copy.deepcopy(payload),'state':state,'version':self.version,
                            'due_at':due,'expires_at':expires,'cancel_requested':self.cancel_requested}
    def decode(self, record):return copy.deepcopy(record['payload'])
    async def release(self):self.owned=False
    async def renew(self):pass
    async def request_cancel(self):
        self.cancel_requested=True
        if self.db.get('record'):self.db['record']['cancel_requested']=True
        return True
    async def forget(self):self.db.clear();self.version=0;self.owned=False


def durable_bot(store=None):
    bot = FakeBot();bot.store=store or MemoryStore();bot.storage_ready=True
    bot.automatic_wait=AsyncMock(return_value=True)
    return bot


def test_authenticated_encryption_and_wrong_key_rejection():
    key=Fernet.generate_key().decode()
    store=CheckpointStore(None,'https://example.supabase.co','service',key,123)
    payload={'password':'not-a-real-password','rows':[{'api_key':'synthetic-key'}]}
    encrypted=store.encode(payload,1)
    assert 'not-a-real-password' not in encrypted and 'synthetic-key' not in encrypted
    record={'payload':encrypted,'version':1}
    assert store.decode(record)==payload
    other=CheckpointStore(None,'https://example.supabase.co','service',Fernet.generate_key().decode(),123)
    with pytest.raises(StorageError):other.decode(record)
    with pytest.raises(StorageError):store.decode({'payload':encrypted,'version':2})
    wrong_owner=CheckpointStore(None,'https://example.supabase.co','service',key,999)
    with pytest.raises(StorageError):wrong_owner.decode(record)


@pytest.mark.asyncio
async def test_shutdown_preserves_queued_rows_and_encrypted_recovery_password():
    bot=durable_bot()
    bot.session=Session(mode='running',password='synthetic',rows=[Row('a@example.invalid','a'),Row('b@example.invalid','b')],batch_id='job',expires_at=time.time()+86400)
    await bot.begin_saved_job()
    bot.session.stop_reason='host_shutdown';bot.session.stop.set()
    await bot.finish_run()
    saved=bot.store.db['record']
    assert saved['state']=='pending' and saved['payload']['password']=='synthetic'
    assert [r['status'] for r in saved['payload']['rows']]==['queued','queued']
    assert bot.session.password==''
    assert not any('Status: cancelled' in (data or {}).get('text','') for _,data,_ in bot.sent)


@pytest.mark.asyncio
async def test_recovery_processes_only_unfinished_accounts():
    first=durable_bot()
    first.session=Session(mode='running',password='synthetic',rows=[Row('a@example.invalid','a',status='created_verified',api_key=KEY,creation_attempted=True,text_delivered=True),Row('b@example.invalid','b')],position=1,batch_id='job',expires_at=time.time()+86400)
    await first.begin_saved_job()
    first.session.stop_reason='host_shutdown';first.session.stop.set();await first.finish_run()
    recovered=durable_bot(MemoryStore(first.store.db))
    await recovered.recover();await recovered.worker
    assert recovered.client.account.await_count==1
    assert recovered.client.account.await_args.args[0].name=='b'
    assert recovered.session.position==2 and recovered.store.db['record']['state']=='done'
    assert recovered.store.db['record']['payload']['password']==''


@pytest.mark.asyncio
async def test_recovery_does_not_repeat_uncertain_creation():
    first=durable_bot()
    row=Row('a@example.invalid','a',status='creating',creation_attempted=True,attempts=1)
    first.session=Session(mode='running',password='synthetic',rows=[row,Row('b@example.invalid','b')],batch_id='job',expires_at=time.time()+86400)
    await first.begin_saved_job();await first.store.release()
    recovered=durable_bot(MemoryStore(first.store.db))
    # Native client guard must reject replay even when the worker revisits the row.
    from eleven_http import ElevenClient
    client=ElevenClient(None,'synthetic-config');client.request=AsyncMock(side_effect=AssertionError('No external request for uncertain row'))
    original=recovered.client.account
    async def account(row,password,stop):
        if row.name=='a':await client.account(row,password,stop)
        else:await original(row,password,stop)
    recovered.client.account=AsyncMock(side_effect=account)
    await recovered.recover();await recovered.worker
    assert recovered.session.rows[0].status=='creation_unknown'
    assert client.request.await_count==0
    assert recovered.session.rows[1].api_key


@pytest.mark.asyncio
async def test_cooldown_restores_remaining_time_not_entire_previous_delay():
    first=durable_bot()
    first.session=Session(mode='waiting',password='synthetic',rows=[Row('a@example.invalid','a')],batch_id='job',next_delay=1800,wake_at=time.time()+40,expires_at=time.time()+86400)
    await first.begin_saved_job();await first.store.release()
    recovered=durable_bot(MemoryStore(first.store.db))
    await recovered.recover();await recovered.worker
    delay=recovered.automatic_wait.await_args_list[0].args[0]
    assert 0 < delay <= 40


@pytest.mark.asyncio
async def test_manual_cancel_clears_saved_password_and_labels_only_user_cancelled():
    bot=durable_bot()
    bot.session=Session(mode='running',password='synthetic',rows=[Row('a@example.invalid','a')],batch_id='job',expires_at=time.time()+86400)
    await bot.begin_saved_job()
    bot.session.stop_reason='user_cancel';bot.session.stop.set()
    await bot.finish_run()
    saved=bot.store.db['record']
    assert saved['state']=='cancelled' and saved['payload']['password']==''
    assert saved['payload']['rows'][0]['status']=='cancelled'


@pytest.mark.asyncio
async def test_checkpoint_failure_before_creation_sends_no_mutation():
    client,calls,row=prepared()
    async def hook(phase,row):
        if phase=='before_create':raise StorageError('simulated unavailable storage')
    client.checkpoint=hook
    with pytest.raises(StorageError):await client.account(row,'synthetic',asyncio.Event())
    assert not creations(calls)


@pytest.mark.asyncio
async def test_key_is_checkpointed_immediately_before_verification_requests():
    client,calls,row=prepared();captured=[]
    async def hook(phase,row):
        captured.append((phase,row.api_key,len(calls)))
    client.checkpoint=hook
    await client.account(row,'synthetic',asyncio.Event())
    assert captured[0][0]=='attempt_started' and captured[0][2]==0
    assert next(item for item in captured if item[0]=='before_create')[2]==3
    received=next(item for item in captured if item[0]=='key_received')
    assert received[1]==KEY and received[2]==4


@pytest.mark.asyncio
async def test_scheduler_endpoint_requires_dedicated_secret():
    from aiohttp.test_utils import TestClient,TestServer
    bot=durable_bot();bot.settings.wakeup_secret='wake-secret'*5
    bot.schedule_recovery=lambda:None
    async with TestClient(TestServer(create_app(bot))) as client:
        response=await client.post('/jobs/tick');assert response.status==403
        response=await client.post('/jobs/tick',headers={'X-Job-Wakeup-Secret':bot.settings.wakeup_secret})
        assert response.status==200


@pytest.mark.asyncio
async def test_pending_checkpoint_cannot_be_overwritten_by_new_batch():
    bot=durable_bot()
    bot.session=Session(mode='running',password='synthetic',rows=[Row('a@example.invalid','a')],batch_id='first')
    await bot.begin_saved_job()
    other=durable_bot(MemoryStore(bot.store.db));other.session=Session(mode='running',password='other',rows=[],batch_id='second')
    with pytest.raises(StorageError):await other.begin_saved_job()
    assert bot.store.db['record']['payload']['batch_id']=='first'
