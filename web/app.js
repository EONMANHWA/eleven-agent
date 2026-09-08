'use strict';
const $ = id => document.getElementById(id);
let ticket = location.hash.slice(1), token = '', busy = false, imageBusy = false, objectUrl = '', timer;
let batchSnapshot = null, batchBusy = false, batchPolling = false;
history.replaceState(null, '', location.pathname); // Never keep the capability in history or request URLs.
$('connect').disabled = !ticket;
const status = text => { $('status').textContent = text; };
function clearValue() { $('key').value = ''; $('keybox').hidden = true; }
function finish() {
  token = ''; ticket = ''; clearInterval(timer); clearValue();
  for (const id of ['password', 'email', 'text', 'batchPassword', 'batchEmails']) $(id).value = '';
  batchSnapshot = null; renderBatch({mode:'none',rows:[]});
  $('screen').removeAttribute('src');
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  $('workspace').hidden = true; $('welcome').hidden = false; $('connect').disabled = true;
}
async function api(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json', ...(token ? {'Authorization':'Bearer '+token} : {})}, body:JSON.stringify(body), cache:'no-store'});
  if (r.status === 401) { finish(); throw Error('Session or link expired. Send /login to the bot again.'); }
  const text = await r.text(); let data;
  try { data = JSON.parse(text); } catch { throw Error('Server unavailable or waking up. Wait, then get a fresh /login link.'); }
  if (!r.ok) throw Error(data.error || 'Request failed.');
  return data;
}
async function refresh() {
  if (!token || imageBusy || busy) return;
  imageBusy = true;
  try {
    const r = await fetch('/api/screenshot', {headers:{'Authorization':'Bearer '+token}, cache:'no-store'});
    if (r.status === 401) { finish(); throw Error('Session expired. Send /login again.'); }
    if (r.status === 204) { $('screen').removeAttribute('src'); return; }
    if (!r.ok) throw Error('Screenshot unavailable. Retry Refresh; if necessary, use /login again.');
    const blob = await r.blob(); if (!token) return;
    const next = URL.createObjectURL(blob); const old = objectUrl;
    $('screen').src = next; objectUrl = next;
    if (old) URL.revokeObjectURL(old);
  } catch(e) { status(e.message); } finally { imageBusy = false; }
}
async function run(body) {
  if (busy || !token) return;
  if (batchSnapshot?.mode === 'running' && body.op !== 'close') { status('Pause the batch before using manual browser controls.'); return; }
  busy = true; status('Working…');
  try {
    const data = await api('/api/action', body);
    if (data.closed) { finish(); status('Session closed. Revoke unwanted keys in ElevenLabs separately.'); return; }
    if (data.url) $('remoteUrl').textContent = data.url;
    if ('clipboard' in data) { $('key').value = data.clipboard; $('keybox').hidden = false; }
    status(data.message || 'Done.');
  } catch(e) { status(e.message); }
  finally { busy = false; await refresh(); }
}
$('connect').onclick = async () => {
  if (!ticket || busy) return; busy = true; $('connect').disabled = true;
  status('Starting the private browser. This may take a minute…');
  try {
    const data = await api('/api/claim', {ticket}); ticket = ''; token = data.token;
    $('welcome').hidden = true; $('workspace').hidden = false;
    const maxAccounts = data.max_accounts || 100;
    $('batchLimitSummary').textContent = 'Up to '+maxAccounts+' accounts.';
    $('batchLimitLabel').textContent = 'maximum '+maxAccounts;
    $('batchEmails').maxLength = data.max_email_chars || 26000;
    $('sessionLimits').textContent = 'Single-account sessions last 20 minutes. Starting a batch extends the maximum window to '+(data.batch_session_minutes || 240)+' minutes from opening this session.';
    $('solver').disabled = !data.solver_available;
    $('solverStatus').textContent = data.solver_available ? 'An API key is configured on your server. A request may use paid credits.' : 'No API key configured. Solve manually or set NOPECHA_API_KEY in Render.';
    status('Browser ready. Review the screenshot, then sign in.');
    timer = setInterval(() => { pollBatch(); refresh(); }, 3000);
  } catch(e) { ticket = ''; status(e.message + ' Request a new /login link to retry.'); }
  finally { busy = false; await refresh(); }
};
$('screen').onclick = e => {
  if (!token || busy) return;
  const rect = e.target.getBoundingClientRect();
  run({op:'click', x:(e.clientX-rect.left)*1100/rect.width, y:(e.clientY-rect.top)*780/rect.height});
};
$('login').onsubmit = e => {
  e.preventDefault(); if(busy) return;
  const email = $('email').value, password = $('password').value;
  $('password').value = ''; run({op:'login',email,password});
};
$('typing').onsubmit = e => {
  e.preventDefault(); if(busy) return;
  const text = $('text').value; $('text').value = '';
  run({op:'type',text,replace:$('replace').checked});
};
document.querySelectorAll('[data-op]').forEach(b => b.onclick = () => run({op:b.dataset.op}));
document.querySelectorAll('[data-key]').forEach(b => b.onclick = () => run({op:'key',key:b.dataset.key}));
document.querySelectorAll('[data-scroll]').forEach(b => b.onclick = () => run({op:'scroll',dy:Number(b.dataset.scroll)}));
$('refresh').onclick = refresh;
$('solver').onclick = () => { if(!$('consent').checked) { status('Consent is required before sending the CAPTCHA image to NopeCHA.'); return; } run({op:'solver',consent:true}); };
$('reveal').onclick = () => $('key').type = $('key').type === 'password' ? 'text' : 'password';
$('clear').onclick = clearValue;
$('copy').onclick = async () => {
  try { await navigator.clipboard.writeText($('key').value); status('Copied to this device’s clipboard. Clear it when finished.'); }
  catch { status('Clipboard unavailable. Use Download, or show the value and copy manually.'); }
};
$('download').onclick = () => {
  const value = $('key').value;
  if (!/^[A-Za-z0-9_-]{20,500}$/.test(value)) { status('Clipboard value does not look like a single key. Use Show to inspect it; copy the actual key in the remote browser first.'); return; }
  const url = URL.createObjectURL(new Blob(['ELEVENLABS_API_KEY='+value+'\n'], {type:'text/plain'}));
  const a = document.createElement('a'); a.href=url; a.download='elevenlabs.env'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  status('Plaintext key file downloaded. Store it securely and do not commit it to GitHub.');
};
$('close').onclick = () => {
  if(batchSnapshot && batchSnapshot.mode !== 'none' && !confirm('Closing permanently discards the batch password and collected results. Have you downloaded the results?')) return;
  run({op:'close'});
};
window.addEventListener('pagehide', () => { clearValue(); $('password').value=''; $('text').value=''; $('batchPassword').value=''; $('batchEmails').value=''; });


