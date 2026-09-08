# Private ElevenLabs Telegram bot — batch edition

**Render + optional Supabase · One owner · 100 accounts by default · One shared password**

## New: automatic sequential batch workflow

Send `/batch` (or `/login`) to your Telegram bot and open the private web page. Enter **one email per line** and **the shared password once**. The bot attempts the following for each account, in order:

1. Destroy the previous account browser and start a fresh, isolated Chromium browser/context.
2. Fill the account email and shared password and submit sign-in once.
3. Wait for sign-in. Pause for CAPTCHA, email verification, 2FA, login errors, or an unrecognized page.
4. Check the signed-in email. If it cannot read the email reliably from the page/profile menu, pause for your explicit identity confirmation.
5. Open the API Keys page and create form, and assign a unique `personal-batch-…` key name.
6. Keep **Restrict Key ON**, then select the highest recognized permission option in **every detected permission group**: `Access`, `Write`, or `Read/Write`, as applicable. Existing credit-limit defaults are left unchanged.
7. Set the **automatic leaked-key revocation** control to your selected batch option, and check its state. The requested **OFF** option is preselected in the private form, but requires a separate risk acknowledgement before starting. Uncheck it to keep protection ON (recommended).
8. Re-check the name, permission groups, restriction switch and leak-protection switch. Submit **Create Key once** only if the controls can be verified.
9. Read the new key from its creation-result dialog or Copy control, keep it temporarily in server memory, close that browser, and proceed to the next account.
10. Offer a **private CSV download** with email, API key, key name, per-account status and verification notes. Passwords are never included.

This is a deployable source package, **not an already deployed service**. No real account was signed into and no real ElevenLabs key was created during development. There are no real credentials in the package.

### Important limits

- This is **best-effort automation**, not a guarantee of unattended completion. It uses visible browser UI controls, not a verified private ElevenLabs key-management API. Layout changes and unrecognized labels pause the queue instead of triggering guessed clicks.
- It supports up to **100 accounts per batch by default**, all sharing the password you supply. Duplicate emails are rejected. Passwords are submitted only on the `elevenlabs.io` origin.
- It cannot enable permissions disabled by an account plan or workspace administrator. It pauses rather than claiming every permission was enabled.
- Full-access keys may allow credit consumption and destructive operations. Disabling leaked-key revocation can leave an exposed key usable until manually revoked. **Keeping revocation ON and using least privilege is safer.** Use different passwords for your accounts when you can.
- Within a live session, the worker records the creation-submitted state **before clicking Create**. If the response is ambiguous, Resume tries collection, not another creation. This is not a durable cross-restart guarantee: after a restart, inspect the account for existing `personal-batch-…` keys before starting a fresh batch.
- A status of `collected_ui_verified` means the settings were checked **in the form before submission**. It is **not** an independent audit of saved server-side permissions, nor a paid-feature entitlement check. Manual recovery is marked `collected_settings_unverified`.
- These are **new keys**, not recovery of existing hidden keys. Creating them does not revoke your old keys.

### Upgrade an existing deployment

Replace the project files in your connected GitHub repository with this version, including the new **`batch.py`**, updated **`app.py`**, and **`web/`** files. Commit and redeploy Render. Existing bot-token, owner-ID and Supabase settings stay the same; no database migration is needed. Download any existing session results before redeploying. If you have not deployed yet, follow the Render setup below.

### Larger batch configuration

The default accepts **100 emails**, one shared password, and processes them **sequentially**, never in parallel. It does not create accounts or bypass account limits, billing, verification, or platform restrictions. Only use existing accounts you own or are authorized to administer.

You can configure these in Render → Environment, then redeploy:

| Variable | Default | Valid range | Meaning |
|---|---:|---:|---|
| `MAX_BATCH_ACCOUNTS` | `100` | `1`–`1000` | Maximum accepted email count. The panel reads this value from the server. |
| `BATCH_SESSION_MINUTES` | `240` | `20`–`720` | Absolute batch-session ceiling measured from opening the private session. |

The default is **not** a promise that 100 real accounts will complete within four hours. CAPTCHA and UI/identity checks may require many pauses. Increase the ceiling only if necessary: longer sessions keep sensitive access available longer.

