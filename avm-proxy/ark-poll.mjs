// Poll the Ark-compatible endpoint until the task is terminal.
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788/api/v3';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';
const id = process.argv[2];
if (!id) { console.error('usage: node ark-poll.mjs <cgt-id>'); process.exit(1); }

const t0 = Date.now();
let last = null;
while (Date.now() - t0 < 600_000) {
  const r = await fetch(`${BASE}/contents/generations/tasks/${id}`, {
    headers: { Authorization: `Bearer ${KEY}` },
  });
  const j = await r.json();
  last = j;
  console.log(new Date().toISOString(), 'status =', j.status);
  if (['succeeded', 'failed', 'cancelled', 'expired'].includes(j.status)) break;
  await new Promise((r) => setTimeout(r, 12_000));
}
console.log('\n=== 最终 Ark 任务对象 ===');
console.log(JSON.stringify(last, null, 2));
