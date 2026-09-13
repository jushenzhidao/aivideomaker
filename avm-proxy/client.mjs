// Complete aivideomaker.ai client — pure HTTP, no browser, no captcha.
//
// The site gates Turnstile behind `model.needsCaptcha`.  For a subscribed
// account it returns false, and then `ai.minimaxH3` accepts `token: null`.
// That means the whole flow is plain REST with just a session cookie.
//
// Endpoints (all under https://aivideomaker.ai):
//   GET  /api/model.needsCaptcha?...        -> bool
//   POST /api/ai.minimaxH3?batch=1          -> "<taskId>"
//   GET  /api/model.listModel?...           -> { models: [...] }
//   GET  /api/model.queryQueueByModel?...   -> queue position
//   GET  /api/model-status/token?id=&visitorId=  -> { token, expiresInSec }
//   GET  /api/model-status?id=&token=&visitorId= -> text/event-stream

const ORIGIN = 'https://aivideomaker.ai';
const PAGE = '/zh/ai-video-generator';
const LIST_PAGE = '/zh/generations';

// tRPC "no argument" is encoded as null + meta marking it undefined.  Two
// shapes exist in the wild: `values: ["undefined"]` (positional) and
// `values: {foo: ["undefined"]}` (named).  auth.user wants the positional one.
const VOID_INPUT = { 0: { json: null, meta: { values: ['undefined'], v: 1 } } };

const TERMINAL = new Set(['succeed', 'success', 'completed', 'failed', 'error', 'cancelled', 'canceled']);
const SUCCESS = new Set(['succeed', 'success', 'completed']);

export class AvmClient {
  /**
   * @param {object} opts
   * @param {string} opts.cookie     raw Cookie header value
   * @param {string} [opts.userId]   defaults to AVM_USER_ID env
   * @param {string} [opts.visitorId] any 32-hex; server doesn't validate it
   */
  constructor(opts = {}) {
    this.cookie = (opts.cookie || process.env.AVM_COOKIE || '').trim();
    if (!this.cookie) throw new Error('AvmClient needs opts.cookie (or AVM_COOKIE)');
    this.userId = opts.userId || process.env.AVM_USER_ID || '';
    this.visitorId = opts.visitorId || process.env.AVM_VISITOR_ID || 'f29ee26edcb4e8b96ee17e277a384f6f';
    this._userIdPromise = null;
  }

  _headers(referer = PAGE, extra = {}) {
    return {
      'user-agent':
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
      'accept-language': 'zh-CN,zh;q=0.9',
      referer: `${ORIGIN}${referer}`,
      cookie: this.cookie,
      ...extra,
    };
  }

  /** Call a tRPC procedure; unwraps `result.data.json` or throws the error. */
  async trpc(proc, input, { method = 'GET', referer = PAGE, meta } = {}) {
    const payload = JSON.stringify(meta ?? { 0: { json: input } });
    const url = `${ORIGIN}/api/${proc}?batch=1&input=${encodeURIComponent(payload)}`;
    const r = await fetch(url, {
      method,
      headers: this._headers(referer, {
        'content-type': 'application/json',
        accept: '*/*',
        ...(method === 'POST' ? { origin: ORIGIN } : {}),
      }),
      ...(method === 'POST' ? { body: JSON.stringify({ 0: { json: input } }) } : {}),
    });
    const txt = await r.text();
    let parsed;
    try { parsed = JSON.parse(txt); } catch { throw new Error(`${proc}: non-JSON response (${r.status}) ${txt.slice(0, 200)}`); }
    const first = parsed?.[0];
    if (first?.error) {
      const e = first.error.json ?? {};
      const err = new Error(`${proc}: ${e.message}`);
      err.code = e.data?.code;
      err.httpStatus = e.data?.httpStatus ?? r.status;
      throw err;
    }
    return first?.result?.data?.json;
  }

  /** Resolve userId from the session if it wasn't supplied. */
  async getUserId() {
    if (this.userId) return this.userId;
    if (!this._userIdPromise) {
      this._userIdPromise = this.trpc('auth.user', null, { meta: VOID_INPUT }).catch(() => null);
    }
    const u = await this._userIdPromise;
    if (u?.id) this.userId = u.id;
    return this.userId;
  }

  /** Does this account need to solve Turnstile before generating? */
  async needsCaptcha() {
    const userId = await this.getUserId();
    return await this.trpc('model.needsCaptcha', { userId });
  }

