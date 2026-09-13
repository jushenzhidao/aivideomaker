// Full automated test run for the Ark / Seedance native adapter (Protocol A).
// Everything that would create a generation uses X-Avm-Dry-Run; the only real
// network writes are CDN uploads (free).
import fs from 'node:fs';

const BASE = process.env.ADR_BASE || 'http://127.0.0.1:8788';
const V3 = BASE + '/api/v3/contents/generations/tasks';
const KEY = process.env.ADR_KEY || 'sk-avm-demo';
const MODEL = 'doubao-seedance-2-5-260628';
const IMG = 'https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg';
const VID = 'https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4';
const AUD = 'https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3';
const text = (s = 'a red balloon rising into a clear sky') => ({ type: 'text', text: s });

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, detail = '') {
  if (cond) { pass++; console.log('  ✓ ' + name + (detail ? '  — ' + detail : '')); }
  else { fail++; failures.push(name); console.log('  ✗ ' + name + (detail ? '  — ' + detail : '')); }
}

async function post(body, { dry = true, key = KEY } = {}) {
  const r = await fetch(V3, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(key ? { Authorization: `Bearer ${key}` } : {}),
      ...(dry ? { 'X-Avm-Dry-Run': '1' } : {}),
    },
    body: JSON.stringify(body),
  });
  return { status: r.status, json: await r.json().catch(() => null) };
}
const dry = (body) => post(body, { dry: true });

console.log('\n======== A. 接口契约 ========');
{
  const noAuth = await post({ model: MODEL, content: [text()] }, { dry: true, key: null });
  check('无鉴权 → 401', noAuth.status === 401, 'got ' + noAuth.status);

  const badKey = await post({ model: MODEL, content: [text()] }, { dry: true, key: 'wrong' });
  check('错误密钥 → 401', badKey.status === 401, 'got ' + badKey.status);

  const unknown = await fetch(`${BASE}/api/v3/nope`, { headers: { Authorization: `Bearer ${KEY}` } });
  check('未知路由 → 404', unknown.status === 404);

  const notFound = await fetch(V3 + '/cgt-does-not-exist', { headers: { Authorization: `Bearer ${KEY}` } });
  const nf = await notFound.json();
  check('查询不存在的任务 → 404 TaskNotFound', notFound.status === 404 && nf.error?.code === 'TaskNotFound');

  const delMissing = await fetch(V3 + '/cgt-does-not-exist', { method: 'DELETE', headers: { Authorization: `Bearer ${KEY}` } });
  check('删除不存在的任务 → 404', delMissing.status === 404);

  const list = await fetch(V3, { headers: { Authorization: `Bearer ${KEY}` } });
  const lj = await list.json();
  check('任务列表 → {items,total,...}', list.status === 200 && Array.isArray(lj.items), `total=${lj.total}`);

  const health = await (await fetch(`${BASE}/healthz`)).json();
  check('/healthz 暴露协议与队列', !!health.protocols?.volcengine_ark && !!health.submit_queue);
}

console.log('\n======== B. 请求体必填与枚举校验 ========');
{
  const noModel = await dry({ content: [text()] });
  check('缺 model → MissingParameter', noModel.status === 400 && noModel.json.error?.code === 'MissingParameter');

  const noContent = await dry({ model: MODEL });
  check('缺 content → MissingParameter', noContent.status === 400 && noContent.json.error?.code === 'MissingParameter');

  const emptyContent = await dry({ model: MODEL, content: [] });
  check('content 空数组 → 400', emptyContent.status === 400);

  const badRatio = await dry({ model: MODEL, content: [text()], ratio: '5:4' });
  check('ratio 非法 → InvalidParameter', badRatio.status === 400 && badRatio.json.error?.param === 'ratio');

  const badRes = await dry({ model: MODEL, content: [text()], resolution: '4k' });
  check('resolution 非法 → InvalidParameter', badRes.status === 400 && badRes.json.error?.param === 'resolution');

  for (const ratio of ['16:9', '4:3', '1:1', '3:4', '9:16', '21:9']) {
    const r = await dry({ model: MODEL, content: [text()], ratio, resolution: '480p', duration: 5 });
    check(`ratio=${ratio} 映射`, r.json.effective?.aspectRatio === ratio, r.json.effective?.aspectRatio);
  }
  const adaptive = await dry({ model: MODEL, content: [text(), { type: 'image_url', role: 'first_frame', image_url: { url: IMG } }], ratio: 'adaptive', resolution: '480p', duration: 5 });
  check('ratio=adaptive → 从原图推导', String(adaptive.json.effective?.aspectRatio).includes('auto') || adaptive.json.effective?.aspectRatio === '1:1', adaptive.json.effective?.aspectRatio);

  for (const resolution of ['480p', '720p', '1080p']) {
    const r = await dry({ model: MODEL, content: [text()], resolution, duration: 5 });
    check(`resolution=${resolution} 透传`, r.json.effective?.resolution === resolution);
  }
}

