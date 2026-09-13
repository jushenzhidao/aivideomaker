// Resubmit the 720p / 10s probe (it failed earlier on the 2-task concurrency cap).
import { AvmClient } from '../../client.mjs';
import fs from 'node:fs';

const LOG = '/tmp/durtest.log';
const STATE = '/tmp/durtest.json';
const DEADLINE = Date.now() + 30 * 60_000;
const log = (m) => { const line = new Date().toISOString() + ' [10s-retry] ' + m; console.log(line); fs.appendFileSync(LOG, line + '\n'); };

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const PROMPT = 'a red balloon rising slowly into a clear blue sky';

while (Date.now() < DEADLINE) {
  let gate = null;
  try { gate = await c.needsCaptcha(); } catch (e) { log('needsCaptcha error: ' + e.message); }
  if (gate === false) {
    try {
      const id = await c.create({ content: PROMPT, duration: 10, aspectRatio: '16:9', resolution: '720p', tier: 'turbo' });
      if (!id) { log('empty response (rejected)'); process.exit(0); }
      log('720p 10s -> taskId=' + id);
      const st = JSON.parse(fs.readFileSync(STATE, 'utf8'));
      st['10'] = id;
      fs.writeFileSync(STATE, JSON.stringify(st, null, 2));
      const w = await c.waitForTask(id, { timeoutMs: 600_000, intervalMs: 15_000 });
      const t = w.task || {};
      log('RESULT 720p 10s -> ' + w.status + ' | duration=' + t.duration + ' res=' + t.kelingKeyId +
          ' credits=' + t.credits + ' paid=' + t.paid + ' ms=' + Math.round(w.ms / 1000) + 's');
      process.exit(0);
    } catch (e) {
      if (e.code === 'CAPTCHA_REQUIRED') { log('gate flipped after check; back off 45s'); await new Promise(r => setTimeout(r, 45_000)); continue; }
      if (/queue is full/i.test(e.message)) { log('queue still full; back off 30s'); await new Promise(r => setTimeout(r, 30_000)); continue; }
      log('ERROR ' + e.code + ' ' + String(e.message).slice(0, 140));
      process.exit(0);
    }
  }
  await new Promise(r => setTimeout(r, gate === true ? 45_000 : 20_000));
}
log('TIMEOUT');
