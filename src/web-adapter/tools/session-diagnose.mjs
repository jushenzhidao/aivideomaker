#!/usr/bin/env node
// Zero-cost session & anonymous-path diagnostic for the aivideomaker web line.
//
// WHY THIS EXISTS
//   `auth_session` is opaque (no JWT claims), the site never re-issues it, and no
//   endpoint exposes its expiry.  So the only durable facts are:
//     (a) the TTL read out of an exported cookie jar  -> see docs/web-reverse/cookies.md
//     (b) what the live endpoints actually answer      -> this script
//   It answers (b), and it does so WITHOUT creating a task unless you pass --submit.
//
// COST MODEL (hard rule -- see docs/web-reverse/session-runbook.md)
//   default      : GET queries only          -> 0 credits, no task created
//   --dry-run    : builds the requests, sends nothing at all
//   --submit     : adds ONE POST to ai.minimaxH3, hard-coded to the free combo
//                  (tier=turbo + 480p + 5s).  Any other combination is not
//                  reachable from this tool on purpose.
//
// NO-DATA HONESTY
//   Under --dry-run nothing is sent, so every derived conclusion is suppressed.
//   A diagnose tool that prints "gate is closed" without having asked the server
//   is worse than useless: it manufactures false confidence.
//
// USAGE
//   node tools/session-diagnose.mjs                    # anonymous (no cookie)
//   AVM_COOKIE='...' node tools/session-diagnose.mjs   # authenticated
//   node tools/session-diagnose.mjs --dry-run          # offline self-check
//   node tools/session-diagnose.mjs --submit           # + free-combo submit
//
// Secrets: the cookie value is never printed.  Only a short fingerprint, and
// only the NAMES of any cookies the server re-issues.
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { normalizeCookieHeader } from '../client.mjs';

const ORIGIN = process.env.AVM_BASE_URL || 'https://aivideomaker.ai';
const PAGE = '/zh/ai-video-generator';
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36';

// tRPC "no argument" is null + meta marking it undefined (positional shape).
const VOID_INPUT = { 0: { json: null, meta: { values: ['undefined'], v: 1 } } };

const VISITOR_ID = process.env.AVM_VISITOR_ID || 'f29ee26edcb4e8b96ee17e277a384f6f';

// Frozen on purpose: the web line is free only at tier=turbo and duration <= 8s.
// Do not parameterise this -- it is the billing seatbelt, not a convenience.
const FREE_COMBO = Object.freeze({
  aspectRatio: '16:9',
  duration: 5,
  resolution: '480p',
  tier: 'turbo',
  promptEnrichment: false,
});

const argv = process.argv.slice(2);
const has = (f) => argv.includes(f);
const flagValue = (f, dflt) => {
  const i = argv.indexOf(f);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
};

const DRY = has('--dry-run');
const SUBMIT = has('--submit');
const OUT = flagValue('--json', process.env.AVM_DIAGNOSE_OUT || '/tmp/avm-session-diagnose.json');

// ---------------------------------------------------------------- cookie ----

/** Same precedence as adapter.mjs: AVM_COOKIE first, then COOKIES_FILE. */
async function loadCookie() {
  // A bare token gets expanded by normalizeCookieHeader(), which prints its own
  // warning -- we never silently "fix" a mis-pasted credential.
  const env = normalizeCookieHeader(process.env.AVM_COOKIE);
  if (env) return { cookie: env, source: 'AVM_COOKIE' };
  const f = process.env.COOKIES_FILE || './cookies.json';
  if (!existsSync(f)) return { cookie: '', source: `none (${f} absent)` };
  try {
    const arr = JSON.parse(await readFile(f, 'utf8'));
    const s = arr
      .filter((c) => c.name && c.value)
      .map((c) => `${c.name}=${c.value}`)
      .join('; ');
    return { cookie: s, source: `${f} (${arr.length} cookies)` };
  } catch (e) {
    return { cookie: '', source: `none (${f} unreadable: ${e.message})` };
  }
}

function fingerprint(cookie) {
  const m = cookie.match(/auth_session=([^;]+)/);
  return m ? `${m[1].slice(0, 6)}…${m[1].slice(-4)}` : '(no auth_session)';
}

/** Names only -- never echo a re-issued token. */
function rereleasedCookieNames(setCookie) {
  if (!setCookie) return [];
  return [...new Set((setCookie.match(/(?:^|,\s*)([A-Za-z0-9_.-]+)=/g) || []).map((s) => s.replace(/[,\s=]/g, '')))];
}

// ---------------------------------------------------------------- probes ----

