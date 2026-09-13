import fs from 'node:fs';
import { AvmClient } from './client.mjs';

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const l = await c.list({ limit: 100 });
const ms = (l.models || []).sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));

const rows = ms.map((m) => ({
  id: m.id,
  createdAt: m.createdAt,
  completedAt: m.completedAt,
  status: m.taskStatus,
  source: m.source,
  content: m.content,
  resolution: m.kelingKeyId,
  duration: m.duration,
  aspectRatio: m.aspectRatio,
  credits: m.credits,
  paid: m.paid,
  url: m.url,
  cover: m.cover,
}));

fs.writeFileSync('/tmp/all-tasks.json', JSON.stringify(rows, null, 2));
console.log('任务总数 total=' + l.total, ' 取回=' + rows.length);
let free = 0, billed = 0;
for (const r of rows) { if (r.paid === true) billed++; else if (r.paid === false) free++; }
console.log('paid=false(免费):', free, ' paid=true(计费):', billed);
console.log('有 url 的:', rows.filter((r) => r.url).length);
console.log('\nid'.padEnd(19), '时间'.padEnd(22), 'res'.padEnd(5), 'dur'.padEnd(4), 'paid'.padEnd(6), 'url');
for (const r of rows) {
  console.log(
    r.id.padEnd(19) + r.createdAt.padEnd(22) + String(r.resolution).padEnd(5) +
    String(r.duration).padEnd(4) + String(r.paid).padEnd(6) + String(r.url || '').slice(0, 74),
  );
}