function renderBatch(data) {
  const previousEmail = batchSnapshot?.rows?.[batchSnapshot?.current_index]?.email;
  batchSnapshot = data;
  const exists = data.mode !== 'none';
  const running = data.mode === 'running';
  const terminal = ['finished','cancelled'].includes(data.mode);
  const row = data.rows?.[data.current_index];
  $('batchForm').hidden = exists;
  $('batchPanel').hidden = !exists;
  $('batchMessage').textContent = exists ? data.mode.toUpperCase() + ' · ' + (data.message || '') : '';
  if(typeof data.session_seconds_remaining === 'number') {
    $('batchExpiry').textContent = 'Session remaining: '+Math.ceil(data.session_seconds_remaining/60)+' min · idle timeout: '+Math.ceil(data.idle_seconds_remaining/60)+' min. Download collected keys regularly.';
  } else if(!exists) { $('batchExpiry').textContent = ''; }
  const body = $('batchRows'); body.replaceChildren();
  for(const row of data.rows || []) {
    const tr = document.createElement('tr');
    for(const value of [row.email, row.phase, row.status, row.key_collected ? 'Collected' : '—']) {
      const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
    }
    tr.title = row.message || ''; body.appendChild(tr);
  }
  $('pauseBatch').disabled = !running;
  $('resumeBatch').disabled = running || terminal;
  $('skipBatch').disabled = running || terminal || !row;
  $('cancelBatch').disabled = terminal;
  $('captureBatch').disabled = running || terminal || !row || row.identity === 'not_verified';
  $('clearBatch').disabled = running;
  $('exportBatch').disabled = !exists;
  $('identityPrompt').hidden = !row || row.phase !== 'identity' || running || terminal;
  $('identityEmail').textContent = row ? 'Confirm the signed-in account is exactly: ' + row.email : '';
  if(previousEmail !== row?.email) $('confirmIdentity').checked = false;
  document.querySelectorAll('#login input, #login button, [data-op="signin"], [data-op="keys"]').forEach(e=>e.disabled=exists);
}
async function pollBatch() {
  if(!token || batchPolling || batchBusy) return;
  batchPolling = true;
  try {
    const r = await fetch('/api/batch/status',{headers:{'Authorization':'Bearer '+token},cache:'no-store'});
    if(r.status === 401) { finish(); status('Session expired. In-memory results cannot be recovered after cleanup.'); return; }
    if(!r.ok) return;
    const data=await r.json(); if(token) renderBatch(data);
  } catch { /* transient wake-up/network failures do not replace the account status */ }
  finally { batchPolling=false; }
}
async function batchRequest(path, data) {
  if(batchBusy || !token) return;
  batchBusy = true;
  try { renderBatch(await api(path,data)); }
  catch(e) { status(e.message); }
  finally { batchBusy=false; await refresh(); }
}
$('disableLeak').onchange = () => {
  $('acceptLeakRisk').required = $('disableLeak').checked;
  if(!$('disableLeak').checked) $('acceptLeakRisk').checked = false;
};
$('batchForm').onsubmit = e => {
  e.preventDefault(); if(batchBusy || !token) return;
  const body = {
    emails:$('batchEmails').value, password:$('batchPassword').value,
    authorize_full_access:$('authorizeFullAccess').checked,
    disable_leak_revocation:$('disableLeak').checked,
    accept_leak_risk:$('acceptLeakRisk').checked
  };
  $('batchPassword').value = ''; $('batchEmails').value = '';
  batchRequest('/api/batch/start',body);
};
$('pauseBatch').onclick = () => batchRequest('/api/batch/control',{op:'pause'});
$('resumeBatch').onclick = () => {
  const row=batchSnapshot?.rows?.[batchSnapshot.current_index];
  batchRequest('/api/batch/control',{op:'resume',confirm_identity:row?.phase === 'identity' && $('confirmIdentity').checked,expected_email:row?.email});
};
$('skipBatch').onclick = () => {
  if(confirm('Skip this account? A key may already have been created and will NOT be revoked. Save any visible key first.')) batchRequest('/api/batch/control',{op:'skip',confirm_skip:true});
};
$('cancelBatch').onclick = () => {
  if(confirm('Cancel the queue? The shared password is cleared and you cannot resume this batch. Any not-yet-copied key may be lost; already created keys are NOT revoked. Collected keys remain downloadable until this session closes.')) batchRequest('/api/batch/control',{op:'cancel'});
};
$('clearBatch').onclick = () => {
  if(confirm('Have you downloaded the results? Clearing permanently discards the shared password and all collected keys from this session.')) batchRequest('/api/batch/control',{op:'clear',confirm_clear:true});
};
$('captureBatch').onclick = () => {
  const row=batchSnapshot?.rows?.[batchSnapshot.current_index];
  if(confirm('Confirm that the remote clipboard contains the NEW API key for '+row?.email+'. Requested settings will be marked unverified, then the next account starts.')) batchRequest('/api/batch/control',{op:'capture',confirm_manual_key:true});
};
$('exportBatch').onclick = async () => {
  if(!token) return;
  try {
    const r=await fetch('/api/batch/export',{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:'{}',cache:'no-store'});
    if(r.status === 401) { finish(); throw Error('Session expired.'); }
    if(!r.ok) throw Error('Could not download batch results. Try again before closing.');
    const blob=await r.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='elevenlabs-batch-keys.csv'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    status('Downloaded a plaintext CSV containing collected keys. Store it securely; passwords are not included.');
  } catch(e) { status(e.message); }
};
