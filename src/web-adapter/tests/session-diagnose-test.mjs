// Offline test for tools/session-diagnose.mjs -- no network, no cookie, no credits.
//
// Why it exists: the tool's whole value is that it tells the truth about a live site.
// Until now the only way to check it was to point it at the real site, which means the
// two rules that matter most were never actually locked down:
//
//   1. A refusal must not be read as a pass.  Measured on the live site (2026-09-13):
//      `auth.user` without a cookie answers **HTTP 200 with an empty entity**, while
//      `credits.getCredits` answers **401 UNAUTHORIZED**.  Same "not logged in",
//      different shapes.  Any tool that keys off the status code alone is wrong.
//   2. `--dry-run` must emit NO derived conclusion.  A probe that prints "gate closed"
//      without having asked the server manufactures false confidence.
//
// So we stand up a local stub that replays those exact envelopes and assert on stdout.
//
// NOTE: the stub lives in THIS process, so the tool must be spawned **asynchronously**
// -- `execFileSync` blocks the event loop and the stub then cannot answer the child,
// which hangs until it is killed.  (Learned the hard way.)
//
// Run: node tests/session-diagnose-test.mjs
import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const pexec = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const TOOL = path.join(HERE, '..', 'tools', 'session-diagnose.mjs');

let pass = 0;
let failed = 0;
function t(name, fn) {
  try {
    fn();
    pass++;
    console.log('  ✓ ' + name);
  } catch (e) {
    failed++;
    console.log('  ✗ ' + name + ' — ' + e.message);
  }
}

// A multi-line zod dump, exactly the shape the live site returns for a bad input.
const ZOD_DUMP = `[
  {
    "code": "invalid_type",
    "expected": "string",
    "received": "null",
    "path": ["userId"],
    "message": "Expected string, received null"
  }
]`;

const server = http.createServer((req, res) => {
  const p = new URL(req.url, 'http://x').pathname;
  const batch = (obj) => {
    res.writeHead(200, { 'content-type': 'application/json', 'set-cookie': 'NEXT_LOCALE=zh; Max-Age=31536000' });
    res.end(JSON.stringify([obj]));
  };
  if (p === '/api/auth.user') {
    // rejection expressed as 200 + empty entity -- NOT a 401
    batch({ result: { data: { json: null } } });
  } else if (p === '/api/model.needsCaptcha') {
    // measured live: a null `userId` is rejected with 400 (not 200),
    // and the message is a multi-line zod dump
    res.writeHead(400, { 'content-type': 'application/json' });
    res.end(JSON.stringify([{ error: { json: { message: ZOD_DUMP, data: { code: 'BAD_REQUEST' } } } }]));
  } else if (p === '/api/credits.getCredits') {
    res.writeHead(401, { 'content-type': 'application/json' });
    res.end(JSON.stringify([{ error: { json: { message: 'UNAUTHORIZED', data: { code: 'UNAUTHORIZED' } } } }]));
  } else {
    res.writeHead(404).end();
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

/** Run the tool in an empty cwd so `./cookies.json` can never leak in. */
async function runTool(extraArgs = []) {
  const cwd = path.join(os.tmpdir(), 'avm-diag-test-cwd');
  await promisify(execFile)('mkdir', ['-p', cwd]).catch(() => {});
  const { stdout } = await pexec(process.execPath, [TOOL, ...extraArgs], {
    cwd,
    encoding: 'utf8',
    timeout: 30_000,
    env: {
      ...process.env,
      AVM_BASE_URL: `http://127.0.0.1:${port}`,
      AVM_COOKIE: '',
      AVM_DIAGNOSE_OUT: path.join(os.tmpdir(), 'avm-diag-test.json'),
      COOKIES_FILE: path.join(cwd, 'absent.json'),
    },
  });
  return stdout;
}

console.log('\n======== anonymous path (live shapes, local stub) ========');
const anon = await runTool();

t('auth.user is reported as 200 but with no userId', () => {
  assert.match(anon, /auth\.user\s+HTTP 200\s+userId=none/);
});
t('credits.getCredits 401 is surfaced with its code', () => {
  assert.match(anon, /credits\.getCredits\s+HTTP 401/);
  assert.match(anon, /UNAUTHORIZED/);
});

// Rule 1 -- 200 + empty entity must be called a REFUSAL, not a pass
t('200 + empty entity is named a REFUSAL, not an acceptance', () => {
  assert.match(anon, /auth\.user REFUSED/);
  assert.match(anon, /EMPTY entity/);
  assert.match(anon, /"200" here is NOT success/);
  assert.doesNotMatch(anon, /auth\.user ACCEPTED/);
});

// Rule 2 -- one probe is one line, even when the server sends a multi-line dump
t('a multi-line zod dump is collapsed to a single line', () => {
  const probeLine = anon.split('\n').find((l) => l.includes('model.needsCaptcha'));
  assert.ok(probeLine, 'no needsCaptcha line found');
  assert.match(probeLine, /HTTP 400/);
  assert.match(probeLine, /invalid_type/);
});

t('the gate is reported as unreadable rather than guessed at', () => {
  assert.match(anon, /captcha gate could not be read on this path/);
  assert.doesNotMatch(anon, /gate is CLOSED/);
});

console.log('\n======== --dry-run must conclude nothing ========');
const dry = await runTool(['--dry-run']);
t('dry-run says nothing was sent', () => {
  assert.match(dry, /nothing was sent, so no verdict can be derived/);
});
t('dry-run leaks no verdict of any kind', () => {
  for (const forbidden of ['REFUSED', 'ACCEPTED', 'gate is CLOSED', 'gate is OPEN', 're-issued cookies:']) {
    const re = new RegExp(forbidden.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    assert.doesNotMatch(dry, re, `dry-run leaked "${forbidden}"`);
  }
});
t('dry-run marks every probe as not sent', () => {
  assert.equal((dry.match(/not sent/g) || []).length >= 3, true);
});

server.close();
console.log(`\n${pass} passed${failed ? `, ${failed} FAILED` : ''}`);
process.exitCode = failed ? 1 : 0;
