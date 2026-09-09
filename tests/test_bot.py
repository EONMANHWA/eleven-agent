import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from app import Bot, Settings, Session, create_app
from eleven_http import Row

OWNER = 12345
SECRET = 's' * 40


class FakeBot(Bot):
    def __init__(self):
        super().__init__(Settings('bot-token', SECRET, OWNER, 'public-config', 'https://example.test'), None)
        self.sent = []
        self.client.account = AsyncMock(side_effect=self.account)

    async def telegram(self, method, data=None, form=None):
        self.sent.append((method, data, form))
        if method in ('deleteMessage', 'answerCallbackQuery', 'setWebhook'):
            return True
        return {'message_id': len(self.sent)}

    async def account(self, row, password, stop):
        row.api_key = 'sk_' + 'x' * 40
        row.status = 'created_verified'
        row.full_access = 'yes'
        row.leak_auto_disable = 'off'


def message(n, text, owner=OWNER, private=True):
    return {'update_id': n, 'message': {'message_id': n, 'from': {'id': owner},
            'chat': {'id': owner, 'type': 'private' if private else 'group'}, 'text': text}}


def approval(n, nonce, owner=OWNER):
    return {'update_id': n, 'callback_query': {'id': str(n), 'from': {'id': owner},
            'message': {'chat': {'id': owner, 'type': 'private'}}, 'data': 'approve:' + nonce}}


async def staged(bot):
    await bot.handle(message(1, '/batch'))
    await bot.handle(message(2, 'synthetic-secret-password'))
    await bot.handle(message(3, 'owner@example.com'))
    await bot.handle(message(4, '/run'))


@pytest.mark.asyncio
async def test_complete_telegram_flow_and_no_password_echo():
    bot = FakeBot()
    await staged(bot)
    assert bot.session.mode == 'confirm' and bot.client.account.await_count == 0
    await bot.handle(approval(5, bot.session.nonce))
    await bot.worker
    assert bot.client.account.await_count == 1
    assert bot.session.password == '' and bot.session.mode == 'done'
    assert not any(method == 'sendDocument' for method, _, _ in bot.sent)
    assert any(method == 'sendMessage' and 'API key:' in data['text'] for method, data, _ in bot.sent)
    assert 'synthetic-secret-password' not in json.dumps([d for _, d, _ in bot.sent])
    assert {d['message_id'] for method, d, _ in bot.sent if method == 'deleteMessage'} >= {2, 3}


@pytest.mark.asyncio
async def test_duplicate_approval_never_starts_twice():
    bot = FakeBot()
    await staged(bot)
    nonce = bot.session.nonce
    await bot.handle(approval(5, nonce))
    await bot.handle(approval(5, nonce))
    await bot.worker
    await bot.handle(approval(6, nonce))
    assert bot.client.account.await_count == 1


@pytest.mark.asyncio
async def test_editing_email_list_invalidates_approval():
    bot = FakeBot()
    await staged(bot)
    nonce = bot.session.nonce
    await bot.handle(message(5, 'second@example.com'))
    await bot.handle(approval(6, nonce))
    assert bot.session.mode == 'emails' and bot.worker is None
    assert len(bot.session.emails) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('private,owner', [(False, OWNER), (True, 999), (False, 999)])
async def test_owner_private_only(private, owner):
    bot = FakeBot()
    await bot.handle(message(1, '/batch', owner, private))
    assert not bot.sent and bot.session.mode == 'idle'


@pytest.mark.asyncio
async def test_sender_must_also_match_not_just_chat():
    bot = FakeBot()
    update = message(1, '/batch')
    update['message']['from']['id'] = 999
    await bot.handle(update)
    assert not bot.sent


@pytest.mark.asyncio
async def test_cancel_before_approval_clears_password():
    bot = FakeBot()
    await staged(bot)
    nonce = bot.session.nonce
    await bot.handle(message(5, '/cancel'))
    await bot.handle(approval(6, nonce))
    assert bot.session.password == '' and bot.worker is None


@pytest.mark.asyncio
async def test_unknown_row_is_skipped_without_pause_or_retry():
    bot = FakeBot()
    bot.session = Session(mode='running', password='test', emails=['a@example.com', 'b@example.com'],
                          rows=[Row('a@example.com', 'one'), Row('b@example.com', 'two')])
    async def account(row, password, stop):
        row.status = 'creation_unknown' if row.name == 'one' else 'created_verified'
    bot.client.account = AsyncMock(side_effect=account)
    bot.worker = asyncio.create_task(bot.run())
    await bot.worker
    assert bot.session.mode == 'done' and bot.session.position == 2
    assert bot.client.account.await_count == 2
    assert [call.args[0].name for call in bot.client.account.await_args_list] == ['one', 'two']
    assert bot.session.password == ''


@pytest.mark.asyncio
async def test_password_with_command_prefix_is_supported():
    bot = FakeBot()
    await bot.handle(message(1, '/batch'))
    await bot.handle(message(2, '/password /cancel'))
    assert bot.session.password == '/cancel' and bot.session.mode == 'emails'


@pytest.mark.asyncio
async def test_duplicate_input_update_not_reinterpreted():
    bot = FakeBot()
    await bot.handle(message(1, '/batch'))
    update = message(2, 'synthetic-secret-password')
    await bot.handle(update)
    size = len(bot.sent)
    await bot.handle(update)
    assert len(bot.sent) == size and not bot.session.emails