  /**
   * Create a video task. Returns the task id.
   *
   * `model.needsCaptcha` is DYNAMIC — it is not a property of the account.
   * Measured on a paid subscription: false for ~7 generations inside an hour,
   * then it flips to true and every subsequent `token: null` submit is
   * silently rejected (upstream returns an empty string).  It may decay later;
   * it is re-checked on every call.
   *
   * Pass `params.token` (a real Turnstile token) to get through the gate.
   */
  async create(params) {
    const need = await this.needsCaptcha();
    const token = params.token ?? null;
    if (need && !token) {
      const err = new Error(
        'account currently requires a Turnstile captcha (model.needsCaptcha=true). ' +
        'This is a dynamic, velocity-based gate, not an account property. ' +
        'Supply a fresh Turnstile token via params.token, wait for it to decay, ' +
        'or spread the submissions out.',
      );
      err.code = 'CAPTCHA_REQUIRED';
      throw err;
    }
    const body = {
      content: params.content,
      imageUrl: params.imageUrl ?? null,
      lastFrameUrl: params.lastFrameUrl ?? null,
      referenceImageUrls: params.referenceImageUrls ?? [],
      referenceVideoUrl: params.referenceVideoUrl ?? null,
      referenceAudioUrls: params.referenceAudioUrls ?? [],
      aspectRatio: params.aspectRatio ?? '16:9',
      duration: params.duration ?? 5,
      resolution: params.resolution ?? '480p',
      tier: params.tier ?? 'turbo',
      promptEnrichment: params.promptEnrichment ?? false,
      visitorId: this.visitorId,
      token,
    };
    const id = await this.trpc('ai.minimaxH3', body, { method: 'POST' });
    if (!id) throw new Error('create returned no task id — the request was rejected');
    return id;
  }

  /** Paginated list of the account's generations. */
  async list({ offset = 0, limit = 40, sort = 'desc' } = {}) {
    const userId = await this.getUserId();
    // `createdAt` is sent as undefined-with-meta so the server treats it as "no cursor"
    const payload = {
      0: {
        json: { createdAt: null, offset, limit, createAtSort: sort, userId },
        meta: { values: { createdAt: ['undefined'] }, v: 1 },
      },
    };
    const url = `${ORIGIN}/api/model.listModel?batch=1&input=${encodeURIComponent(JSON.stringify(payload))}`;
    const r = await fetch(url, { headers: this._headers(LIST_PAGE, { accept: '*/*', 'content-type': 'application/json' }) });
    const j = await r.json();
    const data = j?.[0]?.result?.data?.json;
    if (j?.[0]?.error) throw new Error(`model.listModel: ${j[0].error.json?.message}`);
    return data;
  }

  /** Queue position / backend request id for a task. */
  async queryQueue(id) {
    return await this.trpc('model.queryQueueByModel', { id });
  }

  async mintToken(id) {
    const u = `${ORIGIN}/api/model-status/token?id=${encodeURIComponent(id)}&visitorId=${encodeURIComponent(this.visitorId)}`;
    const r = await fetch(u, { headers: this._headers(PAGE, { accept: '*/*', 'cache-control': 'no-cache', pragma: 'no-cache' }) });
    if (!r.ok) throw new Error(`model-status/token ${r.status}: ${await r.text()}`);
    const j = await r.json();
    if (!j.token) throw new Error(`no token in response: ${JSON.stringify(j)}`);
    return j;
  }

  /**
   * Remove tasks from the account's history.
   *
   * NOTE: this only deletes the *record*.  There is no cancel endpoint, so a
   * task that is still running keeps running (and keeps costing) upstream —
   * this just clears it out of the gallery.
   *
   * @param {string|string[]} ids
   */
  async deleteTasks(ids) {
    const list = Array.isArray(ids) ? ids : [ids];
    return await this.trpc('model.deleteModel', { ids: list.map(String) }, { method: 'POST' });
  }

  /** One task, no userId needed.  Best of the three ways to read a task:
   * a single request, authoritative, and it does not depend on the task being
   * inside the first page of the account list.
   */
  async getModel(id) {
    return await this.trpc('model.getModel', { id });
  }