function reqHeaders(cookie, json = false) {
  const h = {
    'user-agent': UA,
    'accept-language': 'zh-CN,zh;q=0.9',
    referer: `${ORIGIN}${PAGE}`,
    accept: '*/*',
  };
  if (json) h['content-type'] = 'application/json';
  if (cookie) h.cookie = cookie;
  return h;
}

function buildGet(proc, input, meta) {
  const payload = JSON.stringify(meta ?? { 0: { json: input } });
  return `${ORIGIN}/api/${proc}?batch=1&input=${encodeURIComponent(payload)}`;
}

/**
 * Unwrap one tRPC batch response.  Returns a normalised record so the caller
 * never has to care whether the upstream answered with a result or an error.
 */
function unwrap(proc, http, text, setCookie) {
  const rec = { proc, http, sent: true, setCookie: rereleasedCookieNames(setCookie) };
  let arr = null;
  try {
    arr = JSON.parse(text);
  } catch {
    // jsonl fallback (the python client asks for application/jsonl)
    try {
      arr = [JSON.parse(text.split('\n')[0])];
    } catch {
      arr = null;
    }
  }
  const first = Array.isArray(arr) ? arr[0] : null;
  if (first?.error) {
    rec.ok = false;
    // zod errors arrive as multi-line dumps; collapse so one probe stays one line
    rec.error = String(first.error.json?.message ?? '(no message)')
      .replace(/\s+/g, ' ')
      .slice(0, 180);
    rec.code = first.error.json?.data?.code ?? null;
  } else if (first?.result) {
    rec.ok = true;
    rec.data = first.result.data?.json ?? null;
  } else {
    rec.ok = false;
    rec.error = `unrecognised envelope (${text.slice(0, 120)})`;
  }
  return rec;
}

async function sendGet(proc, cookie, input = null, meta = null) {
  const url = buildGet(proc, input, meta);
  if (DRY) return { proc, sent: false, url };
  try {
    const r = await fetch(url, { headers: reqHeaders(cookie) });
    return unwrap(proc, r.status, await r.text(), r.headers.get('set-cookie'));
  } catch (e) {
    return { proc, http: -1, sent: true, ok: false, error: `network: ${e.message}` };
  }
}

async function sendSubmit(cookie, body) {
  const url = `${ORIGIN}/api/ai.minimaxH3?batch=1`;
  const payload = JSON.stringify({ 0: { json: body } });
  if (DRY) return { proc: 'ai.minimaxH3', sent: false, url, payload };
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { ...reqHeaders(cookie, true), origin: ORIGIN },
      body: payload,
    });
    const rec = unwrap('ai.minimaxH3', r.status, await r.text(), r.headers.get('set-cookie'));
    rec.taskId = typeof rec.data === 'string' ? rec.data : '';
    return rec;
  } catch (e) {
    return { proc: 'ai.minimaxH3', http: -1, sent: true, ok: false, error: `network: ${e.message}` };
  }
}

// ----------------------------------------------------------------- main ----

const { cookie, source } = await loadCookie();
const mode = cookie ? 'auth' : 'anonymous';

const report = {
  generated_at: new Date().toISOString(),
  mode,
  dry_run: DRY,
  cookie_source: source,
  cookie_fingerprint: fingerprint(cookie),
  probes: [],
  verdict: {
    identity: mode === 'auth' ? 'authenticated session' : 'anonymous (no cookie sent)',
    identityAccepted: null,
    needsCaptcha: null,
    totalRemaining: null,
    anonymousSubmits: null,
  },
};

console.log(`aivideomaker session diagnostic  [mode=${mode}${DRY ? ', DRY-RUN' : ''}]`);
console.log(`  cookie     : ${report.cookie_fingerprint}  (source: ${source})`);
console.log(`  cost model : GET queries only${SUBMIT ? ' + ONE free-combo submit' : ' -- no task will be created'}`);
console.log('');

// 1. identity
const user = await sendGet('auth.user', cookie, null, VOID_INPUT);
report.probes.push(user);

// 2. the dynamic captcha gate -- re-ask every time, never cache
const gate = await sendGet('model.needsCaptcha', cookie, { userId: user.sent ? (user.data?.id ?? null) : null });
report.probes.push(gate);

// 3. credit pool（站点账号余额）
const credits = await sendGet('credits.getCredits', cookie, null, VOID_INPUT);
report.probes.push(credits);