The 10-minute inactivity limit remains: successful worker steps and manual control actions count as activity, but screenshots and status polling do not. The panel displays remaining absolute and idle time. **A completed or paused batch can expire after 10 minutes without an action, even with hours left on the absolute clock.** Download partial results regularly and save the final file promptly. There is no durable restart/resume or background notification when a batch finishes.

Passwords and keys are only held temporarily by the application. A restart, deployment, session expiration, `/stop`, or new session can discard results. Enabling the optional Supabase audit log does **not** make queues or keys durable. For long batches, a suitably sized paid Render instance is more reliable than relying on free-tier browser workloads; it still does not prevent all restarts or website blocks.

### Is Supabase compulsory?

**No.** Leave `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` unset and skip `supabase.sql`. Login, sequential processing, manual CAPTCHA controls and CSV export all work without Supabase. Its only feature in this project is an optional log of non-sensitive event names.

### Publishing to GitHub without sharing a PAT in chat

This package is source code, not a hosted deployment. A local Git repository can be prepared without credentials, but pushing to a remote GitHub repository requires your authorization.

**Do not paste a personal access token into chat or commit it into the project.** The simplest no-token-sharing path is:

1. Create a private GitHub repository in your browser.
2. Upload the extracted source files at its root (or use the Codespaces steps below).
3. Connect that repository in Render and deploy its Blueprint.

If Git authentication is securely configured in the workspace separately from chat, a remote push can be performed from there. No secure credential-entry mechanism is supplied by this project or this chat. Do not put a PAT in a repository URL, shell command, issue, screenshot or README. If you use a GitHub token in your own tooling, prefer a short-lived fine-grained token limited to the selected repository with only the repository-content permissions needed to push.

A GitHub PAT authorizes GitHub operations, **not Render deployment**. Render needs its own GitHub connection or separate Render authorization. Supabase credentials are not needed when its optional audit integration is disabled.

### How to use batch mode after deployment

1. Send `/batch` to your private bot and claim its one-use link.
2. Enter up to 100 emails and the shared password in the **private web form**, never in Telegram messages or this chat.
3. Check the full-access authorization box. For the requested leak-protection OFF setting, also check the separate risk acknowledgement. Click **Start sequential batch** once.
4. Watch the row statuses. You do not need to re-enter the password for each account.
5. If paused, inspect the live screenshot below. Finish any CAPTCHA/verification, then click **Resume**. If the identity prompt is shown, inspect the profile email and check the exact-account confirmation first. The bot does not automatically retry a rejected password.
6. **Pause** stops the runner after the current browser operation releases its lock. Manual controls cannot race a running batch. If a key-creation form cannot be handled automatically, either skip that account or finish the form manually; copy the new key using its Copy button **inside the remote screenshot**, then choose **Collect copied key & continue**. This records the settings as unverified.
7. Download **results CSV** while running, paused, or finished. Only keys already collected at download time are included. Save the final CSV before closing the session.
8. **Cancel batch** clears the shared password and closes the current browser but keeps previously collected results available. Cancelled batches cannot resume. **Clear batch**, `/stop`, a new `/login`/`/batch`, session expiry, or a Render restart discards the in-memory results. Download first.

**Skip and Cancel do not revoke keys.** A possibly created but not-yet-copied key may be lost when its browser closes. Inspect/copy it before skipping or cancelling. A skipped ambiguous submission is reported as `skipped_possible_key`, not as a clean failure.

The original single-account browser controls are still available when no batch exists. While a batch is paused, use the screenshot and typing controls for verification or recovery, not the separate single-account login form.

### CAPTCHA: the honest free option