  /**
   * Full task record.
   *
   * Three ways to read one, with real trade-offs measured against the live API:
   *
   *   model.getModel {id}  - 1 request, no userId, full record.  Best.
   *   model.listModel      - 1 request, full record, but needs userId and the
   *                          task must be inside the requested page.
   *   model-status SSE     - needs 2 requests (mint token, then stream) AND it
   *                          (a) never ends for a task upstream has not picked
   *                          up yet, and (b) rate-limits with 429 fast.
   */
  async getTask(id, { sseTimeoutMs = 12_000, preferList = true } = {}) {
    try {
      const rec = await this.getModel(id);
      if (rec) return rec;
    } catch (e) {
      if (e?.code === 'NOT_FOUND') throw e;
    }

    if (preferList) {
      try {
        const rec = await this.findInList(id);
        if (rec) return rec;
      } catch { /* fall through to SSE */ }
    }

    try {
      const { token } = await this.mintToken(id);
      const u = `${ORIGIN}/api/model-status?id=${encodeURIComponent(id)}&token=${encodeURIComponent(token)}&visitorId=${encodeURIComponent(this.visitorId)}`;
      const r = await fetch(u, {
        headers: this._headers(PAGE, { accept: 'text/event-stream', 'cache-control': 'no-cache', pragma: 'no-cache' }),
        signal: AbortSignal.timeout(sseTimeoutMs),
      });
      if (!r.ok) throw new Error(`model-status ${r.status}: ${await r.text()}`);
      const frame = parseSse(await r.text());
      if (frame) return frame.model ?? frame;
    } catch (e) {
      if (e?.code === 'NOT_FOUND') throw e;
      // transient (429 / timeout / 5xx / no frame yet) -> fall back below
    }

    const rec = await this.findInList(id).catch(() => null);
    if (!rec) {
      const err = new Error(`task ${id} not found`);
      err.code = 'NOT_FOUND';
      throw err;
    }
    return rec;
  }

  /** Look a task up in the account's generation list (finite, unlike SSE). */
  async findInList(id, { limit = 40 } = {}) {
    const data = await this.list({ limit });
    return (data?.models || []).find((m) => m.id === id) || null;
  }

  /** Poll until terminal state. */
  async waitForTask(id, { timeoutMs = 10 * 60_000, intervalMs = 10_000, onUpdate } = {}) {
    const t0 = Date.now();
    let last = null;
    while (Date.now() - t0 < timeoutMs) {
      last = await this.getTask(id);
      onUpdate?.(last);
      const st = String(last.taskStatus || '').toLowerCase();
      if (TERMINAL.has(st)) {
        return { done: true, ok: SUCCESS.has(st), status: last.taskStatus, task: last, ms: Date.now() - t0 };
      }
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    return { done: false, ok: false, status: last?.taskStatus ?? null, task: last, ms: Date.now() - t0 };
  }

  async download(task, outPath) {
    const { createWriteStream } = await import('node:fs');
    const { pipeline } = await import('node:stream/promises');
    const { Readable } = await import('node:stream');
    if (!task?.url) throw new Error('task has no url yet');
    const r = await fetch(task.url);
    if (!r.ok) throw new Error(`download ${r.status}`);
    await pipeline(Readable.fromWeb(r.body), createWriteStream(outPath));
    return outPath;
  }
}

/** Sniff the real type from magic bytes — never trust the file extension.
 *  (Real case: a `.jpg` URL serving `Content-Type: image/jpg` over actual PNG
 *  bytes.  The server rejects `image/jpg` — it is not a real MIME type.)
 *
 *  Covers the three families the upload endpoint accepts.  Measured size caps
 *  (from `maxBytes` in the presign response): image 10MB, video 50MB, audio 15MB. */
export function sniffFile(buf) {
  if (buf.length >= 8 && buf[0] === 0x89 && buf.toString('ascii', 1, 4) === 'PNG') {
    return { contentType: 'image/png', ext: 'png', kind: 'image' };
  }
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) {
    return { contentType: 'image/jpeg', ext: 'jpg', kind: 'image' };
  }
  if (buf.length >= 12 && buf.toString('ascii', 0, 4) === 'RIFF' && buf.toString('ascii', 8, 12) === 'WEBP') {
    return { contentType: 'image/webp', ext: 'webp', kind: 'image' };
  }
  // ISO base media (MP4 / MOV / M4A): 'ftyp' at offset 4, brand at offset 8
  if (buf.length >= 12 && buf.toString('ascii', 4, 8) === 'ftyp') {
    const brand = buf.toString('ascii', 8, 12);
    if (brand === 'qt  ') return { contentType: 'video/quicktime', ext: 'mov', kind: 'video' };
    if (/^M4A|^M4B/.test(brand)) return { contentType: 'audio/mp4', ext: 'm4a', kind: 'audio' };
    return { contentType: 'video/mp4', ext: 'mp4', kind: 'video' };
  }
  if (buf.length >= 4 && buf[0] === 0x1a && buf[1] === 0x45 && buf[2] === 0xdf && buf[3] === 0xa3) {
    return { contentType: 'video/webm', ext: 'webm', kind: 'video' };
  }
  if (buf.length >= 3 && buf.toString('ascii', 0, 3) === 'ID3') {
    return { contentType: 'audio/mpeg', ext: 'mp3', kind: 'audio' };
  }
  if (buf.length >= 2 && buf[0] === 0xff && (buf[1] & 0xe0) === 0xe0) {
    // ADTS AAC (0xFFF1/0xFFF9) vs MPEG audio frame sync (0xFFFB/0xFFF3 ...)
    if ((buf[1] & 0xf6) === 0xf0) return { contentType: 'audio/aac', ext: 'aac', kind: 'audio' };
    return { contentType: 'audio/mpeg', ext: 'mp3', kind: 'audio' };
  }
  if (buf.length >= 12 && buf.toString('ascii', 0, 4) === 'RIFF' && buf.toString('ascii', 8, 12) === 'WAVE') {
    return { contentType: 'audio/wav', ext: 'wav', kind: 'audio' };
  }
  if (buf.length >= 4 && buf.toString('ascii', 0, 4) === 'OggS') {
    return { contentType: 'audio/ogg', ext: 'ogg', kind: 'audio' };
  }
  return { contentType: 'application/octet-stream', ext: 'bin', kind: 'unknown' };
}