console.log('\n======== C. content 角色映射 ========');
{
  const cases = [
    ['first_frame → imageUrl', [text(), { type: 'image_url', role: 'first_frame', image_url: { url: IMG } }], (p) => !!p.imageUrl],
    ['last_frame → lastFrameUrl', [text(), { type: 'image_url', role: 'last_frame', image_url: { url: IMG } }], (p) => !!p.lastFrameUrl],
    ['reference_image → referenceImageUrls', [text(), { type: 'image_url', role: 'reference_image', image_url: { url: IMG } }], (p) => p.referenceImageUrls?.length === 1],
    ['无 role → referenceImageUrls', [text(), { type: 'image_url', image_url: { url: IMG } }], (p) => p.referenceImageUrls?.length === 1],
    ['reference_video → referenceVideoUrl', [text(), { type: 'video_url', role: 'reference_video', video_url: { url: VID } }], (p) => !!p.referenceVideoUrl],
    ['reference_audio → referenceAudioUrls', [text(), { type: 'audio_url', role: 'reference_audio', audio_url: { url: AUD } }], (p) => p.referenceAudioUrls?.length === 1],
    ['多条 text 合并', [text('第一段'), text('第二段')], (p) => p.content === '第一段\n第二段'],
    ['站点字段名 content 被识别', null, null],
  ];
  for (const [name, content, pred] of cases.slice(0, 7)) {
    const r = await dry({ model: MODEL, content, ratio: '16:9', resolution: '480p', duration: 5 });
    check(name, pred(r.json.upstream_payload || {}));
  }
  // The site's own field name `content` is a STRING field, which belongs to the
  // MiniMax / web translation path (`translate()`), not the Ark array protocol.
  const mm = await fetch(`${BASE}/v1/video_generation`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json', 'X-Avm-Dry-Run': '1' },
    body: JSON.stringify({ content: 'a car', imageUrl: IMG, duration: 5, resolution: '480p' }),
  });
  const mj = await mm.json();
  check('站点字段名 content（MiniMax 路径）被识别', mj.upstream_payload?.content === 'a car', JSON.stringify(mj.upstream_payload?.content));

  // ...and it must NOT leak into the Ark path, where `content` is an array.
  const arkStr = await dry({ model: MODEL, content: 'a car' });
  check('Ark 路径拒绝字符串 content（协议一致性）', arkStr.status === 400, 'http=' + arkStr.status);
}

