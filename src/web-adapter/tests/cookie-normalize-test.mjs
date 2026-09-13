// Offline unit test for normalizeCookieHeader() -- no network, no cookie needed.
//
// Why it exists: the value goes out VERBATIM as the `Cookie` request header, so a
// malformed one does not fail loudly -- the server just treats you as logged out,
// which is indistinguishable from an expired session.  That is exactly the kind of
// misdiagnosis this project has already been burned by.  Lock the behaviour down.
//
// CASES mirrors tests/test_cookie_normalize.py 1:1.  Change one, change the other.
import assert from 'node:assert/strict';
import { AvmClient, normalizeCookieHeader } from '../client.mjs';

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

const T = 'abcdef1234567890abcdef1234567890abcdef12'; // 40 chars, ^[a-z0-9]{40}$
const H = `auth_session=${T}`;
const HN = `auth_session=${T}; NEXT_LOCALE=zh`;

// [case name, input, expected]
const CASES = [
  ['bare token', T, H],
  ['auth_session only', H, H],
  ['pairs with no space after ";"', `auth_session=${T};NEXT_LOCALE=zh`, HN],
  ['pairs with a space after ";"', HN, HN],
  ['"Cookie:" label', `Cookie: auth_session=${T};NEXT_LOCALE=zh`, HN],
  ['"cookie:" label, lowercase', `cookie: ${H}`, H],
  ['"Set-Cookie:" label + attributes', `Set-Cookie: ${H}; Path=/; HttpOnly; Secure; SameSite=Lax`, H],
  ['curl -H wrapper', `-H 'Cookie: ${H}; NEXT_LOCALE=zh'`, HN],
  ['curl --cookie wrapper', `--cookie '${H}'`, H],
  ['shell double quotes', `"${H}"`, H],
  ['exported jar JSON',
    `[{"name":"auth_session","value":"${T}"},{"name":"NEXT_LOCALE","value":"zh"}]`, HN],
  ['cookie name case is preserved', `AuthSession=${T};NEXT_LOCALE=zh`, `AuthSession=${T}; NEXT_LOCALE=zh`],
  ['multi-line paste, every line ends with ";"', `auth_session=${T};\n  NEXT_LOCALE=zh;`, HN],
  ['empty', '', ''],
  ['whitespace only', '   ', ''],
  ['null', null, ''],
  ['undefined', undefined, ''],
  ['too short -- never guessed', 'abc', 'abc'],
  ['contains a space -- never guessed', 'foo bar', 'foo bar'],
  ['unrelated pairs pass through', 'a=b; c=d', 'a=b; c=d'],
  ['15 chars -- below the token threshold', 'a'.repeat(15), 'a'.repeat(15)],
  ['16 chars -- at the token threshold', 'a'.repeat(16), `auth_session=${'a'.repeat(16)}`],
  ['non-ascii -- never guessed', '一二三四五六七八九十一二三四五六七八', '一二三四五六七八九十一二三四五六七八'],
  ['surrounding whitespace', `  ${T}  `, H],
  ['trailing semicolon', `${H};`, H],
  // Malformed ON PURPOSE: a newline with no ";" must go out AS-IS so the HTTP layer
  // fails loudly.  Welding it into one pair would send a silently wrong cookie.
  ['newline without ";" passes through untouched',
    `auth_session=${T}\n NEXTLOCALE=zh`, `auth_session=${T}\n NEXTLOCALE=zh`],
];

console.log('\n======== normalizeCookieHeader ========');
for (const [name, input, want] of CASES) {
  t(name, () => assert.equal(normalizeCookieHeader(input), want));
}

console.log('\n======== warnings ========');

t('a bare token warns -- silently "fixing" teaches the user nothing', () => {
  const seen = [];
  const orig = console.warn;
  console.warn = (...a) => seen.push(a.join(' '));
  try {
    normalizeCookieHeader(T);
  } finally {
    console.warn = orig;
  }
  assert.equal(seen.length, 2);
  assert.match(seen.join('\n'), /bare token/);
});

t('a well-formed header does not warn', () => {
  const seen = [];
  const orig = console.warn;
  console.warn = (...a) => seen.push(a.join(' '));
  try {
    normalizeCookieHeader(HN);
  } finally {
    console.warn = orig;
  }
  assert.deepEqual(seen, []);
});

console.log('\n======== AvmClient contract ========');

t('a bare token becomes a full Cookie header on the way in', () => {
  assert.equal(new AvmClient({ cookie: T }).cookie, H);
});

t('_headers() emits the expanded header, never the bare token', () => {
  assert.equal(new AvmClient({ cookie: T })._headers().cookie, H);
});

t('a full pasted header survives the constructor unchanged', () => {
  assert.equal(new AvmClient({ cookie: HN }).cookie, HN);
});

t('empty credentials still throw (no silent downgrade to anonymous)', () => {
  assert.throws(() => new AvmClient({ cookie: '   ' }), /needs opts\.cookie/);
});

console.log(`\n${pass} passed${failed ? `, ${failed} FAILED` : ''}`);
process.exitCode = failed ? 1 : 0;
