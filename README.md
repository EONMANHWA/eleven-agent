# ElevenLabs → Telegram (HTTP-only)

Owner-only Telegram bot for accounts you own or are authorized to manage. It signs in with one shared password, processes emails sequentially, creates one new API key per account, and returns private CSV results.

**No Chromium, Playwright, browser gateway, screenshots, login links, or control panel.** Only Python and aiohttp are installed. Supabase is not required and no database is used. Keep the Render service on **Free**, with one instance and one Python process.

## Phone-only workflow

1. Open your Telegram bot and send `/batch`.
2. Send the shared password as the next plain-text message. If the password looks like a command, send `/password ` followed by the password instead.
3. Send emails one per line. You can use several messages or upload a UTF-8 `.txt` file (maximum 300 KB). Duplicate emails are removed. Default limit: **100** unique accounts.
4. Send `/run`, read the risk notice, then tap **Approve full access + leak auto-disable OFF**.
5. Download the CSV sent by the bot. Successful rows contain the new key; other rows explain their outcome. Checkpoints are sent after every ten processed accounts and a final/partial file at completion or pause.

| Command | Action |
| --- | --- |
| `/batch` | Start password/email intake |
| `/run` | Review and explicitly approve creation/settings |
| `/status` | Current state and counts, without revealing the password |
| `/results` | Download the current CSV again while it remains in RAM |
| `/resume` | After a pause, process the **next queued email**, never retry a failed/uncertain row |
| `/cancel` | Stop safely after an in-flight request, preserve results, clear password |
| `/forget` | Discard idle input/results from active RAM state; does not revoke keys |
| `/help` | Instructions and privacy notice |

`/login` is a compatibility alias for `/batch`; it no longer opens a browser.

## Exact behavior and limits

- One sign-in attempt and at most one key-creation attempt per email in an approved batch. There is no automatic key-creation retry, no fallback payload guessing, and no modification/deletion of existing keys.
- CAPTCHA, MFA/2FA, unverified email, rate limits, service problems, or an uncertain key-creation outcome pause processing. **No challenge solver, bypass, hidden browser, or IP rotation is used.** A pause can require you to resolve the issue directly with ElevenLabs. `/resume` skips already-attempted rows; it is not a retry.
- Three consecutive rejected sign-ins pause the run, since the shared password may be wrong.
- A work window is bounded to about eight minutes (checked between accounts). `/resume` starts the next window for queued accounts. This is not a guarantee against a free-host restart/sleep.
- Personal unrestricted keys use the current web client's semantics: omit `permissions` on creation. The bot requests `third_party_disable_allowed=false`, then reads back key metadata and current workspace policy. It reports `created_verified` only when unrestricted access and effective leak auto-disable OFF are confirmed. Account, workspace, and subscription limitations remain in force.
- If a workspace enforces leak auto-disable ON, no key is created for that account; its workspace policy is **not** changed.
- A successful creation followed by failed settings verification retains the key, marks `created_settings_unverified`, and pauses. A timeout/server failure during creation is `creation_unknown`: check the unique key name in ElevenLabs before any new batch. Creating the same email in a **new batch** can create another key.

## Privacy and data lifetime

Telegram bot chats are **not end-to-end encrypted**. Password and email messages are deleted on a best-effort basis after intake; this cannot erase every copy, notification, backup, provider record, or device cache. A deletion failure is reported. CSV results remain in Telegram until you remove them.

The running app does not write passwords, Firebase tokens, or API keys to disk/database. It does not log request bodies, provider error bodies, Telegram URLs/tokens, passwords, or keys. Account refresh tokens are discarded. Passwords are removed from active state after completion/cancellation, or after 15 minutes of inactivity while staged/paused. Idle results expire after one hour. These are application reference removals, **not guaranteed secure memory erasure**.

RAM is lost on Render sleep/restart/deployment. Save CSV files promptly. A graceful shutdown tries to send partial results, but a hard kill cannot guarantee delivery. Stale approval buttons are rejected after restart; unknown creation outcomes must be reviewed manually. Supabase/durable resume is not implemented.

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

An explicitly supplied owner account successfully completed HTTP sign-in, one key creation, settings verification, and CSV delivery to the owner Telegram chat during development. No browser was used. This does not establish that every account/IP will avoid verification challenges, nor that a 100-account production batch is already tested.

## Tests / local development

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q -o asyncio_mode=auto
python app.py
```

Tests use synthetic credentials and mocked account responses. They cover approval, owner-only access, webhook authentication, duplicate updates, limits, removal of panel routes, cancellation, pauses, exact observed personal-key payload semantics, uncertain creations, key preservation, and CSV safety. Never place real passwords, private API keys, or management tokens in source/tests.
