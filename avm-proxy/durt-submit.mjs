// Submit one 720p probe at a given duration, waiting out the dynamic captcha
// gate and the 2-task concurrency cap, then report the billing result.
//
// Usage: AVM_COOKIE='...' node durt-submit.mjs <seconds> [resolution]
import { AvmClient } from './client.mjs';
import fs from 'node:fs';

const DUR = Number(process.argv[2]);
const RES = process.argv[3] || '720p';
if (!Number.isFinite(DUR)) { console.error('usage: node durt-submit.mjs <seconds> [resolution]'); process.exit(1); }

const LOG = '/tmp/durtest.log';
const STATE = '/tmp/durtest.json';
const DEADLINE = Date.now() + 40 * 60_000;
const log = (m) => { const line = new Date().toISOString() + ` [${DUR}s] ` + m; console.log(line); fs.appendFileSync(LOG, line + '\n'); };

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const PROMPT = 'a red balloon rising slowly into a clear blue sky';
const record = (v) => { const st = JSON.parse(fs.readFileSync(STATE, 'utf8')); st[DUR] = v; fs.writeFileSync(STATE, JSON.stringify(st, null, 2)); };

while (Date.now() < DEADLINE) {
  let gate = null;
  try { gate = await c.needsCaptcha(); } catch (e) { log('needsCaptcha error: ' + e.message); }

  if (gate === false) {
    try {
      const id = await c.create({ content: PROMPT, duration: DUR, aspectRatio: '16:9', resolution: RES, tier: 'turbo' });
      if (!id) { log('empty response (rejected)'); record('REJECTED'); process.exit(0); }
      log('taskId=' + id);
      record(id);
      const w = await c.waitForTask(id, { timeoutMs: 900_000, intervalMs: 15_000 });
      const t = w.task || {};
      log('RESULT ' + w.status + ' | duration=' + t.duration + ' res=' + t.kelingKeyId +
          ' credits=' + t.credits + ' paid=' + t.paid + ' ms=' + Math.round(w.ms / 1000) + 's url=' + (t.url || ''));
      process.exit(0);
    } catch (e) {
      if (e.code === 'CAPTCHA_REQUIRED') { log('gate flipped after check; back off 45s'); await new Promise(r => setTimeout(r, 45_000)); continue; }
      if (/queue is full/i.test(e.message)) { log('queue full; back off 30s'); await new Promise(r => setTimeout(r, 30_000)); continue; }
      log('ERROR ' + e.code + ' ' + String(e.message).slice(0, 140));
      record('ERROR');
      process.exit(0);
    }
  }
  await new Promise(r => setTimeout(r, gate === true ? 45_000 : 20_000));
}
log('TIMEOUT waiting for a captcha window');
