// Session health check for the aivideomaker cookie.
//
// Why this exists: `auth_session` is an OPAQUE random token (no embedded
// timestamp, not a JWT), the site never re-issues it on any request, and no
// endpoint exposes its expiry.  So the only reliable strategy is to keep a
// timestamped record and alert when it dies.
//
// Records first-seen / last-ok in .session-state.json and exits non-zero when
// the session is no longer valid, so it can be dropped into a scheduler.
//
// Usage: AVM_COOKIE='...' node check-session.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { AvmClient, normalizeCookieHeader } from '../client.mjs';

// 状态文件锚定在**包根**（src/web-adapter/），而不是 process.cwd()，也不是
// 脚本所在的 tools/ 子目录 —— 否则从不同目录跑就会各写一份，同一个指纹下
// "已确认存活" 这个数字会随 cwd 变化，计时数据自相矛盾，兜底就失去意义。
const STATE = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '.session-state.json');
// Accept a bare session token as well -- it is the single most likely thing to
// get pasted, and taking it literally would report "session invalid" on a
// perfectly good credential.
const cookie = normalizeCookieHeader(process.env.AVM_COOKIE);
if (!cookie) { console.error('no AVM_COOKIE'); process.exit(2); }

// A short fingerprint so we can tell when the user swaps in a new cookie.
const fp = cookie.match(/auth_session=([^;]+)/)?.[1] || '';
const fingerprint = fp ? fp.slice(0, 6) + '…' + fp.slice(-4) : '(no auth_session)';

let state = {};
try { state = JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { /* first run */ }

const now = new Date().toISOString();
const c = new AvmClient({ cookie });
const out = { fingerprint, checked_at: now, ok: false };

try {
  const user = await c.getUserId();
  if (!user) throw new Error('auth.user returned no id');
  out.ok = true;
  out.userId = user;
  out.needsCaptcha = await c.needsCaptcha();
} catch (e) {
  out.error = String(e.message || e).slice(0, 200);
  out.errorCode = e.code || null;
}

if (state.fingerprint !== fingerprint) {
  // A different session token: restart the clock.
  state = { fingerprint, first_seen: now, last_ok: out.ok ? now : null, last_check: now };
} else {
  state.last_check = now;
  if (out.ok) state.last_ok = now;
}
fs.writeFileSync(STATE, JSON.stringify(state, null, 2));

out.first_seen = state.first_seen;
out.last_ok = state.last_ok;
if (state.first_seen) {
  const h = (Date.now() - new Date(state.first_seen).getTime()) / 3_600_000;
  out.observed_valid_hours = +h.toFixed(1);
  out.observed_valid_days = +(h / 24).toFixed(2);
}

if (out.ok) {
  console.log('✅ session valid');
  console.log('   userId          :', out.userId);
  console.log('   needsCaptcha    :', out.needsCaptcha);
} else {
  console.log('❌ session INVALID — re-export the cookie from the browser');
  console.log('   error           :', out.error, out.errorCode ? `(code ${out.errorCode})` : '');
}
console.log('   cookie          :', fingerprint);
console.log('   first seen      :', state.first_seen);
console.log('   last OK         :', state.last_ok);
if (out.observed_valid_hours !== undefined) {
  console.log('   已确认存活      :', out.observed_valid_hours, '小时 (', out.observed_valid_days, '天 )');
}
console.log('   state file      :', STATE);

process.exit(out.ok ? 0 : 1);