console.log('\n======== D. duration / frames / 计费判定 ========');
{
  const d = async (resolution, duration, extra = {}) =>
    (await dry({ model: MODEL, content: [text()], resolution, duration, ...extra })).json;

  const a = await d('480p', 6); check('480p/6s → 吸附 5s', a.effective.duration === 5, `dur=${a.effective.duration}`);
  const b = await d('480p', 12); check('480p/12s → 吸附 10s', b.effective.duration === 10, `dur=${b.effective.duration}`);
  const c = await d('480p', 20); check('480p/20s → 20s 且计费', c.effective.duration === 20 && c.effective.billed === true);
  const e = await d('720p', 6); check('720p/6s → 保留 6s 且免费', e.effective.duration === 6 && e.effective.billed === false);
  const f = await d('720p', 30); check('720p/30s → 钳到 20s', f.effective.duration === 20);
  const g = await d('1080p', 18); check('1080p/18s → 吸附 10s', g.effective.duration === 10, `dur=${g.effective.duration}`);

  const fr = await dry({ model: MODEL, content: [text()], resolution: '480p', frames: 120 });
  check('frames=120 → 5s', fr.json.effective.duration === 5, `dur=${fr.json.effective.duration}`);

  const intel = await d('480p', -1);
  check('duration=-1 → 5s 且免费', intel.effective.duration === 5 && intel.effective.billed === false);

  for (const [res, dur] of [['480p', 8], ['480p', 9], ['480p', 12]]) {
    const r = await dry({ model: MODEL, content: [text()], resolution: res, duration: dur, extra_body: { aivideomaker_prefer_free: true } });
    check(`prefer_free ${res}/${dur}s → 落到免费档`, r.json.effective.billed === false, `dur=${r.json.effective.duration} billed=${r.json.effective.billed}`);
  }

  const cliff = await d('480p', 8);
  check('480p/8s 提示跨进计费区', cliff.warnings.some((w) => /billed range|prefer_free/.test(w)));
}

console.log('\n======== E. 站点不支持的参数 ========');
{
  const unsupportedKeys = ['watermark', 'generate_audio', 'seed', 'camera_fixed', 'return_last_frame', 'draft', 'service_tier', 'priority', 'callback_url', 'safety_identifier', 'tools', 'omni_reference_task_type', 'execution_expires_after', 'output_format'];
  const sample = { watermark: true, generate_audio: true, seed: 42, camera_fixed: true, return_last_frame: true, draft: true, service_tier: 'default', priority: 5, callback_url: 'https://x/cb', safety_identifier: 'u1', tools: [{ type: 'x' }], omni_reference_task_type: 'auto', execution_expires_after: 3600, output_format: 'mov' };
  for (const k of unsupportedKeys) {
    const r = await dry({ model: MODEL, content: [text()], resolution: '480p', duration: 5, [k]: sample[k] });
    check(`${k} → 列入 unsupported`, (r.json.unsupported || []).includes(k));
  }
  const all = await dry({ model: MODEL, content: [text()], resolution: '480p', duration: 5, ...sample });
  check('14 项一起传全部登记', all.json.unsupported.length === 14, `len=${all.json.unsupported.length}`);

  const weird = await dry({ model: MODEL, content: [text(), { type: 'weird_type' }] });
  check('未知 content type 登记为 unsupported', weird.json.unsupported.some((u) => u.includes('weird_type')));
}

console.log('\n======== F. model → tier / 计费默认值 ========');
{
  for (const m of ['doubao-seedance-2-5-260628', 'doubao-seedance-2-0-260128', 'doubao-seedance-1-0-pro-250528', 'doubao-seedance-1-5-pro-251215', 'unknown-model']) {
    const r = await dry({ model: m, content: [text()], resolution: '480p', duration: 5 });
    check(`${m} 默认 turbo（不自动计费）`, r.json.effective.tier === 'turbo' && r.json.effective.billed === false);
  }
  const base = await dry({ model: MODEL, content: [text()], resolution: '480p', duration: 5, extra_body: { aivideomaker_tier: 'base' } });
  check('显式 base → 计费并告警', base.json.effective.tier === 'base' && base.json.effective.billed === true && base.json.warnings.some((w) => /always billed/.test(w)));
  const bad = await dry({ model: MODEL, content: [text()], resolution: '480p', duration: 5, extra_body: { aivideomaker_tier: 'normal' } });
  check('非法 tier → 忽略并告警', bad.json.effective.tier === 'turbo' && bad.json.warnings.some((w) => /ignored/.test(w)));
}