/** Back-compat alias used by the image paths. */
export function sniffImage(buf) {
  return sniffFile(buf);
}

/** PNG / JPEG dimensions, no dependency.  Returns 0x0 if unknown. */
export function imageDimensions(buf) {
  if (buf.length > 24 && buf.toString('ascii', 1, 4) === 'PNG') {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }
  if (buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8) {
    let i = 2;
    while (i < buf.length - 9) {
      if (buf[i] !== 0xff) { i++; continue; }
      const m = buf[i + 1];
      if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
        return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
      }
      const len = buf.readUInt16BE(i + 2);
      if (len < 2) { i++; continue; }
      i += 2 + len;
    }
  }
  return { width: 0, height: 0 };
}

/**
 * Re-host any media file on the site's own CDN.
 *
 * Why: `ai.minimaxH3` rejects external URLs whose Content-Type is not in its
 * allowlist (`Unsupported upload content type`).  Going through
 * `uploads.getPresignedUrl` always yields an accepted `static.img2video.ai`
 * URL.  Found in the site's JS bundle — note the plural router `uploads`.
 *
 * Handles images, video and audio; the endpoint caps them differently
 * (measured via `maxBytes`): image 10MB, video 50MB, audio 15MB.
 *
 * @param {Buffer|string} input  bytes, or an http(s) URL to fetch
 * @returns {Promise<{publicUrl,contentType,kind,size,fileName,width,height}>}
 */
AvmClient.prototype.uploadFile = async function uploadFile(input, { name, permanent = false } = {}) {
  let buf, base;
  if (Buffer.isBuffer(input)) {
    buf = input;
    base = (name || 'file').replace(/\.[^.]+$/, '');
  } else if (typeof input === 'string' && /^https?:\/\//i.test(input)) {
    const r = await fetch(input);
    if (!r.ok) throw new Error(`download ${r.status}`);
    buf = Buffer.from(await r.arrayBuffer());
    base = decodeURIComponent(input.split('?')[0].split('/').pop() || 'file').replace(/\.[^.]+$/, '');
  } else {
    throw new Error('uploadFile: input must be a Buffer or an http(s) URL');
  }

  const { contentType, ext, kind } = sniffFile(buf);
  const { width, height } = kind === 'image' ? imageDimensions(buf) : { width: 0, height: 0 };
  const fileName = `${base}.${ext}`;

  const pre = await this.trpc(
    'uploads.getPresignedUrl',
    { fileName, contentType, fileSize: buf.length, permanent },
    { method: 'POST' },
  );
  if (pre.maxBytes && buf.length > pre.maxBytes) {
    throw new Error(
      `${kind} too large: ${buf.length} bytes > maxBytes ${pre.maxBytes} (${(pre.maxBytes / 1048576).toFixed(0)}MB for ${contentType})`,
    );
  }

  const put = await fetch(pre.uploadUrl, {
    method: 'PUT',
    headers: { ...(pre.headers || {}), 'Content-Type': contentType, 'Content-Length': String(buf.length) },
    body: buf,
  });
  if (!put.ok) throw new Error(`upload PUT ${put.status}: ${(await put.text()).slice(0, 300)}`);

  return { publicUrl: pre.publicUrl, contentType, kind, size: buf.length, fileName, width, height };
};

/** Image-only wrapper — the image paths rely on getting dimensions back. */
AvmClient.prototype.uploadImage = async function uploadImage(input, opts = {}) {
  const r = await this.uploadFile(input, opts);
  if (r.kind !== 'image') {
    throw new Error(`uploadImage: expected an image, got ${r.contentType}`);
  }
  return r;
};

/** First `data: {...}` frame of an SSE body. */
export function parseSse(text) {
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t.startsWith('data:')) continue;
    const payload = t.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try { return JSON.parse(payload); } catch { /* keep scanning */ }
  }
  return null;
}
