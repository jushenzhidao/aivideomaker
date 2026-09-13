import fs from 'node:fs';
import { AvmClient } from './client.mjs';

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const st = JSON.parse(fs.readFileSync('/tmp/durtest.json', 'utf8'));
const out = [];
for (const [d, id] of Object.entries(st)) {
  const t = await c.getModel(id);
  out.push({
    req: d + 's', taskId: id, status: t.taskStatus, realDur: t.duration, res: t.kelingKeyId,
    credits: t.credits, paid: t.paid, url: t.url,
  });
}
console.log(JSON.stringify(out, null, 2));
fs.writeFileSync('/tmp/dur-results.json', JSON.stringify(out, null, 2));
