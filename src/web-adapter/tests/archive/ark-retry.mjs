// Retry an Ark-format create through the adapter until the captcha gate opens.
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';
const body = JSON.parse(process.argv[2]);
const DEADLINE = Date.now() + 45 * 60_000;

while (Date.now() < DEADLINE) {
  const h = await fetch(`${BASE}/healthz`).then((r) => r.json()).catch(() => ({}));
  if (h.needsCaptcha === false) {
    const r = await fetch(`${BASE}/api/v3/contents/generations/tasks`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.id) { console.log('CREATED ' + j.id); process.exit(0); }
    if (j.error?.code === 'RateLimitExceeded') {
      console.log('gate flipped mid-flight; retrying in 45s');
    } else {
      console.log('ERROR ' + JSON.stringify(j));
      process.exit(1);
    }
  } else {
    console.log(new Date().toISOString(), 'gate closed (needsCaptcha=' + h.needsCaptcha + '), waiting 45s');
  }
  await new Promise((r) => setTimeout(r, 45_000));
}
console.log('TIMEOUT');
process.exit(1);