console.log('\n======== G. base64 首帧转存 ========');
{
  const PNG_1PX = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
  const r = await dry({ model: MODEL, content: [text(), { type: 'image_url', role: 'first_frame', image_url: { url: PNG_1PX } }], resolution: '480p', duration: 5 });
  check('base64 → 转存为 CDN URL', /^https:\/\/static\d?\.img2video\.ai\//.test(r.json.upstream_payload?.imageUrl || ''), String(r.json.upstream_payload?.imageUrl).slice(0, 60));
}

console.log('\n======== H. 官方文档三条示例 ========');
{
  const ex1 = await dry({
    model: MODEL,
    content: [text('明亮多彩的广告片风格'), { type: 'image_url', role: 'reference_image', image_url: { url: IMG } }, ...['a', 'b'].map((n) => ({ type: 'video_url', role: 'reference_video', video_url: { url: VID } }))],
    generate_audio: true, ratio: '16:9', duration: 15, omni_reference_task_type: 'reference', output_format: 'mov',
  });
  check('示例1（多素材参考/15s）→ 计费且告警', ex1.json.effective.billed === true && ex1.json.unsupported.includes('omni_reference_task_type'));

  const ex2 = await dry({
    model: MODEL,
    content: [text('视频编辑：删除 @视频1中的所有人，除了主角。'), { type: 'video_url', role: 'reference_video', video_url: { url: VID } }],
    generate_audio: true, ratio: 'adaptive', duration: -1, omni_reference_task_type: 'edit', output_format: 'mov',
  });
  check('示例2（视频编辑/adaptive/-1）→ 5s 且免费', ex2.json.effective.duration === 5 && ex2.json.effective.billed === false);

  const ex3 = await dry({
    model: 'doubao-seedance-1-5-pro-251215',
    content: [text('360度环绕运镜'), { type: 'image_url', role: 'first_frame', image_url: { url: IMG } }, { type: 'image_url', role: 'last_frame', image_url: { url: IMG } }],
    generate_audio: true, ratio: 'adaptive', duration: 5, watermark: false,
  });
  check('示例3（首尾帧）→ 首尾帧都填且免费', !!ex3.json.upstream_payload.imageUrl && !!ex3.json.upstream_payload.lastFrameUrl && ex3.json.effective.billed === false);
}

console.log('\n======== I. 媒体上传（真实上传，免费） ========');
{
  const { AvmClient } = await import('./client.mjs');
  const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
  for (const [path, kind] of [['/tmp/test-video.mp4', 'video'], ['/tmp/test-tone.mp3', 'audio']]) {
    const buf = fs.readFileSync(path);
    const up = await c.uploadFile(buf, { name: path.split('/').pop() });
    const back = await fetch(up.publicUrl);
    const got = Buffer.from(await back.arrayBuffer());
    check(`${kind} 上传并原样回读`, up.kind === kind && got.length === buf.length && Buffer.compare(got, buf) === 0,
      `${up.contentType} ${(buf.length / 1024).toFixed(1)}KB → ${back.headers.get('content-type')}`);
  }
}

console.log('\n======== J. 上游并发队列（mock 上游） ========');
{
  const { SubmitQueue } = await import('./submit-queue.mjs');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = new Map();
  let n = 0;
  const avm = {
    log: [],
    createCalls: 0,
    async create() { this.createCalls++; const id = 't' + ++n; state.set(id, 0); return id; },
    async getModel(id) { const k = (state.get(id) ?? 0) + 1; state.set(id, k); return { id, taskStatus: k >= 2 ? 'succeed' : 'processing' }; },
  };
  const q = new SubmitQueue({ avm, maxConcurrent: 2, pollMs: 25, baseDelayMs: 30, maxAttempts: 4, log: (m) => avm.log.push(m) });
  let peak = 0;
  const timer = setInterval(() => { peak = Math.max(peak, q.running.size); }, 5);
  const ids = await Promise.all([1, 2, 3, 4, 5].map(() => q.submit({ content: 'x' })));
  while (q.running.size || q.pending.length) await sleep(25);
  clearInterval(timer);
  check('5 个提交并发不超 2', peak <= 2, `peak=${peak}`);
  check('5 个全部成功', ids.length === 5 && q.served === 5);
  check('打印了延迟提示', avm.log.some((m) => m.includes('delayed until a slot frees')));

  const avm2 = { log: [], calls: 0, async create() { this.calls++; if (this.calls <= 2) throw new Error('The queue is full. The premium plan can only run 2 task at a time.'); return 't9'; }, async getModel() { return { taskStatus: 'succeed' }; } };
  const q2 = new SubmitQueue({ avm: avm2, maxConcurrent: 1, pollMs: 20, baseDelayMs: 20, maxAttempts: 5, log: (m) => avm2.log.push(m) });
  const id2 = await q2.submit({ content: 'x' });
  check('queue-full 后回退重试成功', id2 === 't9' && avm2.calls === 3, `calls=${avm2.calls}`);
  check('打印了 UPSTREAM FULL', avm2.log.some((m) => m.includes('UPSTREAM FULL')));
}

console.log('\n======== K. 真实提交（免费组合：turbo + 5s + 480p） ========');
if (!process.argv.includes('--live')) {
  console.log('  (skipped) 加 --live 才会真实提交。默认运行全程零消耗。');
  console.log('  免费组合 = tier:turbo 且 duration<=8s，480p 只有 5s 落在免费区。');
} else {
  // Wait for the captcha gate to open — the site re-opens it every few minutes.
  async function waitGate(maxMs = 600_000) {
    const t0 = Date.now();
    for (;;) {
      const h = await fetch(`${BASE}/healthz`).then((r) => r.json()).catch(() => ({}));
      if (h.needsCaptcha === false) return true;
      if (Date.now() - t0 > maxMs) return false;
      console.log(`  … 闸门关闭中（needsCaptcha=${h.needsCaptcha}），等待 30s`);
      await new Promise((r) => setTimeout(r, 30_000));
    }
  }

  async function liveSubmit(label, body) {
    console.log(`\n  [${label}] 等待闸门…`);
    if (!(await waitGate())) { check(`${label} 提交（闸门超时）`, false, 'gate never opened'); return null; }
    const r = await post(body, { dry: false });
    if (!r.json?.id) {
      check(`${label} 提交`, false, JSON.stringify(r.json).slice(0, 120));
      return null;
    }
    const id = r.json.id;
    console.log(`  [${label}] 已提交 taskId = ${id}`);
    check(`${label} 提交成功`, r.status === 200 && !!id, id);

    // poll to terminal
    const t0 = Date.now();
    let last = null;
    while (Date.now() - t0 < 600_000) {
      const s = await fetch(`${V3}/${id}`, { headers: { Authorization: `Bearer ${KEY}` } });
      last = await s.json();
      if (['succeeded', 'failed', 'cancelled'].includes(last.status)) break;
      console.log(`  [${label}] ${last.status} …（已等 ${Math.round((Date.now() - t0) / 1000)}s）`);
      await new Promise((r) => setTimeout(r, 12_000));
    }
    check(`${label} 出片`, last?.status === 'succeeded' && !!last?.content?.video_url,
      `${last?.status} ${last?.content?.video_url ? last.content.video_url.slice(0, 58) + '…' : ''}`);
    if (last?.content?.video_url) console.log(`  [${label}] 成片: ${last.content.video_url}`);

    // The whole point of the "free combination": prove it was NOT billed.
    const up = last?.aivideomaker || {};
    check(`${label} 未计费（paid=false）`, up.paid === false,
      `paid=${up.paid} credits=${up.credits} res=${up.kelingKeyId} dur=${up.duration}`);
    return last;
  }

  await liveSubmit('K1 文生视频 Ark 480p/5s', {
    model: MODEL,
    content: [text('a paper boat circling in a rain puddle')],
    ratio: '16:9', resolution: '480p', duration: 5,
  });

  await liveSubmit('K2 图生视频 Ark first_frame 480p/5s', {
    model: MODEL,
    content: [text('镜头缓慢推进'), { type: 'image_url', role: 'first_frame', image_url: { url: IMG } }],
    ratio: 'adaptive', resolution: '480p', duration: 5,
  });
}

console.log('\n' + '='.repeat(60));
console.log(`结果：通过 ${pass} 项，失败 ${fail} 项`);
if (fail) { console.log('失败项：'); for (const f of failures) console.log('  - ' + f); }
console.log('='.repeat(60));
process.exit(fail ? 1 : 0);