@pytest.mark.asyncio
async def test_capacity_merge_is_atomic():
    bot = FakeBot()
    bot.settings.maximum = 1
    await bot.handle(message(1, '/batch'))
    await bot.handle(message(2, 'test'))
    await bot.handle(message(3, 'a@example.com'))
    await bot.handle(message(4, 'b@example.com'))
    assert bot.session.emails == ['a@example.com']


@pytest.mark.asyncio
async def test_webhook_secret_and_removed_panel():
    bot = FakeBot()
    async with TestClient(TestServer(create_app(bot))) as client:
        response = await client.post('/telegram', json=message(1, '/batch'))
        assert response.status == 403
        response = await client.post('/telegram', json=message(1, '/batch'),
                                     headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'})
        assert response.status == 403
        for path in ('/panel', '/api/session', '/api/screenshot', '/web/app.js'):
            response = await client.get(path)
            assert response.status == 404
        response = await client.get('/health')
        assert (await response.json())['browser'] is False
        response = await client.post('/telegram', json=message(1, '/batch'),
                                     headers={'X-Telegram-Bot-Api-Secret-Token': SECRET})
        assert response.status == 200
        await asyncio.gather(*list(bot.input_tasks))
        assert bot.session.mode == 'password'


@pytest.mark.asyncio
async def test_bad_json_rejected():
    bot = FakeBot()
    async with TestClient(TestServer(create_app(bot))) as client:
        response = await client.post('/telegram', data='not json', headers={'X-Telegram-Bot-Api-Secret-Token': SECRET})
        assert response.status == 400


@pytest.mark.asyncio
async def test_document_type_size_and_path_validation():
    bot = FakeBot()
    for doc in ({'file_name': 'emails.exe', 'file_size': 10}, {'file_name': 'emails.txt', 'file_size': 300001}):
        with pytest.raises(ValueError):
            await bot.email_document(doc)
    bot.telegram = AsyncMock(return_value={'file_path': '../secret'})
    with pytest.raises(ValueError):
        await bot.email_document({'file_name': 'emails.txt', 'file_size': 10})


@pytest.mark.asyncio
async def test_restart_invalidates_previous_approval():
    first = FakeBot()
    await staged(first)
    restarted = FakeBot()
    await restarted.handle(approval(50, first.session.nonce))
    assert restarted.client.account.await_count == 0
    assert restarted.session.password == ''


def test_settings_repr_does_not_expose_secret():
    settings = Settings('secret-bot-token', SECRET, OWNER, 'public-config', 'https://example.test')
    assert 'secret-bot-token' not in repr(settings)
    settings.validate()
    settings.owner = 0
    with pytest.raises(RuntimeError):
        settings.validate()


@pytest.mark.asyncio
async def test_100_account_worker_is_sequential_and_creates_once_each(monkeypatch):
    async def no_delay(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr('app.asyncio.wait_for', no_delay)
    bot = FakeBot()
    emails = [f'account{i}@example.com' for i in range(100)]
    bot.session = Session(mode='running', password='test', emails=emails,
                          rows=[Row(e, f'test-{i}') for i, e in enumerate(emails)])
    bot.worker = asyncio.create_task(bot.run())
    await bot.worker
    assert bot.client.account.await_count == 100
    assert len({call.args[0].name for call in bot.client.account.await_args_list}) == 100
    assert bot.session.position == 100 and bot.session.password == ''
    assert sum(method == 'sendDocument' for method, _, _ in bot.sent) == 0
    assert sum(method == 'sendMessage' and 'API key:' in data['text'] for method, data, _ in bot.sent) == 100


@pytest.mark.asyncio
async def test_rejected_logins_do_not_pause_batch(monkeypatch):
    async def no_delay(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr('app.asyncio.wait_for', no_delay)
    bot = FakeBot()
    bot.session = Session(mode='running', password='test', rows=[Row(f'a{i}@example.com', str(i)) for i in range(5)])
    async def rejected(row, password, stop):
        row.status = 'login_rejected'
    bot.client.account = AsyncMock(side_effect=rejected)
    bot.worker = asyncio.create_task(bot.run())
    await bot.worker
    assert bot.session.mode == 'done' and bot.session.position == 5
    await bot.handle(message(1, '/cancel'))
    assert bot.session.password == ''
    assert [r.status for r in bot.session.rows] == ['login_rejected'] * 5


@pytest.mark.asyncio
async def test_failed_text_delivery_keeps_keys_and_does_not_pause(monkeypatch):
    async def no_delay(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr('app.asyncio.wait_for', no_delay)
    bot = FakeBot()
    bot.session = Session(mode='running', password='test', rows=[Row(f'a{i}@example.com', str(i)) for i in range(12)])
    original = bot.telegram
    async def telegram(method, data=None, form=None):
        if method == 'sendMessage' and 'API key:' in data.get('text', ''):
            return None
        return await original(method, data, form)
    bot.telegram = telegram
    bot.worker = asyncio.create_task(bot.run())
    await bot.worker
    assert bot.session.mode == 'done' and bot.client.account.await_count == 12
    assert sum(bool(r.api_key) for r in bot.session.rows) == 12
    assert not any(r.text_delivered for r in bot.session.rows)
    bot.telegram = original
    await bot.export()
    assert all(r.text_delivered for r in bot.session.rows)