if (!DRY) {
  report.verdict.identityAccepted = Boolean(user.ok && user.data?.id);
  report.verdict.userId = user.data?.id ?? null;
  report.verdict.needsCaptcha = gate.ok ? Boolean(gate.data) : null;
  report.verdict.totalRemaining = credits.ok ? (credits.data?.totalRemaining ?? null) : null;
}

function detailFor(rec) {
  if (!rec.sent) return '(not sent)';
  if (!rec.ok) return `error=${rec.error}${rec.code ? ` code=${rec.code}` : ''}`;
  if (rec.proc === 'auth.user') return `userId=${rec.data?.id ?? 'none'}`;
  if (rec.proc === 'model.needsCaptcha') return `needsCaptcha=${Boolean(rec.data)}`;
  if (rec.proc === 'credits.getCredits') return `totalRemaining=${rec.data?.totalRemaining ?? 'n/a'}`;
  return typeof rec.data === 'string' ? `data=${JSON.stringify(rec.data)}` : 'ok';
}

for (const p of report.probes) {
  const status = p.sent ? `HTTP ${p.http}` : 'not sent';
  console.log(`  ${p.proc.padEnd(22)} ${status.padEnd(9)} ${detailFor(p)}`);
  if (p.setCookie?.length) console.log(`  ${''.padEnd(31)}re-issued: ${p.setCookie.join(', ')}`);
}

// 4. optional: does the anonymous / authenticated path really reach a task?
if (SUBMIT) {
  console.log('');
  console.log(
    `  --submit: ONE POST ai.minimaxH3, free combo only ` +
      `(${FREE_COMBO.tier} / ${FREE_COMBO.resolution} / ${FREE_COMBO.duration}s)`,
  );
  const sub = await sendSubmit(cookie, {
    content: 'a cat',
    imageUrl: null,
    lastFrameUrl: null,
    referenceImageUrls: [],
    referenceVideoUrl: null,
    referenceAudioUrls: [],
    ...FREE_COMBO,
    visitorId: VISITOR_ID,
    token: null,
  });
  report.probes.push(sub);
  if (sub.sent) report.verdict.anonymousSubmits = Boolean(sub.taskId);
  console.log(`  ai.minimaxH3           ${(sub.sent ? `HTTP ${sub.http}` : 'not sent').padEnd(9)} ${detailFor(sub)}`);
  if (sub.taskId) {
    console.log('  NOTE: a task WAS created. turbo/480p/5s is the free window on an');
    console.log('        authenticated account; on an anonymous path nothing is billable.');
  }
}

// 5. verdict -- only statements the server actually supported
console.log('');
if (DRY) {
  console.log('DRY-RUN: nothing was sent, so no verdict can be derived.');
  console.log('         Re-run without --dry-run to ask the live endpoints.');
} else {
  // RPC-class sites signal "not logged in" with 200 + an EMPTY entity, not 401.
  // Reading "200 == success" turns a refusal into a pass, so name the case out loud.
  const noEntity200 = user.http === 200 && user.ok === true && user.data == null;
  report.verdict.unauthorized200 = noEntity200;

  if (report.verdict.identityAccepted) {
    console.log(`=> auth.user ACCEPTED the ${report.verdict.identity} (userId=${report.verdict.userId}).`);
  } else if (noEntity200) {
    console.log(`=> auth.user REFUSED the ${report.verdict.identity}: HTTP 200 with an EMPTY entity.`);
    console.log('   RPC-style rejection -- "200" here is NOT success. Do not read it as a pass.');
  } else {
    console.log(`=> auth.user REJECTED the ${report.verdict.identity} (HTTP ${user.http}, ${user.error ?? 'no id'}).`);
  }
  if (report.verdict.needsCaptcha === null) {
    console.log('=> captcha gate could not be read on this path.');
  } else if (report.verdict.needsCaptcha) {
    console.log('=> captcha gate is OPEN: token=null submits are silently rejected (returns "").');
  } else {
    console.log('=> captcha gate is CLOSED: token=null is accepted right now. It flips with velocity.');
  }
  console.log(
    `=> re-issued cookies: ${
      report.probes.some((p) => p.setCookie?.length)
        ? 'YES -- rolling refresh exists, revisit docs/web-reverse/cookies.md'
        : 'none -- consistent with the site NOT rolling the session'
    }`,
  );
}

// ------------------------------------------------------------------ out ----

if (!DRY) {
  try {
    const { writeFile } = await import('node:fs/promises');
    await writeFile(OUT, JSON.stringify(report, null, 2));
    console.log(`\nreport -> ${OUT}`);
  } catch (e) {
    console.error(`\ncould not write ${OUT}: ${e.message}`);
  }
}
