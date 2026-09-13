// Protocol adapter — expose aivideomaker.ai through two standard shapes:
//
//   OpenAI Responses API  (POST /v1/responses, GET /v1/responses/:id, SSE stream)
//   MiniMax official API  (POST /v1/video_generation,
//                          GET  /v1/query/video_generation?task_id=,
//                          GET  /v1/files/retrieve?file_id=)
//
// The two never collide on paths, so both are mounted on one port under /v1.
// All of it is a thin translation layer over AvmClient (client.mjs) — no
// browser, no captcha, just the session cookie.
//
// env:
//   AVM_COOKIE     raw Cookie header (required)
//   AVM_USER_ID    optional, auto-resolved via auth.user
//   AVM_VISITOR_ID optional, any 32-hex (server does not validate)
//   AVM_GATE_KEY   optional; if set, /v1/* requires `Authorization: Bearer <it>`
//   PORT           8788

import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AvmClient, imageDimensions } from './client.mjs';
import { SubmitQueue } from './submit-queue.mjs';

const PORT = +(process.env.PORT || 8788);
const GATE = (process.env.AVM_GATE_KEY || '').trim();
const POLL_MS = +(process.env.AVM_POLL_MS || 8000);
const DEFAULT_TIMEOUT_MS = +(process.env.AVM_TIMEOUT_MS || 15 * 60_000);

// ---------------------------------------------------------------- cookie ----

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

// Upstream only runs 1-2 tasks at a time (premium = 2).  Everything queued here
// waits for a slot instead of failing, and "queue is full" is retried with
// backoff.  See submit-queue.mjs.
const submitQ = new SubmitQueue({
  avm,
  maxConcurrent: +(process.env.AVM_MAX_CONCURRENT || 2),
  retryCaptcha: process.env.AVM_RETRY_CAPTCHA === '1',
  baseDelayMs: +(process.env.AVM_QUEUE_BASE_DELAY_MS || 20_000),
  maxAttempts: +(process.env.AVM_QUEUE_MAX_ATTEMPTS || 8),
  log: (m) => console.warn(`[adapter] ${m}`),
});

// ------------------------------------------------------------- mappings ----

// aivideomaker tier lives in the `tier` field.  Upstream zod enum is exactly
// 'turbo' | 'base' (verified against the live API — 'normal' is rejected).
//
// BILLING (measured across 30+ tasks): `base` is ALWAYS billed (record comes
// back paid=true), and `turbo` is billed only when duration >= 9s.  Because
// nobody wants a proxy that silently spends money, every model name defaults
// to `turbo`; `base` must be asked for explicitly via `tier:"base"` or
// `extra_body.aivideomaker_tier:"base"`.
const MODEL_MAP = {
  'minimax-h3': 'turbo',
  'minimax-h3-turbo': 'turbo',
  'minimax-h3-base': 'base',
  'minimax-h3-pro': 'base',
  'minimax-hailuo-02': 'turbo',
  'minimax-hailuo-2.3': 'turbo',
};
const DEFAULT_MODEL = 'minimax-h3';
const TIERS = new Set(['turbo', 'base']);

/** Free only when tier=turbo AND duration <= this. */
const FREE_MAX_DURATION = 8;

const RESOLUTIONS = ['480p', '720p', '1080p'];
const ASPECTS = ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9'];

/** aivideomaker status -> OpenAI Responses status */
const TO_OPENAI = {
  submitted: 'queued',
  pending: 'queued',
  preparing: 'queued',
  queueing: 'queued',
  queued: 'queued',
  processing: 'in_progress',
  running: 'in_progress',
  in_progress: 'in_progress',
  succeed: 'completed',
  success: 'completed',
  completed: 'completed',
  failed: 'failed',
  fail: 'failed',
  error: 'failed',
  cancelled: 'failed',
  canceled: 'failed',
};

/** aivideomaker status -> MiniMax enum */
const TO_MINIMAX = {
  submitted: 'Preparing',
  pending: 'Preparing',
  preparing: 'Preparing',
  queueing: 'Queueing',
  queued: 'Queueing',
  processing: 'Processing',
  running: 'Processing',
  in_progress: 'Processing',
  succeed: 'Success',
  success: 'Success',
  completed: 'Success',
  failed: 'Fail',
  fail: 'Fail',
  error: 'Fail',
  cancelled: 'Fail',
  canceled: 'Fail',
};

function normStatus(s) {
  return TO_OPENAI[String(s || 'submitted').toLowerCase()] || 'queued';
}
function normMinimax(s) {
  return TO_MINIMAX[String(s || 'submitted').toLowerCase()] || 'Preparing';
}

/** MiniMax base_resp codes.  0 ok, 1002 rate limit, 1004 auth, 1008 balance, 1026 content. */
function baseResp(code = 0, msg = 'success') {
  return { status_code: code, status_msg: msg };
}

function classify(err) {
  const m = String(err?.message || err);
  const c = err?.code;
  if (c === 'CAPTCHA_REQUIRED') return baseResp(1002, m);
  if (c === 'UNAUTHORIZED' || err?.httpStatus === 401) return baseResp(1004, m);
  if (c === 'NOT_FOUND') return baseResp(2013, m);
  if (/balance|insufficient|quota|credits/i.test(m)) return baseResp(1008, m);
  if (/sensitive|policy|moderat|nsfw/i.test(m)) return baseResp(1026, m);
  if (c === 'BAD_REQUEST' || /too_big|invalid|expected/i.test(m)) return baseResp(2013, m);
  return baseResp(1000, m);
}

// ------------------------------------------------------------ param glue ----

/**
 * Duration rules, measured against the live API — there are TWO layers:
 *
 *   zod    accepts any number 5..20 (rejects with too_big/too_small)
 *   server then re-checks per resolution, e.g. 480p returns
 *   INTERNAL_SERVER_ERROR "480p supports 5s, 10s, 15s, or 20s duration."
 *
 * So blanket-clamping to 5..20 produces real failures.  Per-resolution:
 *   480p  -> {5,10,15,20}   (verified: 6 and 7 are rejected)
 *   720p  -> any 5..20      (verified: 6 accepted)
 *   1080p -> {5,10}         (only these two verified; 15/20 unknown)
 * `null` means "no extra restriction".
 */
const DURATION_ALLOWED = {
  '480p': [5, 10, 15, 20],
  '720p': null,
  '1080p': [5, 10],
};

function snapDuration(v, resolution, preferFree = false) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 5;
  const r = Math.min(20, Math.max(5, Math.round(n)));
  const allowed = DURATION_ALLOWED[resolution];
  if (!allowed) return r;
  // `prefer_free` picks the longest option that stays inside the free window
  // instead of the numerically nearest one.  This matters: at 480p an 8s
  // request snaps UP to 10s (nearest) which crosses into the billed range,
  // while 5s is free.
  if (preferFree) {
    const free = allowed.filter((x) => x <= FREE_MAX_DURATION && x <= r);
    if (free.length) return free[free.length - 1];
  }
  return allowed.reduce((a, b) => (Math.abs(b - r) < Math.abs(a - r) ? b : a));
}

