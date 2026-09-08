"""Bounded single-owner batch automation. No credential/key logging or persistence.
UI adapters are conservative: unknown controls pause instead of guessing.
"""
import asyncio
import contextlib
import csv
import io
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

LOGIN_URL = 'https://elevenlabs.io/app/sign-in'
KEYS_URL = 'https://elevenlabs.io/app/api/api-keys'
def bounded_setting(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be an integer.') from None
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}.')
    return value


MAX_ACCOUNTS = bounded_setting('MAX_BATCH_ACCOUNTS', 100, 1, 1000)
MAX_EMAIL_TEXT_CHARS = MAX_ACCOUNTS * 260
BATCH_SESSION_MINUTES = bounded_setting('BATCH_SESSION_MINUTES', 240, 20, 720)
BATCH_SESSION_SECONDS = BATCH_SESSION_MINUTES * 60
KEY_RE = re.compile(r'(?:sk_[A-Za-z0-9_-]{20,500}|[a-fA-F0-9]{32})\Z')
CREATE_RE = re.compile(r'^\+?\s*Create (?:API )?Key$', re.I)


class NeedsInput(Exception):
    """Only fixed, non-sensitive user-facing explanations belong here."""


def parse_emails(raw):
    if not isinstance(raw, str) or len(raw) > MAX_EMAIL_TEXT_CHARS:
        raise ValueError(f'Enter up to {MAX_ACCOUNTS} emails, one per line.')
    emails = [s.strip() for s in re.split(r'[\n,;]+', raw) if s.strip()]
    if not 1 <= len(emails) <= MAX_ACCOUNTS:
        raise ValueError(f'Enter between one and {MAX_ACCOUNTS} accounts per batch.')
    if any(len(s) > 254 or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', s) for s in emails):
        raise ValueError('One or more email addresses is invalid.')
    if len({s.casefold() for s in emails}) != len(emails):
        raise ValueError('Duplicate emails found. Each account should appear once.')
    return emails


@dataclass(repr=False)
class Row:
    email: str
    key_name: str
    phase: str = 'open'
    status: str = 'queued'
    message: str = ''
    api_key: str = field(default='', repr=False)
    login_attempted: bool = False
    submitted: bool = False
    identity: str = 'not_verified'
    settings: str = 'not_verified'
    permission_count: int = 0

    def public(self):
        return {k: getattr(self, k) for k in (
            'email', 'key_name', 'phase', 'status', 'message', 'submitted',
            'identity', 'settings', 'permission_count'
        )} | {'key_collected': bool(self.api_key)}


async def first_visible(locator):
    for i in range(await locator.count()):
        el = locator.nth(i)
        if await el.is_visible():
            return el
    return None


async def exact_button(root, pattern):
    target = await first_visible(root.get_by_role('button', name=pattern))
    if target is None:
        raise NeedsInput('Expected button not found. Inspect the screenshot; this page layout may need manual handling.')
    if not await target.is_enabled():
        raise NeedsInput('The expected button is disabled. Complete verification or review the form first.')
    return target


async def dialog(page):
    dialogs = page.locator('[role="dialog"], dialog[open]')
    visible = [dialogs.nth(i) for i in range(await dialogs.count()) if await dialogs.nth(i).is_visible()]
    if len(visible) != 1:
        raise NeedsInput('Could not identify exactly one key dialog. Open the Create Key form manually, then Resume.')
    return visible[0]


async def control_state(el):
    tag = await el.evaluate('(e) => e.tagName.toLowerCase()')
    if tag == 'input':
        return await el.is_checked()
    for name in ('aria-checked', 'aria-pressed'):
        value = await el.get_attribute(name)
        if value in ('true', 'false'):
            return value == 'true'
    value = await el.get_attribute('data-state')
    if value in ('on', 'off', 'checked', 'unchecked'):
        return value in ('on', 'checked')
    raise NeedsInput('A control has no readable selected state. Its settings cannot be verified automatically.')


async def set_control(el, wanted):
    if await control_state(el) != wanted:
        if not await el.is_enabled():
            raise NeedsInput('A required setting is disabled by the account or plan. The bot cannot grant unavailable access.')
        await el.click()
    if await control_state(el) != wanted:
        raise NeedsInput('A setting did not reach the requested state. Review it manually.')


async def labeled_toggle(root, pattern):
    for role in ('switch', 'checkbox'):
        target = await first_visible(root.get_by_role(role, name=pattern))
        if target is not None:
            return target
    # Strict nearby-label fallback for UI libraries missing accessible labels.
    texts = root.get_by_text(pattern)
    for i in range(await texts.count()):
        text = texts.nth(i)
        if not await text.is_visible():
            continue
        for depth in (1, 2):
            parent = text.locator('xpath=' + '/'.join(['..'] * depth))
            controls = parent.locator('[role="switch"], [role="checkbox"], input[type="checkbox"]')
            if await controls.count() == 1:
                return controls.first
    raise NeedsInput('A required labeled switch was not found. No unlabeled security setting will be guessed.')


# Identify independent segmented permission controls, not arbitrary page buttons.
# Names and readable states are required; disabled maximum-access choices pause.
MARK_GROUPS = r"""root => {
 const norm = s => (s || '').trim().replace(/\s+/g,' ').toLowerCase();
 const rank = s => ({'no access':0,'read':1,'access':2,'write':3,'read / write':3,
   'read/write':3,'read & write':3,'read and write':3})[norm(s)];
 const label = e => e.getAttribute('aria-label') || e.innerText || e.value || '';
 const selector = 'button, [role="radio"], input[type="radio"]';
 const options = [...root.querySelectorAll(selector)];
 const noAccess = options.filter(e => rank(label(e)) === 0);
 if (!noAccess.length) return {error:'no_groups'};
 const groups = new Set();
 let index = 0;
 for (const zero of noAccess) {
   let parent = zero.parentElement, found = null;
   for (let depth=0; parent && root.contains(parent) && depth<4; depth++,parent=parent.parentElement) {
     const all = [...parent.querySelectorAll(selector)];
     if (all.length>=2 && all.length<=4 && all.every(e => rank(label(e)) !== undefined) &&
         all.some(e => rank(label(e))>0) && all.filter(e=>rank(label(e))===0).length===1) {
       found = parent; break;
     }
   }
   if (!found) return {error:'unknown_group'};
   if (groups.has(found)) continue;
   groups.add(found);
   const all = [...found.querySelectorAll(selector)];
   const best = all.reduce((a,b)=>rank(label(a))>=rank(label(b))?a:b);
   found.setAttribute('data-batch-permission-group', String(index));
   best.setAttribute('data-batch-permission-best', String(index));
   index++;
 }
 return {count:index};
}"""


class ElevenUI:
    """Live DOM adapter. Tested against fixtures, not authenticated ElevenLabs."""
    def __init__(self, state):
        self.state = state

    @property
    def page(self):
        return self.state.page

    async def open(self):
        await self.state.open_browser(LOGIN_URL)

    async def login(self, row, password):
        p = self.page
        if urlparse(p.url).hostname != 'elevenlabs.io':
            raise NeedsInput('Remote page is not elevenlabs.io. Credentials were not submitted.')
        email = p.locator('input[type="email"]:visible').first
        try:
            await email.wait_for(state='visible', timeout=10000)
        except Exception:
            for label in (r'^(sign in|log in|continue) with email$', r'^use email$'):
                button = await first_visible(p.get_by_role('button', name=re.compile(label, re.I)))
                if button is not None:
                    await button.click()
                    break
        if not await email.count() or not await email.is_visible():
            raise NeedsInput('Email sign-in form not found. Select email sign-in below and Resume.')
        if row.login_attempted:
            # Never repeat a submitted password automatically.
            return
        await email.fill(row.email)
        pw = p.locator('input[type="password"]:visible').first
        if not await pw.count():
            # Some UIs ask for email first. This submits email only, once per step.
            await (await exact_button(p, re.compile(r'^(continue|next)$', re.I))).click()
            try:
                await pw.wait_for(state='visible', timeout=10000)
            except Exception:
                raise NeedsInput('Password field not shown. Check for CAPTCHA or an email verification step.')
        await pw.fill(password)
        submit = await exact_button(p, re.compile(r'^(sign in|log in|continue)$', re.I))
        row.login_attempted = True
        row.phase = 'auth'  # Set before the non-idempotent click.
        await submit.click()

    async def authenticated(self):
        p = self.page
        # Wait without resubmitting login or navigating away from a challenge.
        try:
            await p.wait_for_function("""() => location.hostname === 'elevenlabs.io' &&
              !/sign-in|sign-up|login|verify|verification|two-factor/.test(location.pathname) &&
              ![...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length) &&
              document.body.innerText.trim().length > 30""", timeout=20000)
        except Exception:
            raise NeedsInput('Sign-in is not confirmed. Solve CAPTCHA/verification or check the password below, then Resume. No password retry is automatic.')

    async def identity(self, email):
        p = self.page
        match = p.get_by_text(email, exact=True)
        if await first_visible(match) is not None:
            return True
        for label in (r'^(account|profile|user menu|account menu|profile menu)$', re.escape(email)):
            button = await first_visible(p.get_by_role('button', name=re.compile(label, re.I)))
            if button is not None:
                await button.click()
                visible = await first_visible(match) is not None
                await p.keyboard.press('Escape')
                if visible:
                    return True
        return False

    async def keys(self):
        if urlparse(self.page.url).hostname == 'elevenlabs.io' and 'api-keys' in urlparse(self.page.url).path:
            if await first_visible(self.page.get_by_role('button', name=CREATE_RE)) is not None:
                return
        await self.page.goto(KEYS_URL, wait_until='domcontentloaded', timeout=30000)
        try:
            await self.page.get_by_role('button', name=CREATE_RE).first.wait_for(state='visible', timeout=10000)
        except Exception:
            raise NeedsInput('API Keys page not recognized. Navigate to API Keys manually and Resume.')

    async def open_form(self):
        try:
            root = await dialog(self.page)
            if await root.get_by_text(re.compile(r'^Restrict Key$', re.I)).count():
                return
        except NeedsInput:
            pass
        await (await exact_button(self.page, CREATE_RE)).click()
        try:
            await self.page.locator('[role="dialog"], dialog[open]').last.wait_for(state='visible', timeout=7000)
        except Exception:
            raise NeedsInput('Create Key form did not open. Inspect the browser and Resume.')

    async def configure(self, row, disable_leak):
        root = await dialog(self.page)
        name = await first_visible(root.get_by_label(re.compile(r'^(name|key name|api key name)$', re.I)))
        if name is None:
            raise NeedsInput('The key-name field could not be identified safely.')
        await name.fill(row.key_name)
        # Keep restrictions ON, explicitly grant the highest option in every group.
        restrict = await labeled_toggle(root, re.compile(r'^Restrict Key$', re.I))
        await set_control(restrict, True)
        leak = await labeled_toggle(root, re.compile(r'(?=.*(?:leak|expos|compromis))(?=.*(?:revok|revoke|revocation|disable|deactivat)).*', re.I))
        # Never reverse a negatively worded security option by guessing polarity.
        leak_label = await leak.evaluate('(e) => e.getAttribute("aria-label") || e.innerText || e.closest("label")?.innerText || ""')
        if re.search(r"\b(?:never|not|don't|prevent)\b", leak_label, re.I):
            raise NeedsInput('The leaked-key setting is negatively worded; its polarity cannot be safely inferred.')
        await set_control(leak, not disable_leak)
        groups = await root.evaluate(MARK_GROUPS)
        if groups.get('error') or not 1 <= groups.get('count', 0) <= 100:
            raise NeedsInput('Could not enumerate all permission groups. Configure manually or stop; automatic creation is paused.')
        count = groups['count']
        for i in range(count):
            choice = root.locator(f'[data-batch-permission-best="{i}"]')
            await choice.scroll_into_view_if_needed()
            await set_control(choice, True)
        # Re-enumerate after scrolling to detect groups revealed later.
        after = await root.evaluate(MARK_GROUPS)
        if after.get('count') != count or after.get('error'):
            raise NeedsInput('Permission groups changed during configuration. Resume to re-check the complete list.')
        for i in range(count):
            if not await control_state(root.locator(f'[data-batch-permission-best="{i}"]')):
                raise NeedsInput('A maximum-access choice is not selected. Creation has not been submitted.')
        if not await control_state(restrict) or await control_state(leak) != (not disable_leak):
            raise NeedsInput('Final restriction or leaked-key setting check failed. Creation has not been submitted.')
        if await name.input_value() != row.key_name:
            raise NeedsInput('Key name changed unexpectedly. Review the form.')
        row.permission_count = count
        row.settings = 'pre_submit_ui_verified'
        await self.page.evaluate('navigator.clipboard.writeText("")')
        # Existing success dialogs are never blindly clicked as Create Key.
        submit = await exact_button(root, CREATE_RE)
        row.submitted = True
        row.phase = 'collect'  # Never retry Create, even after timeout/cancellation.
        await submit.click()

    async def collect(self):
        p = self.page
        # Only one current creation-result dialog, never an old key listing.
        try:
            await p.wait_for_function("""() => {
              const ds=[...document.querySelectorAll('[role=dialog],dialog[open]')].filter(e=>e.getClientRects().length);
              return ds.length===1 && /(?:copy|created|save|secret)/i.test(ds[0].innerText);
            }""", timeout=12000)
        except Exception:
            raise NeedsInput('A key may have been created, but its result dialog was not found. Inspect it below. The bot will NOT click Create again.')
        root = await dialog(p)
        candidates = set()
        fields = root.locator('input:not([type=password]), textarea')
        for i in range(await fields.count()):
            if await fields.nth(i).is_visible():
                value = (await fields.nth(i).input_value()).strip()
                if KEY_RE.fullmatch(value):
                    candidates.add(value)
        for value in re.findall(r'\bsk_[A-Za-z0-9_-]{20,500}\b', await root.inner_text()):
            candidates.add(value)
        if len(candidates) == 1:
            return candidates.pop()
        copy = await first_visible(root.get_by_role('button', name=re.compile(r'^(copy|copy key|copy api key|copy to clipboard)$', re.I)))
        if copy is not None:
            await p.evaluate('navigator.clipboard.writeText("")')
            await copy.click()
            value = (await p.evaluate('navigator.clipboard.readText()')).strip()
            await p.evaluate('navigator.clipboard.writeText("")')
            if KEY_RE.fullmatch(value):
                return value
        raise NeedsInput('The new key was not captured reliably. Use the remote Copy button, then Collect copied key. Creation is not retried.')


class Batch:
    def __init__(self, state, emails, password, disable_leak=False, driver=None):
        self.state = state
        self._password = password
        self.disable_leak = disable_leak
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        self.rows = [Row(email, f'personal-batch-{stamp}-{i+1}') for i, email in enumerate(emails)]
        self.index = 0
        self.mode = 'paused'
        self.message = 'Ready to start.'
        self.task = None
        self.driver = driver or ElevenUI(state)

    @property
    def current(self):
        return self.rows[self.index] if self.index < len(self.rows) else None

    @property
    def running(self):
        return self.mode == 'running'

    def public(self):
        return {'mode': self.mode, 'message': self.message, 'current_index': self.index,
                'disable_leak_revocation': self.disable_leak, 'rows': [r.public() for r in self.rows]}

    def launch(self):
        if self.task and not self.task.done():
            raise ValueError('Batch task is still stopping; try again.')
        if self.mode in ('finished', 'cancelled'):
            raise ValueError('This batch is finished. Download results and clear it before starting another.')
        self.mode, self.message = 'running', 'Processing accounts one at a time.'
        if self.current:
            self.current.status = 'running'
            self.current.message = ''
        self.task = asyncio.create_task(self.run())

    async def halt(self, clear_password=False):
        # Must be callable while the state lock is held. Task cleanup does not acquire it.
        self.mode = 'cancelled' if clear_password else 'paused'
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if clear_password:
            self._password = ''
        if self.current and self.current.status == 'running':
            self.current.status = 'paused' if not clear_password else ('cancelled_possible_key' if self.current.submitted else 'cancelled')

    async def run(self):
        while self.mode == 'running' and self.current:
            async with self.state.lock:
                if not self.state.session or time.monotonic() >= self.state.expires or time.monotonic()-self.state.last_action >= 600:
                    self.mode = 'cancelled'
                    self._password = ''
                    self.message = 'Session expired. No further account actions will run.'
                    return
                row = self.current
                try:
                    await self.step(row)
                    self.state.last_action = time.monotonic()
                except NeedsInput as exc:
                    self.mode, self.message = 'paused', str(exc)
                    row.status, row.message = 'needs_input', str(exc)
                    return
                except Exception:
                    # Never interpolate Playwright exception details: they can include credentials.
                    self.mode = 'paused'
                    self.message = ('An operation failed. Inspect the browser and Resume. If creation was submitted, it will not be submitted twice.')
                    row.status, row.message = 'needs_input', self.message
                    return
            await asyncio.sleep(0.2)
        if not self.current and self.mode == 'running':
            self.mode, self.message = 'finished', 'Batch finished. Download the private results now.'
            self._password = ''

    async def step(self, row):
        row.status = 'running'
        if row.phase == 'open':
            await self.driver.open()
            row.phase = 'login'
        elif row.phase == 'login':
            await self.driver.login(row, self._password)
            row.phase = 'auth'
        elif row.phase == 'auth':
            await self.driver.authenticated()
            row.phase = 'identity'
        elif row.phase == 'identity':
            if row.identity == 'not_verified':
                if not await self.driver.identity(row.email):
                    raise NeedsInput('Open the profile menu and verify the displayed email matches this row. Check the identity confirmation box below, then Resume.')
                row.identity = 'visible_email_verified'
            row.phase = 'keys'
        elif row.phase == 'keys':
            await self.driver.keys()
            row.phase = 'form'
        elif row.phase == 'form':
            await self.driver.open_form()
            row.phase = 'configure'
        elif row.phase == 'configure':
            if row.submitted:
                row.phase = 'collect'
            else:
                await self.driver.configure(row, self.disable_leak)
        elif row.phase == 'collect':
            row.api_key = await self.driver.collect()
            row.status = 'collected_ui_verified' if row.settings == 'pre_submit_ui_verified' else 'collected_settings_unverified'
            row.message = 'Key collected. UI settings were checked before submission; server-side permissions were not independently tested.' if row.settings == 'pre_submit_ui_verified' else 'Key collected manually; requested settings were not automatically verified.'
            row.phase = 'done'
            await self.state.dispose_browser()
            self.index += 1
        else:
            raise NeedsInput('Unknown workflow state; stop the batch and inspect your account.')

    def csv(self):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['email', 'api_key', 'key_name', 'status', 'identity_check', 'settings_check', 'permission_groups', 'leaked_key_auto_revocation_requested', 'creation_submitted', 'note'])
        def safe(value):
            text = str(value)
            return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else text
        for row in self.rows:
            writer.writerow([safe(v) for v in (row.email, row.api_key, row.key_name, row.status, row.identity, row.settings,
                row.permission_count, 'off' if self.disable_leak else 'on', row.submitted, row.message)])
        return output.getvalue()
