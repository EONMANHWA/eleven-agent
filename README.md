# ElevenLabs → Telegram (HTTP-only)

Owner-only Telegram bot for accounts you own or are authorized to manage. It signs in with one shared password, processes emails sequentially, creates one new API key per account, and sends each collected key as a private plain-text message.

**No Chromium, Playwright, browser gateway, screenshots, login links, or control panel.** Only Python and aiohttp are installed. Supabase is not required and no database is used. Keep the Render service on **Free**, with one instance and one Python process.

## Phone-only workflow

1. Open your Telegram bot and send `/batch`.
2. Send the shared password as the next plain-text message. If the password looks like a command, send `/password ` followed by the password instead.
3. **Forward your existing channel posts into the private bot chat**, or paste any text containing emails. Headings and surrounding sentences are fine. The bot extracts addresses from text, captions, and explicit email/URL link targets. You can also upload a UTF-8 `.txt` file (maximum 300 KB). Duplicate emails are removed. Default limit: **100** unique accounts.
4. Optionally use `/emails` to review the extracted list. Then send `/run` **once after all forwards arrive**, read the risk notice, and tap **Approve full access + leak auto-disable OFF**.
5. Keys arrive as **plain-text messages as they are collected**. The bot proceeds through the entire queue without asking for `/resume`. A final summary lists failures. `/results` resends saved keys/outcomes as text; `/csv` is an optional backup.

| Command | Action |
| --- | --- |
| `/batch` | Start password/email intake |
| `/run` | Review and explicitly approve creation/settings |
| `/status` | Current state, counts, and number of messages skipped without a readable address |
| `/emails` | Review all extracted email addresses before approval |
| `/results` | Send saved keys and account outcomes as plain text |
| `/csv` | Optional CSV download (not the default delivery format) |
| `/cancel` | Stop safely after an in-flight request, preserve results, clear password |
| `/forget` | Discard idle input/results from active RAM state; does not revoke keys |
| `/help` | Instructions and privacy notice |

`/login` is a compatibility alias for `/batch`; it no longer opens a browser.

### Forwarded posts and extraction

- **No channel admin access is needed for private forwards.** Only forwards/messages sent by the configured owner into the private bot chat are accepted. Telegram bots cannot fetch old channel history merely by being made an admin; forward existing posts yourself. This version does not subscribe to or process channel posts directly.
- Text such as “Your old email address has been successfully deleted / New temporary email address generated: / mailbox@example.com” is accepted. Surrounding prose is ignored.
- Forwarded posts with no readable address are quietly skipped and counted in `/status`; ordinary pasted text without an address gets a helpful notice, not the old strict-format rejection.
- Hidden `text_link` targets, captions, and inline-button URLs are inspected when Telegram supplies them. Encoded URL targets are decoded locally. External pages are **not** opened; a link that contains no address cannot reveal the address on its destination page. **Image-only text/OCR is not supported**—paste the address or supply it in a caption.
- All detected addresses are included, even ones in footers/signatures. Review `/emails` before approving and only submit accounts you own/control. No key creation begins merely because a message was forwarded.
- Forwarded text is data: a forwarded `/cancel`, `/run`, or other command cannot control the bot. A forwarded post also cannot become the shared password accidentally.
- Input-copy deletion affects the private bot chat, **not original channel posts**. Bulk-forward acknowledgements are throttled to avoid a reply for every post. All accepted messages are still processed sequentially; `/emails` and `/run` show the complete collection.
- Adding a new unique email invalidates an older approval button; repeated/duplicate or address-free forwards do not invalidate it.

## Automatic continuation, retries, and limits

- **Account-specific failures do not pause the batch.** Bad credentials, verification requirements, denied access, protocol errors, and uncertain creation outcomes are recorded; the next email is processed automatically. Three rejected logins no longer force a pause. There is no eight-minute manual-resume window.
- Rate limits cause an **automatic wait**, not a `/resume` prompt. The bot honors provider `Retry-After` seconds/HTTP dates. Without a usable hint, rate-limit waits start at 60 seconds and increase with consecutive throttling, up to 600 seconds. A longer provider hint is still honored. Temporary service failures use at least 15 seconds unless the provider requests longer.
- A transient rate limit, HTTP 503/other service failure, or network problem **before key creation** gets **one automatic retry** after waiting. If it still fails, the row is reported as failed and the queue continues after any necessary cooldown. A provider may block requests much longer; retries do not guarantee success.
- **Key creation is attempted at most once per email in an approved batch.** There are no fallback creation payloads or retries after creation has started—even if a response is uncertain or is a rejected creation request. The client also guards against a mistakenly repeated call on that row.
- A successful creation followed by verification failure retains and delivers the key as `created_settings_unverified`. A timeout/server failure during creation is `creation_unknown`; check the unique key name directly with ElevenLabs before a future batch. Neither outcome stops the other accounts or triggers another creation.
- Full/unrestricted access and effective leak auto-disable OFF are read back before claiming `created_verified`. Plan/workspace restrictions still apply. If the workspace forces leak auto-disable ON, its policy is not changed and no key is created for that account.
- CAPTCHA, MFA/2FA, and account email verification are **not bypassed**. These accounts require action directly with ElevenLabs and are skipped, not retried repeatedly. No solver, browser, proxy/IP rotation, or password guessing is used.
- Plain-text keys are delivered immediately. Already delivered keys are not automatically resent at every checkpoint. Failed text delivery is attempted again at checkpoints/finalization while saved keys remain in RAM; it does not recreate keys or pause the account queue. `/results` explicitly resends all saved results. Telegram flood waits are respected too. Optional `/csv` includes attempt counts and failed stages.
- `/cancel` interrupts an automatic cooldown or safely stops after an in-flight request. `/status` shows an automatic-wait countdown. `/resume` is only a compatibility notice and cannot restart a completed batch.