function normResolution(v) {
  const s = String(v || '').toLowerCase().replace(/\s/g, '');
  if (RESOLUTIONS.includes(s)) return s;
  if (/^(768|720)p?$/.test(s)) return '720p';
  if (/^(1080|1920)p?$/.test(s)) return '1080p';
  return '480p';
}

function normAspect(v) {
  const s = String(v || '').replace(/\s/g, '');
  if (ASPECTS.includes(s)) return s;
  const m = s.match(/^(\d+)\s*[:\/x×]\s*(\d+)$/i);
  if (m) {
    const [a, b] = [+m[1], +m[2]];
    for (const cand of ASPECTS) {
      const [x, y] = cand.split(':').map(Number);
      if (Math.abs(a / b - x / y) < 0.02) return cand;
    }
  }
  return '16:9';
}

/** "1920x1080" -> { aspectRatio, resolution } */
function parseSize(size) {
  const m = String(size || '').match(/^(\d{2,5})\s*[x×]\s*(\d{2,5})$/i);
  if (!m) return null;
  const [w, h] = [+m[1], +m[2]];
  const res = h >= 1080 ? '1080p' : h >= 720 ? '720p' : '480p';
  return { aspectRatio: normAspect(`${w}:${h}`), resolution: res };
}

function videoSize(aspectRatio, resolution) {
  const h = resolution === '1080p' ? 1080 : resolution === '720p' ? 720 : 480;
  const [x, y] = aspectRatio.split(':').map(Number);
  const w = Math.round((h * x) / y);
  const even = (n) => n - (n % 2);
  return { video_width: even(w), video_height: even(h) };
}

/**
 * Upstream hard-rejects mixing the two task kinds:
 *   "Cannot mix reference assets with first/last frame.
 *    Use either frame-to-video or reference-to-video"
 * Catch it locally so the caller gets a clear 400 instead of a round trip.
 */
function assertTaskKindIsPure({ hasFrame, hasReference }) {
  if (hasFrame && hasReference) {
    const e = new Error(
      'cannot mix reference assets with first/last frame: upstream requires either ' +
        'frame-to-video (first_frame / last_frame only) or reference-to-video ' +
        '(reference_image / reference_video / reference_audio only), not both',
    );
    e.code = 'MIXED_TASK_KIND';
    throw e;
  }
}

/** Does the request carry anything at all to generate from? */
function hasAnyInput(p) {
  return !!(
    p.content ||
    p.imageUrl ||
    p.lastFrameUrl ||
    (p.referenceImageUrls && p.referenceImageUrls.length) ||
    p.referenceVideoUrl ||
    (p.referenceAudioUrls && p.referenceAudioUrls.length)
  );
}

/**
 * Translate a client payload (OpenAI-ish or MiniMax-ish — they overlap enough
 * that one parser handles both) into AvmClient.create() params.
 */
function translate(body = {}) {
  const model = String(body.model || DEFAULT_MODEL).trim();
  // Explicit `tier` always wins; otherwise the model-name table; else turbo (free-ish).
  const explicitTier = String(body.tier || body.extra_body?.aivideomaker_tier || '').toLowerCase();
  const tier = TIERS.has(explicitTier)
    ? explicitTier
    : MODEL_MAP[model.toLowerCase()] || 'turbo';

  const size = parseSize(body.size || body.video_size);
  let aspectRatio = normAspect(body.aspect_ratio ?? body.aspectRatio ?? size?.aspectRatio);
  let resolution = normResolution(body.resolution ?? size?.resolution ?? body.quality);

  // `content` is the site's OWN field name (it is what the web client sends),
  // so it must be accepted alongside prompt/input.
  const prompt =
    body.prompt ??
    body.content ??
    (typeof body.input === 'string' ? body.input : null) ??
    extractInputText(body.input) ??
    '';

  const firstFrame =
    body.first_frame_image ?? body.image ?? body.imageUrl ?? body.image_url ?? null;
  const lastFrame =
    body.last_frame_image ?? body.lastFrameUrl ?? body.last_frame_url ?? null;

  let referenceImageUrls = [];
  if (Array.isArray(body.reference_image_urls)) referenceImageUrls = body.reference_image_urls.filter(Boolean);
  else if (Array.isArray(body.referenceImageUrls)) referenceImageUrls = body.referenceImageUrls.filter(Boolean);
  else if (Array.isArray(body.subject_reference)) {
    // MiniMax S2V: subject_reference: [{type, image: [url]}]
    for (const s of body.subject_reference) {
      if (Array.isArray(s?.image)) referenceImageUrls.push(...s.image.filter(Boolean));
    }
  }
  // 参考素材 / omni-reference: video and audio references.
  const referenceVideoUrl =
    body.reference_video_url ?? body.referenceVideoUrl ?? null;
  let referenceAudioUrls = [];
  if (Array.isArray(body.reference_audio_urls)) referenceAudioUrls = body.reference_audio_urls.filter(Boolean);
  else if (Array.isArray(body.referenceAudioUrls)) referenceAudioUrls = body.referenceAudioUrls.filter(Boolean);
  else if (body.reference_audio_url) referenceAudioUrls = [body.reference_audio_url];

  assertTaskKindIsPure({
    hasFrame: !!(firstFrame || lastFrame),
    hasReference: referenceImageUrls.length > 0 || !!referenceVideoUrl || referenceAudioUrls.length > 0,
  });

  const preferFree = body?.extra_body?.aivideomaker_prefer_free === true;
  const duration = snapDuration(
    body.seconds ?? body.duration ?? body.duration_seconds ?? 5,
    resolution,
    preferFree,
  );
  const promptEnrichment = !!(body.prompt_optimizer ?? body.promptEnrichment ?? false);

  return {
    model,
    params: {
      content: prompt,
      imageUrl: firstFrame || null,
      lastFrameUrl: lastFrame || null,
      referenceImageUrls: referenceImageUrls.slice(0, 4),
      referenceVideoUrl: referenceVideoUrl || null,
      referenceAudioUrls: referenceAudioUrls.slice(0, 4),
      aspectRatio,
      duration,
      resolution,
      tier,
      promptEnrichment,
      // bring-your-own Turnstile token, for when needsCaptcha is true
      token:
        body.captcha_token ??
        body.turnstile_token ??
        body.token ??
        (process.env.AVM_TURNSTILE_TOKEN || null),
    },
  };
}