- **Manual solving is free:** tap the CAPTCHA in the live screenshot.
- NopeCHA advertises a free allowance, but explicitly excludes **non-residential IP addresses**. Its free service is therefore not a suitable assumption for Render. See its [official documentation](https://developers.nopecha.com/#is-it-free).
- An optional **NopeCHA Recognition API** adapter is included for eligible API access. It handles **one static reCAPTCHA 3×3 or 4×4 image-grid round** and selects suggested squares. You review them and click Verify manually. Dynamic grids, Turnstile, hCaptcha, token injection, and phone verification are **not integrated**.
- The adapter is off unless `NOPECHA_API_KEY` is configured. It asks for consent before submitting only the CAPTCHA crop and instruction to the provider. A request may consume paid credits, even if the site rejects the answer.
- No unlimited free solver or trial-credit workaround is represented as a working feature.

---

## 1. Create your Telegram bot

1. Open the official **[@BotFather](https://t.me/BotFather)** in Telegram.
2. Send `/newbot` and follow its instructions.
3. Keep the token private. Add it to **Render environment variables**, not source code, Supabase, this chat, or messages to your new bot.

You need an accessible Telegram session. If Telegram itself requires a code on your broken phone, this project cannot recover that session.

## 2. Put the project on GitHub

Use a private repository you control. Put these at the repository root:

```text
app.py
batch.py
requirements.txt
Dockerfile
render.yaml
supabase.sql
web/
```

Upload **the extracted ZIP contents**, not just the ZIP. Include `.dockerignore` and `.gitignore` too. Never upload a filled `.env` or downloaded key.

### Browser-only alternative if you cannot extract the ZIP locally

Create a GitHub repository with a README, then use **Code → Codespaces → Create codespace** if your account has access. Codespaces may have usage limits or charges; it is not required to run the finished bot.

Upload `eleven-telegram-bot.zip` to the root in the Codespaces file explorer. In its browser terminal, run:

```bash
python -m zipfile -e eleven-telegram-bot.zip extracted
cp -a extracted/eleven-telegram-bot/. .
rm -rf extracted eleven-telegram-bot.zip
git add app.py batch.py requirements.txt Dockerfile render.yaml supabase.sql web tests README.md .gitignore .dockerignore .env.example
git commit -m "Add private ElevenLabs Telegram browser bot"
git push
```

These commands assume the uploaded ZIP is at the repository root and named exactly as above. If browser-only development is unavailable on your device, you will need access to another browser-capable device to complete deployment.

## 3. Deploy to Render

### Recommended: Blueprint

1. Open the [Render dashboard](https://dashboard.render.com/).
2. Choose **New → Blueprint** and connect your GitHub repository.
3. Render reads `render.yaml`. Enter `TELEGRAM_BOT_TOKEN` when prompted.
4. The blueprint generates `TELEGRAM_WEBHOOK_SECRET` and starts with `ADMIN_CHAT_ID=0`. No one can open a browser until you set the owner ID.
5. Deploy. Chromium and its operating-system dependencies are installed by the Dockerfile, so the first build can take several minutes.
6. Open the service URL in your browser. You should see the private-browser landing page. `/health` should return `{"ok":true}`.
7. Check Render logs for **Telegram webhook registered**.

The app uses Render’s `RENDER_EXTERNAL_URL` automatically. You do **not** need to register the webhook manually or expose your bot token in a browser URL.

### Alternative: manual Web Service

Choose **New → Web Service**, connect the repository, select **Docker**, and set health check path `/health`. Add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | A unique random 32–256 character URL-safe secret |
| `ADMIN_CHAT_ID` | `0` initially |

Generate the webhook secret in a password manager, or in a private Codespaces terminal with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Do not reuse a secret printed in an example or send it to this chat. The Blueprint method generates it for you.

### Render free-plan limitations

Render documents that free web services sleep after 15 minutes without inbound traffic and lose local filesystem changes on restart/spin-down. Waking can take about a minute. [Official Render documentation](https://render.com/docs/free)

This project uses a **Telegram webhook**, not background long-polling. An inbound webhook can wake the service, but a cold start may delay delivery or cause a retry. If the bot is slow, open the Render service URL, wait for it to load, then send `/login` again.

Chromium can exhaust a small free instance’s memory. **Free hosting is not guaranteed to be sufficient.** If the browser crashes or Render reports out-of-memory restarts, use a larger instance or create the key directly in a normal browser.

Run **one instance and one Python worker only**. Browser sessions and link tokens are intentionally ephemeral. Do not scale horizontally. Do not add an artificial keep-alive service to evade the free tier’s sleep behavior.

## 4. Lock the bot to your Telegram account

1. Send `/id` to **your new bot**, in a private chat.
2. It returns your private chat ID. This command works while `ADMIN_CHAT_ID=0`.
3. Copy that number into Render → your service → **Environment → `ADMIN_CHAT_ID`**.
4. Save and redeploy.
5. Send `/login` after the deployment is ready.

Only that private chat can launch or stop a browser or batch. Group chats are ignored. Other private chats can retrieve only their own ID.

| Command | Result |
|---|---|
| `/start`, `/login` or `/batch` | Clear the old session/results and issue a new single-use link |
| `/stop` | Close the browser and revoke pending/in-memory access |
| `/id` | Show the caller’s private chat ID |

## 5. Connect Supabase (optional audit events)

Supabase is **not needed for login or browser sessions**. Its only role here is persisting non-sensitive event names.

1. Create your own Supabase project.
2. Open its SQL editor and run `supabase.sql`.
3. In Render, add:
   - `SUPABASE_URL`: your Supabase project HTTPS URL.
   - `SUPABASE_SERVICE_ROLE_KEY`: the project’s legacy **service_role** JWT key, from the API key settings. This implementation expects that server-side key, not an anon/publishable key.
4. Save and redeploy.

The table stores only an event ID, timestamp, and one of `session_link_created`, `browser_opened`, or `browser_closed`. It is a best-effort activity log, not a complete security audit; expiry, crashes and restarts may not produce a closing event.

RLS is enabled with no public-client policies. The service-role key bypasses RLS and must remain server-side. Do not put it into the HTML, JavaScript, Telegram, or GitHub. If you do not want this server to hold a broad Supabase credential, leave the integration disabled. Use a dedicated Supabase project if enabling it.

**No passwords, API keys, cookies, email addresses, chat IDs, or session links are written to Supabase.** If audit writes fail, the browser tool continues to work.

## 6. Single-account mode: sign in and create one key manually

1. Send `/login` and open its private link within **5 minutes**. Do not forward it; possession grants access. The link works once.
2. Click **Open private browser**. If Telegram’s in-app browser fails, open the original link in your regular browser before claiming it.
3. Review the screenshot and remote URL. Credentials should be entered only while the remote page is on `elevenlabs.io`.
4. If necessary, tap the email sign-in option in the screenshot. Use **Attempt email sign-in** on the private control page. The server forwards these credentials to its ElevenLabs browser—not to Telegram.
5. Complete CAPTCHA or email verification in the live screenshot. To type into a field, tap it, enter text in **Text for the focused remote field**, and press **Send text**. Use Tab, Enter and scrolling controls as needed.
6. Click **Open API Keys**, or navigate manually to **Developers → API Keys** if the direct route has changed.
7. Click **Create Key** in the screenshot. Name it, choose minimum required permissions, and optionally set a credit limit. **Review and confirm creation yourself.**
8. Click the new key’s **Copy** button inside the screenshot.
9. Click **Read remote clipboard** on the control page. Inspect the result and use **Download as .env** or **Copy on this device**. The clipboard is not automatically verified as an ElevenLabs API key.
10. Click **Close session & clear local display**, then remove unneeded link messages from Telegram and clear the device clipboard.

ElevenLabs says the full API key is visible only when first created. Save it before leaving the creation screen. [Official ElevenLabs instructions](https://help.elevenlabs.io/hc/en-us/articles/14599447207697-How-do-I-authorize-myself-using-an-API-key)

Downloaded `.env` files are **plaintext secrets**. Do not send them to this chat or commit them to GitHub. Closing the browser does **not** revoke a created API key; revoke unwanted keys in ElevenLabs.

## Security and operational boundaries

- This is a single-owner personal tool, **not a production-hardened multi-user login service**.
- The private panel is clearly labeled as self-hosted, not official ElevenLabs. Render processes credential input and browser state, so use only hosting you control and trust. HTTPS is transport encryption, not end-to-end encrypted remote computing.
- Telegram bot chats are not end-to-end encrypted. They contain a short-lived access link, not account credentials or the resulting API key. Someone who steals that link before use can claim the session.
- Ticket and session tokens are kept as hashes in application memory. The session bearer token stays in the panel’s JavaScript memory, not localStorage. Reloading the panel requires a new `/login` link.
- Single-account sessions have a 20-minute maximum. Starting a batch extends the fixed maximum to **240 minutes (4 hours) from opening the private session** by default; starting another batch in the same panel does not reset that deadline. The **10-minute inactivity limit still applies**, including while paused or after completion. Successful batch steps count as activity; automatic screenshots/status polls do not. `/login` or `/batch` invalidates the previous session and discards its batch/results.
- The app does not deliberately persist browser profiles or screenshots, and closes the temporary Chromium profile on normal shutdown. Chromium may use temporary server files; this is **not a guarantee of RAM-only processing or forensic erasure**. Abrupt crashes may leave temporary files until Render replaces the filesystem.
- Access logs and detailed exception messages are disabled in this app to avoid leaking secrets. Hosting infrastructure can still collect its own metadata. Do not enable browser tracing, request-body logging, analytics, or session recording.
- The panel uses same-origin requests, authorization headers, explicit POST-origin checks, no external scripts, and anti-framing headers. Open it as a top-level page; embedding is intentionally blocked.
- Chromium runs as a non-root container user but with `--no-sandbox` for hosting compatibility. Basic private-IP URL blocking is **not a complete network sandbox or DNS-rebinding defense**. Do not host this container alongside sensitive internal services or unrelated secrets. Keep dependencies patched.
- No automatic password resubmission or repeated Create submission is made after the submission flag is set. Resume may repeat non-submitting page reads or idempotent configuration checks. Stop if ElevenLabs blocks access; do not repeatedly hammer the sign-in page.
- Use your own account and review ElevenLabs’ applicable terms. This tool cannot recover inaccessible email, phone verification, or two-factor authentication, and is not intended for bypassing account, billing, or service usage limits.

## Troubleshooting

| Symptom | Action |
|---|---|
| Bot doesn’t answer | Open the Render URL to wake it; check deployment and webhook registration logs. Verify bot token and public URL. |
| `/id` works, `/login` doesn’t | Set the exact private chat ID in `ADMIN_CHAT_ID`, then redeploy. |
| Link expired/already used | Send `/login` for a new one. Don’t refresh a claimed panel or open it in two browsers. |
| Browser out of memory | Use a larger Render instance. Supabase does not reduce Chromium memory needs. |
| Email fields not found | Select the email sign-in option in the screenshot, or use the manual typing controls. |
| CAPTCHA remains blocked | Use manual controls if offered. Cloud/browser reputation checks may be impossible to complete here; use a normal browser or account support. |
| New tab or provider popup is required | This version controls one tab. Use the email/password path; popup and SSO workflows are not implemented. |
| API Keys URL doesn’t work | Use the visible sidebar to navigate to Developers → API Keys. |
| Copy button gives an empty clipboard | Click the actual new-key Copy control in the remote browser first. If unsupported, show and transcribe the new key carefully without posting it to chat. |
| Phone verification required | Use your existing recovery options or contact ElevenLabs support; a CAPTCHA service cannot replace the factor. |
| Session disappears on restart | Expected: sessions are deliberately not persisted in Supabase. Start again with `/login`. |

## Tests and verification

The current suite has **28 passing tests**. Added checks accept 100 emails through both validation and the HTTP endpoint, reject 101 under the default configuration, exercise maximum-length addresses, validate configurable bounds, and check that the extended session deadline cannot be reset by starting another batch. Checks cover Python/JavaScript syntax, webhook authentication, owner allowlisting, duplicate updates, link replay protection, expiry/revocation, origin checks, bounded email lists, explicit risk consents, password cleanup, secret-free status responses, CSV formula-injection protection, identity pauses, and no duplicate Create after an ambiguous submission. Real Chromium tests exercise login, both permission choices, the leak-protection switch, key capture, and two fresh isolated account browsers against intercepted fixture pages. Frontend tests cover entering one shared password, acknowledgement requirements, clearing credential fields and downloading results. These fixtures are not the authenticated ElevenLabs site.

Run in a development environment:

```bash
pip install -r requirements.txt pytest pytest-asyncio pytest-aiohttp
playwright install --with-deps chromium
MAX_BATCH_ACCOUNTS=100 BATCH_SESSION_MINUTES=240 python -m pytest -q -o asyncio_mode=auto
node --check web/app.js
```

**Not verified:** a Render deployment, a live Telegram bot token, your Supabase project, authenticated ElevenLabs navigation or key creation, or paid/free solver acceptance. No real credentials are included in this package.

### Reference documentation

- [ElevenLabs API key instructions](https://help.elevenlabs.io/hc/en-us/articles/14599447207697-How-do-I-authorize-myself-using-an-API-key)
- [Render free-service behavior](https://render.com/docs/free)
- [NopeCHA free-access restrictions](https://developers.nopecha.com/#is-it-free)
- [NopeCHA reCAPTCHA Recognition API](https://developers.nopecha.com/recognition/recaptcha/)

Package prepared 8 September 2026. Service interfaces and plan terms may change.
