// Submit one 720p / 11s probe, waiting out the dynamic captcha gate.
import { AvmClient } from './client.mjs';
import fs from 'node:fs';

const DUR = 11;
const LOG = '/tmp/durtest.log';
const STATE = '/tmp/durtest.json';
const DEADLINE = Date.now() + 75 * 60_000;
const log = (m) => { const line = new Date().toISOString() + ' [11s] ' + m; console.log(line); fs.appendFileSync(LOG, line + '\n'); };

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const PROMPT = 'a red balloon rising slowly into a clear blue sky';

while (Date.now() < DEADLINE) {
  let gate = null;
  try { gate = await c.needsCaptcha(); } catch (e) { log('needsCaptcha error: ' + e.message); }

  if (gate === false) {
    try {
      const id = await c.create({ content: PROMPT, duration: DUR, aspectRatio: '16:9', resolution: '720p', tier: 'turbo' });
      if (id) {
        log('720p ' + DUR + 's -> taskId=' + id);
        const st = JSON.parse(fs.readFileSync(STATE, 'utf8'));
        st[DUR] = id;
        fs.writeFileSync(STATE, JSON.stringify(st, null, 2));
        const w = await c.waitForTask(id, { timeoutMs: 600_000, intervalMs: 15_000 });
        const t = w.task || {};
        log('RESULT 720p ' + DUR + 's -> ' + w.status + ' | duration=' + t.duration + ' res=' + t.kelingKeyId +
            ' credits=' + t.credits + ' paid=' + t.paid + ' ms=' + Math.round(w.ms / 1000) + 's');
        process.exit(0);
      }
      log('720p ' + DUR + 's -> empty response (rejected)');
      const st = JSON.parse(fs.readFileSync(STATE, 'utf8'));
      st[DUR] = 'REJECTED';
      fs.writeFileSync(STATE, JSON.stringify(st, null, 2));
      process.exit(0);
    } catch (e) {
      if (e.code === 'CAPTCHA_REQUIRED') { log('gate flipped after check; back off 45s'); await new Promise(r => setTimeout(r, 45_000)); continue; }
      log('720p ' + DUR + 's -> ERROR ' + e.code + ' ' + String(e.message).slice(0, 120));
      const st = JSON.parse(fs.readFileSync(STATE, 'utf8'));
      st[DUR] = 'ERROR';
      fs.writeFileSync(STATE, JSON.stringify(st, null, 2));
      process.exit(0);
    }
  }
  await new Promise(r => setTimeout(r, gate === true ? 45_000 : 20_000));
}
log('TIMEOUT waiting for a captcha window');