/** OpenAI Responses `input` may be a string or an array of content parts. */
function extractInputText(input) {
  if (!Array.isArray(input)) return null;
  const parts = [];
  for (const it of input) {
    if (typeof it === 'string') parts.push(it);
    else if (it?.type === 'input_text' || it?.type === 'text') parts.push(it.text ?? '');
    else if (it?.type === 'message' && Array.isArray(it.content)) parts.push(extractInputText(it.content) ?? '');
  }
  return parts.filter(Boolean).join('\n') || null;
}

function extractInputImages(input) {
  if (!Array.isArray(input)) return [];
  const out = [];
  for (const it of input) {
    if (it?.type === 'input_image' && (it.image_url || it.imageUrl)) {
      out.push(it.image_url ?? it.imageUrl);
    }
  }
  return out;
}

// ------------------------------------------------------- response store ----

/** resp_xxx <-> aivideomaker task id.  In-memory; restart loses history. */
const RESPONSES = new Map();

function newId() {
  return 'resp_' + randomUUID().replace(/-/g, '').slice(0, 24);
}

function remember(entry) {
  RESPONSES.set(entry.id, entry);
  return entry;
}

function entryForTaskId(taskId) {
  for (const e of RESPONSES.values()) if (e.taskId === taskId) return e;
  return null;
}

// ------------------------------------------------- OpenAI response object --

/** The task record carries its real resolution in `kelingKeyId` ("480"/"1080"). */
function resolutionOf(task, fallback) {
  const k = String(task?.kelingKeyId || '').trim();
  if (/^\d{3,4}$/.test(k)) {
    const s = `${k}p`;
    if (RESOLUTIONS.includes(s)) return s;
    return normResolution(s);
  }
  return fallback || '480p';
}

function sizeOf(task, entry) {
  const aspectRatio = normAspect(task?.aspectRatio || entry?.params?.aspectRatio);
  const resolution = resolutionOf(task, entry?.params?.resolution);
  return { aspectRatio, resolution, ...videoSize(aspectRatio, resolution) };
}

function buildResponseObject(entry, task) {
  const status = task ? normStatus(task.taskStatus) : 'queued';
  const { aspectRatio, resolution, video_width, video_height } = sizeOf(task, entry);
  const url = task?.url || null;
  const outId = 'vgen_' + entry.taskId;

  const call = {
    type: 'video_generation_call',
    id: outId,
    status,
    // NOTE: OpenAI's image_generation_call puts a base64 string here.  For
    // video there is no official shape yet, so `result` is an object — this is
    // the one deliberate deviation from the spec.  The URL is duplicated in
    // output_text below so naive `output_text` readers still work.
    result: url
      ? {
          url,
          cover: task?.cover ?? null,
          duration_seconds: Number(task?.duration || entry.params.duration),
          width: video_width,
          height: video_height,
          aspect_ratio: aspectRatio,
          resolution,
          task_id: entry.taskId,
          file_id: entry.taskId,
          created_at: task?.createdAt ?? null,
          completed_at: task?.completedAt ?? null,
          credits: task?.credits ?? null,
        }
      : null,
  };

  const text =
    status === 'completed' && url
      ? url
      : status === 'failed'
        ? `video generation failed: ${task?.taskStatusMsg || task?.taskStatus || 'unknown error'}`
        : `video generation ${status} (task ${entry.taskId})`;

  const message = {
    type: 'message',
    id: 'msg_' + entry.id.slice(5),
    status: status === 'failed' ? 'incomplete' : status === 'completed' ? 'completed' : 'in_progress',
    role: 'assistant',
    content: [{ type: 'output_text', text, annotations: [] }],
  };

  const createdAt = Math.floor((entry.createdAtMs ?? Date.now()) / 1000);

  return {
    id: entry.id,
    object: 'response',
    created_at: createdAt,
    status,
    error:
      status === 'failed'
        ? { code: 'video_generation_failed', message: task?.taskStatusMsg || 'generation failed' }
        : null,
    incomplete_details: null,
    instructions: null,
    metadata: entry.metadata ?? {},
    model: entry.model,
    output: [call, message],
    output_text: text,
    parallel_tool_calls: false,
    previous_response_id: null,
    store: true,
    temperature: 1,
    top_p: 1,
    truncation: 'disabled',
    user: null,
    usage: {
      input_tokens: 0,
      input_tokens_details: { cached_tokens: 0 },
      output_tokens: 0,
      output_tokens_details: { reasoning_tokens: 0 },
      total_tokens: 0,
      // extension: aivideomaker bills in credits
      credits: task?.credits ?? null,
    },
    // extensions
    task_id: entry.taskId,
    aivideomaker: task ?? null,
  };
}

// ==================================================================== //
//  Volcengine Ark / Doubao Seedance native API                          //
//  POST   /api/v3/contents/generations/tasks                            //
//  GET    /api/v3/contents/generations/tasks/{id}                       //
//  DELETE /api/v3/contents/generations/tasks/{id}                       //
//  GET    /api/v3/contents/generations/tasks            (list, partial) //
//                                                                        //
//  Point an Ark SDK at base_url = http://<host>:<port>/api/v3           //
// ==================================================================== //

const ARK_BASE = '/api/v3';

// Ark task status enum, as observed in the official docs' polling example.
const TO_ARK = {
  submitted: 'queued',
  pending: 'queued',
  preparing: 'queued',
  queueing: 'queued',
  queued: 'queued',
  processing: 'running',
  running: 'running',
  in_progress: 'running',
  succeed: 'succeeded',
  success: 'succeeded',
  completed: 'succeeded',
  failed: 'failed',
  fail: 'failed',
  error: 'failed',
  cancelled: 'cancelled',
  canceled: 'cancelled',
};

// Ark `ratio` enum (per the Seedance 2.5 docs): 16:9 4:3 1:1 3:4 9:16 21:9 adaptive
const ARK_RATIOS = new Set(['16:9', '4:3', '1:1', '3:4', '9:16', '21:9', 'adaptive']);
const ARK_RESOLUTIONS = new Set(['480p', '720p', '1080p']);
const ARK_OUTPUT_FORMATS = new Set(['mp4', 'mov']);

// Seedance model id -> aivideomaker tier.
//
// Deliberately NOT mapped to `base`: `base` is always billed, and silently
// turning a Seedance-2.5 request into a paid generation would be a nasty
// surprise.  Everything defaults to `turbo`; pass
// `extra_body.aivideomaker_tier = "base"` to opt in.
const ARK_MODEL_TIER = [];

