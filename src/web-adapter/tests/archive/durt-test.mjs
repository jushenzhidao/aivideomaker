// Submit the remaining 720p duration probes, waiting out the dynamic captcha
// gate between submissions.  Writes progress to /tmp/durtest.json and a log.
import { AvmClient } from '../../client.mjs';
import fs from 'node:fs';

const COOKIE = process.env.AVM_COOKIE;
const DEADLINE = Date.now() + 75 * 60_000; // give up after 75 min
const LOG = '/tmp/durtest.log';
const STATE = '/tmp/durtest.json';

const log = (m) => { const line = new Date().toISOString() + ' ' + m; console.log(line); fs.appendFileSync(LOG, line + '\n'); };

const state = JSON.parse(fs.readFileSync(STATE, 'utf8'));
const pending = [6, 7, 8, 9, 10].filter((d) => !state[d]);
const c = new AvmClient({ cookie: COOKIE });
const PROMPT = 'a red balloon rising slowly into a clear blue sky';

log('pending: ' + pending.join(','));

while (pending.length && Date.now() < DEADLINE) {
  let gate = null;
  try { gate = await c.needsCaptcha(); } catch (e) { log('needsCaptcha error: ' + e.message); }

  if (gate === false) {
    const d = pending[0];
    try {
      const id = await c.create({ content: PROMPT, duration: d, aspectRatio: '16:9', resolution: '720p', tier: 'turbo' });
      if (id) {
        state[d] = id;
        fs.writeFileSync(STATE, JSON.stringify(state, null, 2));
        log('720p ' + d + 's -> taskId=' + id);
        pending.shift();
        continue; // check the gate again immediately
      }
      log('720p ' + d + 's -> empty response (rejected)');
      state[d] = 'REJECTED';
      fs.writeFileSync(STATE, JSON.stringify(state, null, 2));
      pending.shift();
      continue;
    } catch (e) {
      if (e.code === 'CAPTCHA_REQUIRED') {
        log('gate flipped right after check; backing off 45s');
        await new Promise((r) => setTimeout(r, 45_000));
        continue;
      }
      log('720p ' + d + 's -> ERROR ' + e.code + ' ' + String(e.message).slice(0, 120));
      state[d] = 'ERROR';
      fs.writeFileSync(STATE, JSON.stringify(state, null, 2));
      pending.shift();
      continue;
    }
  }
  await new Promise((r) => setTimeout(r, gate === true ? 45_000 : 20_000));
}

if (pending.length) log('TIMEOUT, never submitted: ' + pending.join(','));
else log('all submitted');

// wait for every task to reach a terminal state, then dump the billing data
const ids = Object.entries(state).filter(([, v]) => typeof v === 'string' && v !== 'REJECTED' && v !== 'ERROR');
for (const [d, id] of ids) {
  try {
    const w = await c.waitForTask(id, { timeoutMs: 600_000, intervalMs: 15_000 });
    const t = w.task || {};
    log('RESULT 720p ' + d + 's -> ' + w.status + ' | duration=' + t.duration + ' res=' + t.kelingKeyId +
        ' credits=' + t.credits + ' paid=' + t.paid + ' ms=' + Math.round(w.ms / 1000) + 's');
  } catch (e) { log('RESULT 720p ' + d + 's -> poll error: ' + e.message); }
}
log('DONE');