**Free-host limitation:** removing manual pauses does not make RAM durable. Render can sleep after inactivity or restart during a long cooldown/batch. The bot does not guarantee background completion through those events. Already delivered messages remain in Telegram, but undelivered results, queued work, and passwords can be lost. No paid upgrade, artificial keepalive, database, or durable resume was added. Save results and retry only failed/unprocessed accounts—not the ones that already returned keys.

## Privacy and data lifetime

Telegram bot chats are **not end-to-end encrypted**. Password and email messages are deleted on a best-effort basis after intake; this cannot erase every copy, notification, backup, provider record, or device cache. A deletion failure is reported. Plain-text results and any optional CSV remain in Telegram until you remove them.

The running app does not write passwords, Firebase tokens, or API keys to disk/database. It does not log request bodies, provider error bodies, Telegram URLs/tokens, passwords, or keys. Account refresh tokens are discarded. Passwords are removed from active state after completion/cancellation, or after 15 minutes of inactivity while staged and inactive. Idle results expire after one hour. These are application reference removals, **not guaranteed secure memory erasure**.

RAM is lost on Render sleep/restart/deployment. Keep delivered key messages and any optional CSV safe. A graceful shutdown tries to send partial results, but a hard kill cannot guarantee delivery. Stale approval buttons are rejected after restart; unknown creation outcomes must be reviewed manually. Supabase/durable resume is not implemented.

Full-access keys with leak auto-disable OFF are deliberately high-risk. Keep results private and revoke any exposed key manually. The bot requires explicit approval before applying these settings.

## Configuration

Set environment variables in Render, not GitHub:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`: random URL-safe value, at least 32 characters
- `ADMIN_CHAT_ID`: the owner's positive Telegram user/private-chat ID; both sender and chat must match
- `ELEVENLABS_FIREBASE_API_KEY`: the **public Firebase web-client project identifier** used by ElevenLabs' current production web app. This is not a personal ElevenLabs API key or an account password.
- `MAX_BATCH_ACCOUNTS`: defaults to 100; configurable from 1–1000
- `RENDER_EXTERNAL_URL`: supplied by Render; `PUBLIC_BASE_URL` can override it for other HTTPS hosting

Render exposes `/health` for health checks and `/telegram` for the authenticated webhook. `/` contains a plain Telegram-only notice. Former panel/API/screenshot paths no longer exist. Webhook registration and bot commands are configured automatically. Existing free-host sleep and monthly usage limits still apply; no paid service or second service is needed.

Old `LOW_MEMORY_MODE`, `BATCH_SESSION_MINUTES`, Supabase, and CAPTCHA-solver variables are unused by this version.

## Protocol provenance / compatibility

The HTTP flow mirrors the current public ElevenLabs frontend, inspected on September 9, 2026 (Asia/Kolkata):

- Email/password: Firebase Identity Toolkit `accounts:signInWithPassword`; email verification read via `accounts:lookup`.
- Workspace policy: authenticated `GET https://api.elevenlabs.io/v1/workspace`.
- Personal key creation: authenticated `POST /v1/user/create-api-key` with `name` and `third_party_disable_allowed=false` (unrestricted permissions omitted).
- Key verification: authenticated `GET /v1/user/api-keys`, plus a fresh workspace-policy read.

These personal dashboard endpoints are **undocumented web-client interfaces**, not a stable supported ElevenLabs public API contract. They may change or reject automation. Public sources: [sign-in client](https://elevenlabs.io/app/sign-in), [personal-key client](https://elevenlabs.io/app/api/api-keys), and [Firebase REST authentication documentation](https://firebase.google.com/docs/reference/rest/auth). Do not confuse this personal-account flow with the documented service-account key API, which needs existing workspace authorization.

An explicitly supplied owner account successfully completed HTTP sign-in, one key creation, settings verification, and CSV delivery to the owner Telegram chat during development (before plain-text delivery became the default). No browser was used. This does not establish that every account/IP will avoid verification challenges, nor that a 100-account production batch is already tested.

## Tests / local development

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q -o asyncio_mode=auto
python app.py
```

Tests use synthetic credentials and mocked account responses. They cover approval, owner-only access, webhook authentication, duplicate updates, limits, removal of panel routes, cancellation, automatic cooldowns, safe pre-creation retries, exact observed personal-key payload semantics, uncertain creations, key preservation, plain-text chunking/delivery, and optional CSV safety. Never place real passwords, private API keys, or management tokens in source/tests.
