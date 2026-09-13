// aivideomaker.ai reverse proxy — pure HTTP, no browser.
//
// For a subscribed account `model.needsCaptcha` returns false and
// `ai.minimaxH3` accepts `token: null`, so nothing here needs Cloudflare
// Turnstile or a real Chrome.
//
// Routes:
//   GET  /healthz                     liveness + account + captcha requirement
//   POST /generate[?wait=1&download=] create a task (optionally block + save)
//   GET  /tasks[?offset=&limit=]      list the account's generations
//   GET  /task/:id                    one task record
//   GET  /task/:id/wait               poll until terminal
//   GET  /task/:id/queue              queue position
//
// env:
//   AVM_COOKIE   raw Cookie header value (required)
//   AVM_USER_ID  optional; auto-resolved via auth.user
//   AVM_KEY      optional; if set, /generate uses the official /api/v1 API
//   PORT         8787

import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { AvmClient } from '../../client.mjs';

const PORT = +(process.env.PORT || 8787);

async function loadCookie() {
  if (process.env.AVM_COOKIE) return process.env.AVM_COOKIE.trim();
  const f = process.env.COOKIES_FILE || './cookies.json';
  if (!existsSync(f)) return '';
  try {
    const arr = JSON.parse(await readFile(f, 'utf8'));
    return arr.filter((c) => c.name && c.value).map((c) => `${c.name}=${c.value}`).join('; ');
  } catch {
    return '';
  }
}

const cookie = await loadCookie();
if (!cookie) {
  console.error('no cookie. Set AVM_COOKIE or create cookies.json');
  process.exit(1);
}

const avm = new AvmClient({ cookie, userId: process.env.AVM_USER_ID });
const useOfficialApi = !!process.env.AVM_KEY;

// warm up: resolve userId + captcha requirement so /healthz can report it
const boot = { userId: null, needsCaptcha: null, error: null };
try {
  boot.userId = await avm.getUserId();
  boot.needsCaptcha = await avm.needsCaptcha();
} catch (e) {
  boot.error = e.message;
}
console.log(`[avm-proxy] userId=${boot.userId} needsCaptcha=${boot.needsCaptcha} officialApi=${useOfficialApi}`);

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://x');
    const p = url.pathname;

    if (req.method === 'GET' && p === '/healthz') {
      return send(res, 200, {
        ok: true,
        userId: boot.userId,
        needsCaptcha: boot.needsCaptcha,
        visitorId: avm.visitorId,
        mode: useOfficialApi ? 'api' : 'web',
        bootError: boot.error,
      });
    }

    if (req.method === 'GET' && p === '/tasks') {
      const data = await avm.list({
        offset: +(url.searchParams.get('offset') || 0),
        limit: +(url.searchParams.get('limit') || 40),
        sort: url.searchParams.get('sort') || 'desc',
      });
      return send(res, 200, data);
    }

    if (req.method === 'POST' && p === '/generate') {
      const body = await readJsonBody(req);
      if (!body?.content) return err(res, 400, 'content is required');
      return handleGenerate(body, url, res);
    }

    let m = p.match(/^\/task\/([A-Za-z0-9_:-]+)$/);
    if (m && req.method === 'GET') {
      const task = await avm.getTask(m[1]);
      return send(res, 200, { id: m[1], taskStatus: task.taskStatus, url: task.url, cover: task.cover, task });
    }

    m = p.match(/^\/task\/([A-Za-z0-9_:-]+)\/wait$/);
    if (m && req.method === 'GET') {
      const w = await avm.waitForTask(m[1], {
        timeoutMs: +(url.searchParams.get('timeoutMs') || 600_000),
        intervalMs: +(url.searchParams.get('intervalMs') || 10_000),
      });
      return send(res, 200, { id: m[1], ...w });
    }

    m = p.match(/^\/task\/([A-Za-z0-9_:-]+)\/queue$/);
    if (m && req.method === 'GET') {
      const q = await avm.queryQueue(m[1]);
      return send(res, 200, { id: m[1], queue: q });
    }

    m = p.match(/^\/task\/([A-Za-z0-9_:-]+)\/cancel$/);
    if (m) return err(res, 404, 'aivideomaker.ai exposes no cancel endpoint for this task type');

    err(res, 404, 'not found');
  } catch (e) {
    console.error('[avm-proxy]', e.message);
    err(res, e.code === 'CAPTCHA_REQUIRED' ? 403 : 500, e.message);
  }
});

server.listen(PORT, () => console.log(`[avm-proxy] listening on :${PORT}`));

async function handleGenerate(params, url, res) {
  const t0 = Date.now();

  if (useOfficialApi) {
    const r = await fetch('https://aivideomaker.ai/api/v1/generate/minimax', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': 'Mozilla/5.0', key: process.env.AVM_KEY },
      body: JSON.stringify(buildBody(params)),
    });
    const txt = await r.text();
    let parsed; try { parsed = JSON.parse(txt); } catch { parsed = txt; }
    return send(res, 200, { mode: 'api', ms: Date.now() - t0, upstreamStatus: r.status, upstream: parsed });
  }

  const taskId = await avm.create(params);
  const out = { mode: 'web', taskId, ms: Date.now() - t0 };

  if (url.searchParams.get('wait') === '1') {
    const w = await avm.waitForTask(taskId, { timeoutMs: +(url.searchParams.get('timeoutMs') || 600_000) });
    out.wait = { done: w.done, ok: w.ok, status: w.status, ms: w.ms };
    out.taskStatus = w.task?.taskStatus;
    out.url = w.task?.url;
    if (w.ok && url.searchParams.get('download')) {
      await avm.download(w.task, url.searchParams.get('download'));
      out.downloadedTo = url.searchParams.get('download');
    } else if (w.ok) {
      out.task = w.task;
    }
    out.ms = Date.now() - t0;
  }
  send(res, 200, out);
}

function buildBody(p) {
  return {
    content: p.content,
    imageUrl: p.imageUrl ?? null,
    lastFrameUrl: p.lastFrameUrl ?? null,
    referenceImageUrls: p.referenceImageUrls ?? [],
    referenceVideoUrl: p.referenceVideoUrl ?? null,
    referenceAudioUrls: p.referenceAudioUrls ?? [],
    aspectRatio: p.aspectRatio ?? '16:9',
    duration: p.duration ?? 5,
    resolution: p.resolution ?? '480p',
    tier: p.tier ?? 'turbo',
    promptEnrichment: p.promptEnrichment ?? false,
  };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const cs = [];
    req.on('data', (c) => cs.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(cs).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch { reject(new Error('invalid JSON body')); }
    });
    req.on('error', reject);
  });
}
function send(res, code, obj) { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj, null, 2)); }
function err(res, code, msg) { send(res, code, { error: msg }); }
