// Submit a queue of 720p/480p duration probes sequentially.
// Handles both walls: the dynamic captcha gate (one submit per window) and the
// premium 2-concurrent-task cap ("The queue is full").
//
// Usage: AVM_COOKIE='...' node durt-batch.mjs 8:720p 10:720p 9:480p
import { AvmClient } from '../../client.mjs';
import fs from 'node:fs';

const LOG = '/tmp/durtest.log';
const PROMPT = 'a red balloon rising slowly into a clear blue sky';
const JOBS = process.argv.slice(2).map((s) => {
  const [d, r] = s.split(':');
  return { dur: Number(d), res: r || '720p' };
});
if (!JOBS.length) { console.error('usage: node durt-batch.mjs 8:720p 10:720p 9:480p'); process.exit(1); }

const log = (m) => { const line = new Date().toISOString() + ' [batch] ' + m; console.log(line); fs.appendFileSync(LOG, line + '\n'); };
const c = new AvmClient({ cookie: process.env.AVM_COOKIE });

log('queue: ' + JOBS.map((j) => j.dur + 's/' + j.res).join(', '));

for (const job of JOBS) {
  const tag = job.dur + 's/' + job.res;
  const deadline = Date.now() + 30 * 60_000;
  let done = false;

  while (!done && Date.now() < deadline) {
    let gate = null;
    try { gate = await c.needsCaptcha(); } catch (e) { log(tag + ' needsCaptcha err: ' + e.message); }

    if (gate === false) {
      try {
        const id = await c.create({
          content: PROMPT, duration: job.dur, aspectRatio: '16:9', resolution: job.res, tier: 'turbo',
        });
        if (!id) { log(tag + ' -> 空串（静默拒绝）'); done = true; break; }
        log(tag + ' -> taskId=' + id);
        const w = await c.waitForTask(id, { timeoutMs: 900_000, intervalMs: 15_000 });
        const t = w.task || {};
        log(tag + ' RESULT ' + w.status + ' | realDur=' + t.duration + ' res=' + t.kelingKeyId +
            ' credits=' + t.credits + ' paid=' + t.paid + ' ms=' + Math.round(w.ms / 1000) + 's url=' + (t.url || ''));
        done = true;
      } catch (e) {
        if (e.code === 'CAPTCHA_REQUIRED') { log(tag + ' gate flipped; back off 45s'); await new Promise(r => setTimeout(r, 45_000)); continue; }
        if (/queue is full/i.test(e.message)) { log(tag + ' queue full; back off 30s'); await new Promise(r => setTimeout(r, 30_000)); continue; }
        log(tag + ' -> ERROR ' + e.code + ' | ' + String(e.message).replace(/\s+/g, ' ').slice(0, 150));
        done = true;
      }
    } else {
      await new Promise(r => setTimeout(r, gate === true ? 40_000 : 20_000));
    }
  }
  if (!done) log(tag + ' TIMEOUT');
  // let the backend release the slot before the next job
  await new Promise(r => setTimeout(r, 8_000));
}
log('BATCH DONE');
