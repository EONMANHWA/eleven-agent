import json
import pytest
from eleven_http import extract_emails, message_email_text
from test_bot import FakeBot, message, approval


@pytest.mark.parametrize('text,expected', [
    ('Your old email address has been successfully deleted\n\nNew temporary email address generated:\n\n newbox@example.com', ['newbox@example.com']),
    ('Your old email address has been successfully deleted\n\nNew temporary email address generated:\n\n .', []),
    ('Email: first@example.com. Next: (second@example.org), done!', ['first@example.com', 'second@example.org']),
    ('**first@example.com**; `second@example.org`; __third@example.net__', ['first@example.com', 'second@example.org', 'third@example.net']),
    ("'first@example.com' and o'connor+tag@example.org", ['first@example.com', "o'connor+tag@example.org"]),
    ('A@example.com appears again as a@example.com', ['A@example.com']),
    ('Contact <first@example.com>\nName <second@sub.example.org>', ['first@example.com', 'second@sub.example.org']),
    ('Invalid foo..bar@example.com and @example.com and foo@example.com_bad', []),
    ('No addresses, just headings and status text.', []),
    ('plus+tag@sub.example.co.uk and percent%tag@example.com', ['plus+tag@sub.example.co.uk', 'percent%tag@example.com']),
    ('HTML: &lt;first@example.com&gt;', ['first@example.com']),
])
def test_prose_extraction(text, expected):
    assert extract_emails(text) == expected


def test_hidden_mailto_entities_captions_and_buttons():
    msg = {'text': 'Your new mailbox: click here', 'caption': 'Another: second@example.org',
           'entities': [{'type': 'text_link', 'url': 'mailto:first%40example.com'}],
           'caption_entities': [{'type': 'text_link', 'url': 'mailto:third@example.net'}],
           'reply_markup': {'inline_keyboard': [[{'text': 'Email', 'url': 'mailto:fourth@example.com'}]]}}
    assert extract_emails(message_email_text(msg)) == ['second@example.org', 'first@example.com', 'third@example.net', 'fourth@example.com']


def test_limits_apply_to_unique_extracted_addresses_not_words():
    text = 'Here are many ordinary words before the generated email a@example.com. Duplicate a@example.com.'
    assert extract_emails(text, 1) == ['a@example.com']
    with pytest.raises(ValueError):
        extract_emails(text + ' Another b@example.com.', 1)
    with pytest.raises(ValueError):
        extract_emails('x' * 300001)


def forwarded(n, text):
    update = message(n, text)
    update['message']['forward_origin'] = {'type': 'channel', 'chat': {'id': -10099, 'type': 'channel'}, 'message_id': 20, 'date': 1}
    return update


async def ready(bot):
    await bot.handle(message(1, '/batch'))
    await bot.handle(message(2, 'synthetic-password'))


@pytest.mark.asyncio
async def test_forwarded_commands_are_data_not_instructions():
    bot = FakeBot()
    await ready(bot)
    await bot.handle(forwarded(3, '/cancel\nGenerated mailbox: owner@example.com'))
    assert bot.session.mode == 'emails'
    assert bot.session.password == 'synthetic-password'
    assert bot.session.emails == ['owner@example.com']
    assert bot.client.account.await_count == 0


@pytest.mark.asyncio
async def test_forwarded_message_not_mistaken_for_shared_password():
    bot = FakeBot()
    await bot.handle(message(1, '/batch'))
    await bot.handle(forwarded(2, 'Mailbox: owner@example.com'))
    assert bot.session.password == '' and bot.session.mode == 'password'


@pytest.mark.asyncio
async def test_caption_and_hidden_link_intake():
    bot = FakeBot()
    await ready(bot)
    update = message(3, '')
    update['message'].update({'caption': 'Mailbox generated: click here', 'photo': [{'file_id': 'unused'}],
                              'caption_entities': [{'type': 'text_link', 'url': 'mailto:owner%40example.com'}]})
    await bot.handle(update)
    assert bot.session.emails == ['owner@example.com']


@pytest.mark.asyncio
async def test_non_text_document_caption_is_still_extracted_without_download():
    bot = FakeBot()
    await ready(bot)
    update = message(3, '')
    update['message'].update({'document': {'file_name': 'picture.png', 'file_size': 40}, 'caption': 'Mailbox: owner@example.com'})
    await bot.handle(update)
    assert bot.session.emails == ['owner@example.com']
    assert not any(method == 'getFile' for method, _, _ in bot.sent)


@pytest.mark.asyncio
async def test_no_address_forward_is_quietly_skipped():
    bot = FakeBot()
    await ready(bot)
    before = sum(method == 'sendMessage' for method, _, _ in bot.sent)
    await bot.handle(forwarded(3, 'New temporary email address generated:\n\n .'))
    after = sum(method == 'sendMessage' for method, _, _ in bot.sent)
    assert not bot.session.emails and bot.session.skipped_messages == 1
    assert before == after


@pytest.mark.asyncio
async def test_duplicate_forward_does_not_invalidate_approval():
    bot = FakeBot()
    await ready(bot)
    await bot.handle(forwarded(3, 'Created: owner@example.com'))
    await bot.handle(message(4, '/run'))
    nonce = bot.session.nonce
    await bot.handle(forwarded(5, 'Again owner@example.com'))
    assert bot.session.nonce == nonce and bot.session.mode == 'confirm'
    await bot.handle(approval(6, nonce))
    await bot.worker
    assert bot.client.account.await_count == 1


@pytest.mark.asyncio
async def test_100_forwarded_prose_posts_collected_before_one_approval():
    bot = FakeBot()
    await ready(bot)
    for index in range(100):
        await bot.handle(forwarded(3 + index, f'Your old email address has been successfully deleted\nNew temporary email address generated:\n\n box{index}@example.com'))
    assert len(bot.session.emails) == 100 and bot.client.account.await_count == 0
    assert sum(method == 'sendMessage' for method, _, _ in bot.sent) < 10
    await bot.handle(message(200, '/run'))
    assert bot.session.mode == 'confirm'
    assert '100 unique accounts' in json.dumps([data for _, data, _ in bot.sent])


@pytest.mark.asyncio
async def test_review_lists_extracted_addresses():
    bot = FakeBot()
    await ready(bot)
    await bot.handle(forwarded(3, 'New mailbox: owner@example.com'))
    await bot.handle(message(4, '/emails'))
    assert '1. owner@example.com' in bot.sent[-1][1]['text']


def test_encoded_url_in_plain_text_and_literal_percent_mailbox():
    text = message_email_text({'text': 'Your mailbox: https://example.org/mail/owner%40example.com\nOther: literal%40tag@example.org'})
    assert extract_emails(text) == ['owner@example.com', 'literal%40tag@example.org']


@pytest.mark.parametrize('text,expected', [
    ('https://example.org/?email=owner%40example.com', ['owner@example.com']),
    ('mailto:owner@example.com?cc=second%40example.org&subject=Hello', ['owner@example.com', 'second@example.org']),
    ('https://example.org/box/owner@example.com', ['owner@example.com']),
    ('https://username@example.com', []),
])
def test_email_url_components_do_not_include_url_prefixes(text, expected):
    assert extract_emails(text) == expected