/** Ark task ids look like `cgt-20260414114820-abcdef`. */
function newArkId() {
  const d = new Date();
  const p = (n, w = 2) => String(n).padStart(w, '0');
  const stamp =
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
    `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  return `cgt-${stamp}-${randomUUID().replace(/-/g, '').slice(0, 8)}`;
}

/** cgt id -> { taskId, model, requested, effective, params, createdAtMs } */
const ARK_TASKS = new Map();

function arkError(res, httpStatus, code, message, param = null) {
  return send(res, httpStatus, {
    error: { code, message, param: param ?? '', type: 'InvalidRequest' },
  });
}

/** Decode a `data:image/png;base64,...` URI into bytes, or null. */
function decodeDataUri(u) {
  const m = String(u || '').match(/^data:([^;,]+);base64,(.*)$/s);
  if (!m) return null;
  try { return { mime: m[1], buf: Buffer.from(m[2], 'base64') }; } catch { return null; }
}

/**
 * Translate an Ark create-task body into AvmClient.create() params.
 * Returns { model, params, requested, effective, warnings, unsupported }.
 */
function translateArk(body = {}) {
  const warnings = [];
  const unsupported = [];

  const model = String(body.model || '').trim();
  if (!model) throw Object.assign(new Error('model is required'), { arkCode: 'MissingParameter', param: 'model' });

  const items = Array.isArray(body.content) ? body.content : [];
  if (!items.length) throw Object.assign(new Error('content is required'), { arkCode: 'MissingParameter', param: 'content' });

  const texts = [];
  const firstFrames = [];
  const lastFrames = [];
  const refImages = [];
  const refVideos = [];
  const refAudios = [];

  for (const it of items) {
    const type = String(it?.type || '');
    if (type === 'text') {
      if (it.text) texts.push(String(it.text));
      continue;
    }
    const url = it?.image_url?.url ?? it?.video_url?.url ?? it?.audio_url?.url ?? null;
    const role = String(it?.role || '').trim();
    if (type === 'image_url') {
      if (role === 'first_frame') firstFrames.push(url);
      else if (role === 'last_frame') lastFrames.push(url);
      else refImages.push(url); // reference_image, or no role at all
    } else if (type === 'video_url') {
      refVideos.push(url);
    } else if (type === 'audio_url') {
      refAudios.push(url);
    } else {
      unsupported.push(`content[].type="${type}"`);
    }
  }

  assertTaskKindIsPure({
    hasFrame: firstFrames.length > 0 || lastFrames.length > 0,
    hasReference: refImages.length > 0 || refVideos.length > 0 || refAudios.length > 0,
  });

  // Site-side proxy parameters we cannot honour — surface them instead of
  // silently dropping, so SDK callers can see what did not apply.
  const PASSTHROUGH_UNSUPPORTED = [
    'watermark', 'generate_audio', 'seed', 'camera_fixed', 'return_last_frame',
    'draft', 'service_tier', 'priority', 'callback_url', 'safety_identifier',
    'tools', 'omni_reference_task_type', 'execution_expires_after', 'output_format',
  ];
  for (const k of PASSTHROUGH_UNSUPPORTED) {
    if (body[k] !== undefined) unsupported.push(k);
  }

  // ---- ratio -> aspectRatio -------------------------------------------
  let ratio = String(body.ratio ?? '').trim();
  if (ratio && !ARK_RATIOS.has(ratio)) {
    throw Object.assign(new Error(`ratio: invalid enum value "${ratio}"`), { arkCode: 'InvalidParameter', param: 'ratio' });
  }
  let aspectRatio; // undefined means "derive from the image"
  if (ratio && ratio !== 'adaptive') aspectRatio = normAspect(ratio);

  // ---- resolution ------------------------------------------------------
  let resolution = '720p';
  if (body.resolution !== undefined && body.resolution !== null && body.resolution !== '') {
    const r = String(body.resolution).trim();
    if (!ARK_RESOLUTIONS.has(r)) {
      throw Object.assign(new Error(`resolution: invalid enum value "${r}"`), { arkCode: 'InvalidParameter', param: 'resolution' });
    }
    resolution = r;
  }

  // ---- duration (Ark: -1 = intelligent, else 4..30) --------------------
  let duration = null;
  if (body.duration !== undefined && body.duration !== null) {
    const d = Number(body.duration);
    if (d === -1) {
      duration = 5;
      warnings.push('duration=-1 (intelligent) mapped to 5s');
    } else if (Number.isFinite(d)) {
      duration = d;
    }
  } else if (body.frames !== undefined && body.frames !== null) {
    // Ark allows frames instead of duration; the site only knows seconds.
    duration = Math.max(1, Math.round(Number(body.frames) / 24));
    warnings.push(`frames=${body.frames} converted to ${duration}s at 24fps`);
  }
  const preferFree = body?.extra_body?.aivideomaker_prefer_free === true;
  const requestedDuration = duration;
  duration = snapDuration(duration ?? 5, resolution, preferFree);
  if (requestedDuration !== null && requestedDuration !== duration) {
    let msg = `duration ${requestedDuration}s snapped to ${duration}s (site limit for ${resolution})`;
    if (duration > FREE_MAX_DURATION && requestedDuration <= FREE_MAX_DURATION) {
      msg += `; this crosses into the billed range — set extra_body.aivideomaker_prefer_free=true to snap down to a free duration instead`;
    }
    warnings.push(msg);
  }

  if (aspectRatio && firstFrames.length && ratio !== 'adaptive') {
    warnings.push('Ark requires ratio="adaptive" when role=first_frame; the site derives the ratio from the image anyway');
  }
  if (refVideos.length) {
    // The create body does carry referenceVideoUrl, but we have not verified
    // that the upstream honours it (editing / extension / motion transfer).
    warnings.push('reference_video is forwarded as referenceVideoUrl but upstream support is unverified');
  }
  if (refAudios.length) {
    warnings.push('reference_audio is forwarded as referenceAudioUrls but upstream support is unverified');
  }

  const params = {
    content: texts.join('\n').trim(),
    imageUrl: firstFrames[0] ?? null,
    lastFrameUrl: lastFrames[0] ?? null,
    referenceImageUrls: (firstFrames.length > 1 ? firstFrames.slice(1) : []).concat(refImages).filter(Boolean).slice(0, 4),
    referenceVideoUrl: refVideos[0] ?? null,
    referenceAudioUrls: refAudios.filter(Boolean).slice(0, 4),
    ...(aspectRatio ? { aspectRatio } : {}),
    duration,
    resolution,
    tier: 'turbo',
  };

  const override = body?.extra_body?.aivideomaker_tier;
  if (override === 'turbo' || override === 'base') params.tier = override;
  else if (override) warnings.push(`extra_body.aivideomaker_tier="${override}" ignored (expected turbo|base)`);
  else {
    for (const [re, tier] of ARK_MODEL_TIER) if (re.test(model)) { params.tier = tier; break; }
  }

  if (params.tier === 'base') {
    warnings.push('tier=base is always billed by aivideomaker');
  } else if (duration > FREE_MAX_DURATION) {
    warnings.push(`duration ${duration}s exceeds the free window (turbo is billed from ${FREE_MAX_DURATION + 1}s)`);
  }

  return {
    model,
    params,
    requested: {
      model,
      content: items,
      ratio: ratio || null,
      resolution: body.resolution ?? null,
      duration: body.duration ?? null,
      frames: body.frames ?? null,
      output_format: body.output_format ?? null,
      generate_audio: body.generate_audio ?? null,
      watermark: body.watermark ?? null,
      seed: body.seed ?? null,
    },
    effective: {
      aspectRatio: aspectRatio ?? 'auto(from image)',
      duration,
      resolution,
      tier: params.tier,
      // Measured rule: base is always billed; turbo is billed from 9s up.
      billed: params.tier === 'base' || duration > FREE_MAX_DURATION,
    },
    warnings,
    unsupported: [...new Set(unsupported)],
  };
}

/** Build the Ark task object returned by GET /tasks/{id}. */
function buildArkTask(entry, task) {
  const status = task ? (TO_ARK[String(task.taskStatus || '').toLowerCase()] || 'queued') : 'queued';
  const size = sizeOf(task, entry);
  const createdAt = task?.createdAt
    ? Math.floor(new Date(task.createdAt).getTime() / 1000)
    : Math.floor(entry.createdAtMs / 1000);
  const updatedAt = task?.completedAt
    ? Math.floor(new Date(task.completedAt).getTime() / 1000)
    : Math.floor(Date.now() / 1000);

  return {
    id: entry.id,
    model: entry.model,
    status,
    error:
      status === 'failed'
        ? { code: 'GenerationFailed', message: task?.taskStatusMsg || 'video generation failed' }
        : null,
    content: {
      video_url: status === 'succeeded' ? (task?.url ?? null) : null,
      last_frame_url: null,
      file_url: null,
    },
    usage: {
      completion_tokens: 0,
      total_tokens: 0,
      // extensions
      credits: task?.credits ?? null,
    },
    frames: null,
    framespersecond: 24,
    created_at: createdAt,
    updated_at: updatedAt,
    seed: entry.requested.seed ?? -1,
    service_tier: 'default',
    execution_expires_after: 172800,
    generate_audio: entry.requested.generate_audio ?? false,
    duration: Number(task?.duration ?? entry.params.duration),
    ratio: task?.aspectRatio || entry.requested.ratio || null,
    output_format: entry.requested.output_format || 'mp4',
    resolution: size.resolution,
    draft: false,
    draft_task_id: null,
    // extensions: what the caller asked for vs what actually ran
    requested: entry.requested,
    effective: entry.effective,
    warnings: entry.warnings,
    unsupported: entry.unsupported,
    aivideomaker: task ?? null,
  };
}

// ---------------------------------------------------------- http plumbing --

function send(res, code, obj, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(obj, null, 2));
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    ...corsHeaders(),
    ...extraHeaders,
  });
  res.end(body);
}
function err(res, code, msg, type = 'invalid_request_error', param = null) {
  send(res, code, { error: { message: msg, type, param, code: String(code) } });
}
function corsHeaders() {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-headers': '*',
    'access-control-allow-methods': 'GET,POST,DELETE,OPTIONS',
  };
}
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const cs = [];
    req.on('data', (c) => cs.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(cs).toString('utf8');
      if (!raw.trim()) return resolve({});
      try { resolve(JSON.parse(raw)); } catch { reject(new Error('invalid JSON body')); }
    });
    req.on('error', reject);
  });
}
function unauthorized(res) {
  return err(res, 401, 'invalid API key', 'invalid_request_error');
}
function gateOk(req) {
  if (!GATE) return true;
  const h = req.headers.authorization || '';
  const m = h.match(/^Bearer\s+(.+)$/i);
  return !!m && m[1].trim() === GATE;
}

/** Fetch a task, tolerating "not in the list yet" right after submit. */
async function fetchTask(entry) {
  try {
    return await avm.getTask(entry.taskId);
  } catch (e) {
    if (e?.code === 'NOT_FOUND' || /not found/i.test(e.message)) return null;
    throw e;
  }
}

// ---------------------------------------------------------------- server ----

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://x');
    const p = url.pathname;

    if (req.method === 'OPTIONS') {
      res.writeHead(204, corsHeaders());
      return res.end();
    }

    // ---- unauthenticated ----
    if (req.method === 'GET' && (p === '/' || p === '/healthz' || p === '/health')) {
      let userId = null, needsCaptcha = null, error = null;
      try {
        userId = await avm.getUserId();
        needsCaptcha = await avm.needsCaptcha();
      } catch (e) { error = e.message; }
      return send(res, 200, {
        ok: true,
        service: 'aivideomaker protocol adapter',
        protocols: {
          openai_responses: ['POST /v1/responses', 'GET /v1/responses/:id', 'GET /v1/models'],
          minimax: [
            'POST /v1/video_generation',
            'GET /v1/query/video_generation?task_id=',
            'GET /v1/files/retrieve?file_id=',
          ],
          volcengine_ark: [
            'POST /api/v3/contents/generations/tasks',
            'GET /api/v3/contents/generations/tasks/:id',
            'DELETE /api/v3/contents/generations/tasks/:id',
            'GET /api/v3/contents/generations/tasks',
          ],
        },
        models: Object.keys(MODEL_MAP),
        userId,
        needsCaptcha,
        gateKeyRequired: !!GATE,
        bootError: error,
        submit_queue: submitQ.stats(),
      });
    }

    if (req.method === 'GET' && p === '/queue') {
      return send(res, 200, submitQ.stats());
    }

    if (!gateOk(req)) return unauthorized(res);

    // Universal zero-cost validation switch: run the full translation (including
    // media re-hosting) and return the payload without submitting anything.
    const dryRun = req.headers['x-avm-dry-run'] === '1';

    // ================= OpenAI Responses API =================
    if (req.method === 'GET' && p === '/v1/models') {
      const now = Math.floor(Date.now() / 1000);
      return send(res, 200, {
        object: 'list',
        data: Object.keys(MODEL_MAP).map((id) => ({
          id,
          object: 'model',
          created: now,
          owned_by: 'aivideomaker',
        })),
      });
    }

    if (req.method === 'POST' && p === '/v1/responses') {
      return handleCreate(req, res, await readJsonBody(req), dryRun);
    }

    let m = p.match(/^\/v1\/responses\/([A-Za-z0-9_-]+)$/);
    if (m && req.method === 'GET') {
      const entry = RESPONSES.get(m[1]);
      if (!entry) return err(res, 404, `Unknown response: ${m[1]}`, 'invalid_request_error');
      const task = await fetchTask(entry);
      return send(res, 200, buildResponseObject(entry, task));
    }
    if (m && req.method === 'DELETE') {
      return send(res, 200, { id: m[1], object: 'response.deleted', deleted: RESPONSES.delete(m[1]) });
    }

    let mc = p.match(/^\/v1\/responses\/([A-Za-z0-9_-]+)\/cancel$/);
    if (mc && req.method === 'POST') {
      return err(res, 400, 'aivideomaker.ai exposes no cancel endpoint for this task type');
    }

    // ================= MiniMax official API =================
    if (req.method === 'POST' && p === '/v1/video_generation') {
      return handleMinimaxCreate(res, await readJsonBody(req), dryRun);
    }

    if (req.method === 'GET' && p === '/v1/query/video_generation') {
      const taskId = url.searchParams.get('task_id');
      if (!taskId) return send(res, 200, { task_id: null, status: 'Fail', base_resp: baseResp(2013, 'task_id is required') });
      return handleMinimaxQuery(res, taskId);
    }

    if (req.method === 'GET' && p === '/v1/files/retrieve') {
      const fileId = url.searchParams.get('file_id');
      if (!fileId) return send(res, 200, { file: null, base_resp: baseResp(2013, 'file_id is required') });
      return handleMinimaxRetrieve(res, fileId);
    }

    // ================= Volcengine Ark / Seedance native =================
    if (p === ARK_BASE + '/contents/generations/tasks') {
      if (req.method === 'POST') return handleArkCreate(res, await readJsonBody(req), dryRun);
      if (req.method === 'GET') return handleArkList(res, url);
    }

    let ma = p.match(/^\/api\/v3\/contents\/generations\/tasks\/([A-Za-z0-9_-]+)$/);
    if (ma) {
      if (req.method === 'GET') return handleArkGet(res, ma[1]);
      if (req.method === 'DELETE') return handleArkDelete(res, ma[1]);
    }

    return err(res, 404, `Unknown route: ${req.method} ${p}`);
  } catch (e) {
    console.error('[adapter]', e?.stack || e);
    if (e instanceof Error && /invalid JSON body/.test(e.message)) {
      return err(res, 400, e.message);
    }
    return err(res, 500, e?.message || 'internal error', 'server_error');
  }
});

/**
 * Image-to-video needs two fixes that the raw upstream does not do for you:
 *
 *  1. The site rejects external image URLs whose Content-Type is not in its
 *     allowlist (`Unsupported upload content type` — e.g. a URL serving the
 *     non-standard `image/jpg`).  Re-hosting through uploads.getPresignedUrl
 *     always produces an accepted static.img2video.ai URL.
 *  2. The output aspect ratio follows the SOURCE IMAGE, not the `aspectRatio`
 *     field.  Measured: a 800x1200 image produced 480x704 even though the
 *     request said 16:9, and a 2560x1440 image produced 864x480.  So derive it
 *     from the real pixels instead of trusting the caller.
 */
async function prepareImage(url, { rehost = true, needSize = true } = {}) {
  if (!url) return null;
  const trusted = /^https?:\/\/static\d*\.img2video\.ai\//i.test(url);

  if (!trusted && rehost) {
    const up = await avm.uploadImage(url); // also gives width/height
    return { url: up.publicUrl, width: up.width, height: up.height, rehosted: true };
  }
  if (!needSize) return { url, width: 0, height: 0, rehosted: false };

  const r = await fetch(url);
  if (!r.ok) return { url, width: 0, height: 0, rehosted: false };
  const buf = Buffer.from(await r.arrayBuffer());
  const { width, height } = imageDimensions(buf);
  return { url, width, height, rehosted: false };
}

async function resolveImages(params, body) {
  const wantsAutoAspect = !(body.aspect_ratio ?? body.aspectRatio ?? body.size);
  const rehost = body.no_rehost !== true;

  const out = { ...params, imageMeta: [] };

  for (const key of ['imageUrl', 'lastFrameUrl']) {
    if (!out[key]) continue;
    const meta = await prepareImage(out[key], { rehost, needSize: wantsAutoAspect });
    if (!meta) continue;
    out[key] = meta.url;
    if (meta.width && meta.height) {
      out[key === 'imageUrl' ? '_firstW' : '_lastW'] = meta.width;
      out[key === 'imageUrl' ? '_firstH' : '_lastH'] = meta.height;
      out.imageMeta.push({ field: key, ...meta });
    }
  }

  if (wantsAutoAspect && out._firstW && out._firstH) {
    out.aspectRatio = normAspect(`${out._firstW}:${out._firstH}`);
  }
  delete out._firstW; delete out._firstH; delete out._lastW; delete out._lastH;

  // 参考素材 / omni-reference: video and audio references go through the same
  // CDN upload path (50MB / 15MB caps).  Only http(s) and data: inputs can be
  // re-hosted; anything already on the site CDN is left alone.
  out.mediaMeta = [];
  for (const key of ['referenceVideoUrl']) {
    const v = out[key];
    if (typeof v !== 'string' || !v) continue;
    const data = decodeDataUri(v);
    if (data) {
      const up = await avm.uploadFile(data.buf, { name: `ref.${sniffExt(data.mime)}` });
      out[key] = up.publicUrl;
      out.mediaMeta.push({ field: key, kind: up.kind, contentType: up.contentType, size: up.size });
    } else if (rehost && !/^https?:\/\/static\d*\.img2video\.ai\//i.test(v)) {
      const up = await avm.uploadFile(v).catch(() => null);
      if (up) {
        out[key] = up.publicUrl;
        out.mediaMeta.push({ field: key, kind: up.kind, contentType: up.contentType, size: up.size, rehosted: true });
      }
    }
  }
  if (Array.isArray(out.referenceAudioUrls)) {
    const next = [];
    for (const v of out.referenceAudioUrls) {
      if (typeof v !== 'string' || !v) continue;
      const data = decodeDataUri(v);
      if (data) {
        const up = await avm.uploadFile(data.buf, { name: `ref-audio.${sniffExt(data.mime)}` });
        next.push(up.publicUrl);
        out.mediaMeta.push({ field: 'referenceAudioUrls', kind: up.kind, contentType: up.contentType, size: up.size });
      } else if (rehost && !/^https?:\/\/static\d*\.img2video\.ai\//i.test(v)) {
        const up = await avm.uploadFile(v).catch(() => null);
        if (up) {
          next.push(up.publicUrl);
          out.mediaMeta.push({ field: 'referenceAudioUrls', kind: up.kind, contentType: up.contentType, size: up.size, rehosted: true });
        } else next.push(v);
      } else next.push(v);
    }
    out.referenceAudioUrls = next;
  }
  return out;
}

/** Extension for a data-URI mime, so the uploaded filename looks sane. */
function sniffExt(mime) {
  const m = String(mime || '').toLowerCase();
  if (m.includes('mp4')) return 'mp4';
  if (m.includes('quicktime')) return 'mov';
  if (m.includes('webm')) return 'webm';
  if (m.includes('mpeg')) return 'mp3';
  if (m.includes('wav')) return 'wav';
  if (m.includes('m4a') || m.includes('mp4a')) return 'm4a';
  if (m.includes('png')) return 'png';
  if (m.includes('webp')) return 'webp';
  return 'jpg';
}

// ------------------------------------------------- OpenAI: create ----------

async function handleCreate(req, res, body, dryRun = false) {
  if (!body || typeof body !== 'object') return err(res, 400, 'body is required');

  let translated;
  try {
    translated = translate(body);
  } catch (e) {
    return err(res, 400, e.message, 'invalid_request_error');
  }
  const model = translated.model;
  let params = translated.params;

  // Attach to an upstream task that already exists (e.g. started in the web
  // UI) instead of submitting a new one.  Read-only: does not touch upstream.
  const attachId = body.task_id || body.taskId;
  if (attachId) {
    let existing = null;
    try {
      existing = await avm.getTask(String(attachId));
    } catch (e) {
      console.warn(`[adapter] attach ${attachId}: upstream lookup failed: ${e?.message}`);
    }
    const entry = remember({
      id: newId(),
      taskId: String(attachId),
      model,
      params,
      metadata: body.metadata ?? {},
      createdAtMs: existing?.createdAt ? new Date(existing.createdAt).getTime() : Date.now(),
    });
    if (body.stream === true) return streamResponse(req, res, entry);
    return send(res, 200, buildResponseObject(entry, existing));
  }

  if (!hasAnyInput(params)) {
    return err(res, 400, 'input (or prompt) is required');
  }
  // images embedded in the Responses `input` array
  if (!params.imageUrl) {
    const imgs = extractInputImages(body.input);
    if (imgs[0]) params.imageUrl = imgs[0];
    if (imgs[1]) params.lastFrameUrl = imgs[1];
    if (imgs.length > 2) params.referenceImageUrls = imgs.slice(2);
  }

  try {
    params = await resolveImages(params, body);
  } catch (e) {
    return err(res, 400, `image preparation failed: ${e?.message}`, 'invalid_request_error');
  }

  if (dryRun) {
    return send(res, 200, { dry_run: true, ok: true, model, upstream_payload: params });
  }

  let taskId;
  try {
    taskId = await submitQ.submit(params);
  } catch (e) {
    const msg = e?.message || 'submit failed';
    if (e?.code === 'CAPTCHA_REQUIRED') return err(res, 403, msg, 'permission_error');
    if (e?.code === 'BAD_REQUEST' || /too_big|expected|invalid/i.test(msg)) {
      return err(res, 400, msg, 'invalid_request_error');
    }
    return err(res, 502, msg, 'server_error');
  }

  const entry = remember({
    id: newId(),
    taskId,
    model,
    params,
    metadata: body.metadata ?? {},
    createdAtMs: Date.now(),
    prompt: params.content,
  });

  const background = body.background === true;
  const stream = body.stream === true;

  if (background) {
    return send(res, 200, buildResponseObject(entry, null));
  }

  if (stream) {
    return streamResponse(req, res, entry);
  }

  // synchronous: block until terminal
  try {
    const w = await avm.waitForTask(taskId, {
      timeoutMs: +(body.timeoutMs || DEFAULT_TIMEOUT_MS),
      intervalMs: POLL_MS,
    });
    return send(res, 200, buildResponseObject(entry, w.task ?? null));
  } catch (e) {
    return err(res, 502, e?.message || 'wait failed', 'server_error');
  }
}

/** SSE: OpenAI Responses lifecycle events. */
async function streamResponse(req, res, entry) {
  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache, no-transform',
    connection: 'keep-alive',
    'x-accel-buffering': 'no',
    ...corsHeaders(),
  });
  let closed = false;
  req.on('close', () => { closed = true; });

  const emit = (event, data) => {
    if (closed) return;
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  try {
    let obj = buildResponseObject(entry, null);
    emit('response.created', obj);
    emit('response.in_progress', obj);

    const w = await avm.waitForTask(entry.taskId, {
      timeoutMs: DEFAULT_TIMEOUT_MS,
      intervalMs: POLL_MS,
      onUpdate: (task) => {
        obj = buildResponseObject(entry, task);
        emit('response.output_item.done', obj.output[0]);
        emit('response.in_progress', obj);
      },
    });

    obj = buildResponseObject(entry, w.task ?? null);
    if (!w.done) {
      obj.status = 'incomplete';
      obj.incomplete_details = { reason: 'max_poll_time_exceeded' };
      emit('response.incomplete', obj);
    } else if (obj.status === 'failed') {
      emit('response.failed', obj);
    } else {
      emit('response.output_item.done', obj.output[0]);
      emit('response.completed', obj);
    }
  } catch (e) {
    const obj = buildResponseObject(entry, null);
    obj.status = 'failed';
    obj.error = { code: 'server_error', message: e?.message || 'stream failed' };
    emit('response.failed', obj);
  } finally {
    if (!closed) res.write('event: done\ndata: [DONE]\n\n');
    res.end();
  }
}

// ------------------------------------------------- MiniMax: create ---------

async function handleMinimaxCreate(res, body, dryRun = false) {
  let params;
  try {
    params = translate(body).params;
  } catch (e) {
    return send(res, 200, { task_id: '', base_resp: baseResp(2013, e.message) });
  }
  if (!hasAnyInput(params)) {
    return send(res, 200, { task_id: '', base_resp: baseResp(2013, 'prompt is required') });
  }
  try {
    params = await resolveImages(params, body);
  } catch (e) {
    return send(res, 200, { task_id: '', base_resp: baseResp(2013, `image preparation failed: ${e?.message}`) });
  }

  if (dryRun) {
    return send(res, 200, { dry_run: true, ok: true, upstream_payload: params });
  }

  try {
    const taskId = await submitQ.submit(params);
    remember({
      id: newId(),
      taskId,
      model: String(body.model || DEFAULT_MODEL),
      params,
      createdAtMs: Date.now(),
    });
    return send(res, 200, { task_id: String(taskId), base_resp: baseResp(0) });
  } catch (e) {
    return send(res, 200, { task_id: '', base_resp: classify(e) });
  }
}

async function handleMinimaxQuery(res, taskId) {
  let task;
  try {
    task = await avm.getTask(taskId);
  } catch (e) {
    const br = classify(e);
    if (br.status_code === 2013) {
      // not visible yet — some backends lag a few seconds after submit
      return send(res, 200, { task_id: String(taskId), status: 'Preparing', base_resp: baseResp(0) });
    }
    return send(res, 200, { task_id: String(taskId), status: 'Fail', base_resp: br });
  }
  if (!task) {
    return send(res, 200, { task_id: String(taskId), status: 'Preparing', base_resp: baseResp(0) });
  }
  const entry = entryForTaskId(taskId);
  const status = normMinimax(task.taskStatus);
  const out = {
    task_id: String(taskId),
    status,
    base_resp: baseResp(0),
  };
  if (status === 'Success') {
    const { video_width, video_height } = sizeOf(task, entry);
    out.file_id = String(taskId);
    out.video_width = video_width;
    out.video_height = video_height;
  }
  if (status === 'Fail') {
    out.error_message = task.taskStatusMsg || task.taskStatus || 'generation failed';
  }
  return send(res, 200, out);
}

async function handleMinimaxRetrieve(res, fileId) {
  let task;
  try {
    task = await avm.getTask(fileId);
  } catch (e) {
    return send(res, 200, { file: null, base_resp: classify(e) });
  }
  if (!task?.url) {
    return send(res, 200, { file: null, base_resp: baseResp(2013, 'file not ready') });
  }
  return send(res, 200, {
    file: {
      file_id: String(fileId),
      bytes: null,
      created_at: task.createdAt ? Math.floor(new Date(task.createdAt).getTime() / 1000) : null,
      filename: `${fileId}.mp4`,
      purpose: 'video_generation',
      download_url: task.url,
    },
    base_resp: baseResp(0),
  });
}

// ================================================== Ark: create / query ====

async function handleArkCreate(res, body, dryRun = false) {
  let t;
  try {
    t = translateArk(body);
  } catch (e) {
    return arkError(res, 400, e.arkCode || 'InvalidParameter', e.message, e.param);
  }

  // Fetch + re-host any image the site will not accept as-is (external URL with
  // a non-allowlisted Content-Type, or an inline base64 data URI).
  let params = t.params;
  try {
    params = await prepareArkImages(params);
    params = await resolveImages(params, body);
  } catch (e) {
    return arkError(res, 400, 'InvalidParameter', `image preparation failed: ${e?.message}`);
  }

  // Extension: validate a request end-to-end (translation + image re-hosting)
  // WITHOUT submitting, so a request body can be checked at zero cost.
  if (dryRun || body?.extra_body?.aivideomaker_dry_run === true) {
    return send(res, 200, {
      dry_run: true,
      ok: true,
      requested: t.requested,
      effective: t.effective,
      warnings: t.warnings,
      unsupported: t.unsupported,
      upstream_payload: params,
    });
  }

  let taskId;
  try {
    taskId = await submitQ.submit(params);
  } catch (e) {
    if (e?.code === 'CAPTCHA_REQUIRED') {
      return arkError(res, 429, 'RateLimitExceeded', e.message);
    }
    if (e?.code === 'BAD_REQUEST' || /too_big|expected|invalid/i.test(e?.message || '')) {
      return arkError(res, 400, 'InvalidParameter', e.message);
    }
    if (/queue is full/i.test(e?.message || '')) {
      return arkError(res, 429, 'TaskQueueFull', e.message);
    }
    return arkError(res, 500, 'InternalServiceError', e?.message || 'submit failed');
  }
  // The site silently rejects with an empty string rather than an error.
  if (!taskId) {
    return arkError(res, 400, 'InvalidParameter', 'upstream rejected the request (captcha gate or invalid parameters)');
  }

  const entry = {
    id: newArkId(),
    taskId,
    model: t.model,
    requested: t.requested,
    effective: { ...t.effective, aspectRatio: params.aspectRatio ?? t.effective.aspectRatio },
    warnings: t.warnings,
    unsupported: t.unsupported,
    params,
    createdAtMs: Date.now(),
  };
  ARK_TASKS.set(entry.id, entry);
  return send(res, 200, { id: entry.id });
}

async function handleArkGet(res, id) {
  const entry = ARK_TASKS.get(id);
  if (!entry) return arkError(res, 404, 'TaskNotFound', `task ${id} not found`);
  let task = null;
  try { task = await avm.getTask(entry.taskId); } catch { task = null; }
  return send(res, 200, buildArkTask(entry, task));
}

async function handleArkDelete(res, id) {
  const entry = ARK_TASKS.get(id);
  if (!entry) return arkError(res, 404, 'TaskNotFound', `task ${id} not found`);
  // aivideomaker exposes no cancel endpoint, so this only drops the local
  // record.  Ark returns an empty object on success.
  ARK_TASKS.delete(id);
  return send(res, 200, {});
}

async function handleArkList(res, url) {
  const limit = Math.min(100, Math.max(1, +(url.searchParams.get('page_size') || 20)));
  const page = Math.max(1, +(url.searchParams.get('page_num') || 1));
  const all = [...ARK_TASKS.values()].sort((a, b) => b.createdAtMs - a.createdAtMs);
  const slice = all.slice((page - 1) * limit, page * limit);
  const items = [];
  for (const e of slice) {
    let task = null;
    try { task = await avm.getTask(e.taskId); } catch { task = null; }
    items.push(buildArkTask(e, task));
  }
  // NOTE: the list response envelope could not be verified against the public
  // docs (that page is JS-rendered), so this shape is our best effort.
  return send(res, 200, { items, total: all.length, page_num: page, page_size: limit });
}

/** Decode inline data: URIs and re-host anything not already on the site CDN. */
async function prepareArkImages(params) {
  const out = { ...params };
  for (const key of ['imageUrl', 'lastFrameUrl']) {
    const v = out[key];
    if (typeof v !== 'string' || !v) continue;
    const data = decodeDataUri(v);
    if (data) {
      const up = await avm.uploadImage(data.buf, { name: `ark-${key}.${data.mime.split('/')[1] || 'png'}` });
      out[key] = up.publicUrl;
    }
  }
  if (Array.isArray(out.referenceImageUrls)) {
    const next = [];
    for (const v of out.referenceImageUrls) {
      const data = typeof v === 'string' ? decodeDataUri(v) : null;
      if (data) {
        const up = await avm.uploadImage(data.buf, { name: `ark-ref.${data.mime.split('/')[1] || 'png'}` });
        next.push(up.publicUrl);
      } else next.push(v);
    }
    out.referenceImageUrls = next;
  }
  return out;
}

server.listen(PORT, () => {
  console.log(`[adapter] listening on http://127.0.0.1:${PORT}`);
  console.log(`[adapter] openai  : POST /v1/responses  GET /v1/responses/:id  GET /v1/models`);
  console.log(`[adapter] minimax : POST /v1/video_generation  GET /v1/query/video_generation  GET /v1/files/retrieve`);
  console.log(`[adapter] ark     : POST ${ARK_BASE}/contents/generations/tasks  GET|DELETE ${ARK_BASE}/contents/generations/tasks/{id}`);
  console.log(`[adapter] gate key: ${GATE ? 'required' : 'disabled (open)'}`);
});
