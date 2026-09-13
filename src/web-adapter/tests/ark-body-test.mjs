// Walk every field of the Seedance 2.5 / Volcengine Ark create-task request
// body against the adapter, using extra_body.aivideomaker_dry_run so that
// nothing is submitted and nothing is billed.
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788/api/v3';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';
const MODEL = 'doubao-seedance-2-5-260628';
const CDN_IMG = 'https://static.img2video.ai/1789020854691-d80ed1ba-edfe-49f5-9ac0-74543a53ef27-53bbd0874ac247549beb8cb0226b5e66.png';
const text = (s = 'a red balloon rising into a clear sky') => ({ type: 'text', text: s });

async function call(body) {
  const r = await fetch(`${BASE}/contents/generations/tasks`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { http: r.status, json: await r.json() };
}

const rows = [];
function note(name, res, pick) {
  const j = res.json;
  if (j.error) rows.push([name, `HTTP ${res.http} ERR ${j.error.code}`, j.error.message.slice(0, 78)]);
  else rows.push([name, `HTTP ${res.http} OK`, pick(j)]);
}
const eff = (j) => `ratio=${j.effective.aspectRatio} res=${j.effective.resolution} dur=${j.effective.duration}s tier=${j.effective.tier} billed=${j.effective.billed}`;

console.log('############ A. 必填与错误分支 ############\n');
note('缺 model', await call({ content: [text()] }), (j) => '');
note('缺 content', await call({ model: MODEL }), (j) => '');
note('content 为空数组', await call({ model: MODEL, content: [] }), (j) => '');
note('ratio 非法 (5:4)', await call({ model: MODEL, content: [text()], ratio: '5:4' }), (j) => '');
note('resolution 非法 (4k)', await call({ model: MODEL, content: [text()], resolution: '4k' }), (j) => '');
note('content type 未知', await call({ model: MODEL, content: [text(), { type: 'weird_type' }], extra_body: { aivideomaker_dry_run: true } }), (j) => 'unsupported=' + JSON.stringify(j.unsupported));

console.log('############ B. content 各角色 ############\n');
const contentCases = {
  '仅 text': [text()],
  'text + first_frame': [text(), { type: 'image_url', role: 'first_frame', image_url: { url: CDN_IMG } }],
  'text + last_frame': [text(), { type: 'image_url', role: 'last_frame', image_url: { url: CDN_IMG } }],
  'text + reference_image(带role)': [text(), { type: 'image_url', role: 'reference_image', image_url: { url: CDN_IMG } }],
  'text + image_url(无role)': [text(), { type: 'image_url', image_url: { url: CDN_IMG } }],
  'text + first+last': [text(), { type: 'image_url', role: 'first_frame', image_url: { url: CDN_IMG } }, { type: 'image_url', role: 'last_frame', image_url: { url: CDN_IMG } }],
  'text + reference_video': [text(), { type: 'video_url', role: 'reference_video', video_url: { url: 'https://example.com/a.mp4' } }],
  'text + reference_audio': [text(), { type: 'audio_url', role: 'reference_audio', audio_url: { url: 'https://example.com/a.mp3' } }],
  '多条 text': [text('第一段'), text('第二段')],
};
for (const [name, content] of Object.entries(contentCases)) {
  const res = await call({ model: MODEL, content, ratio: 'adaptive', resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true } });
  note(name, res, (j) => {
    const p = j.upstream_payload;
    return `img=${p.imageUrl ? 'Y' : '-'} last=${p.lastFrameUrl ? 'Y' : '-'} refs=${(p.referenceImageUrls || []).length} vid=${p.referenceVideoUrl ? 'Y' : '-'} aud=${(p.referenceAudioUrls || []).length} | ${eff(j)}`;
  });
}

console.log('############ C. ratio / resolution 枚举 ############\n');
for (const ratio of ['16:9', '4:3', '1:1', '3:4', '9:16', '21:9', 'adaptive']) {
  const res = await call({ model: MODEL, content: [text()], ratio, resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true } });
  note(`ratio=${ratio}`, res, (j) => eff(j));
}
for (const resolution of ['480p', '720p', '1080p']) {
  const res = await call({ model: MODEL, content: [text()], resolution, duration: 5, extra_body: { aivideomaker_dry_run: true } });
  note(`resolution=${resolution}`, res, (j) => eff(j));
}

console.log('############ D. duration / frames 与档位吸附 ############\n');
for (const [resolution, duration] of [['480p', 5], ['480p', 6], ['480p', 12], ['480p', 20], ['720p', 6], ['720p', 20], ['720p', 30], ['1080p', 6], ['1080p', 18]]) {
  const res = await call({ model: MODEL, content: [text()], resolution, duration, extra_body: { aivideomaker_dry_run: true } });
  note(`res=${resolution} duration=${duration}`, res, (j) => `${eff(j)} warn=${j.warnings.length ? j.warnings.join(' | ').slice(0, 60) : '-'}`);
}
for (const frames of [120, 240]) {
  const res = await call({ model: MODEL, content: [text()], resolution: '480p', frames, extra_body: { aivideomaker_dry_run: true } });
  note(`frames=${frames} (24fps)`, res, (j) => `${eff(j)} warn=${j.warnings.join(' | ').slice(0, 60)}`);
}
for (const duration of [-1, 8, 9]) {
  const res = await call({ model: MODEL, content: [text()], resolution: '480p', duration, extra_body: { aivideomaker_dry_run: true } });
  note(`duration=${duration} 计费判定`, res, (j) => `${eff(j)} warn=${j.warnings.join(' | ').slice(0, 56)}`);
}
for (const [resolution, duration] of [['480p', 8], ['480p', 9], ['480p', 12]]) {
  const res = await call({ model: MODEL, content: [text()], resolution, duration, extra_body: { aivideomaker_dry_run: true, aivideomaker_prefer_free: true } });
  note(`prefer_free res=${resolution} duration=${duration}`, res, (j) => `${eff(j)}`);
}

console.log('############ E. 站点不支持的参数（逐个） ############\n');
const unsupportedCases = {
  'generate_audio': true, 'watermark': true, 'seed': 42, 'camera_fixed': true,
  'return_last_frame': true, 'draft': true, 'service_tier': 'default',
  'priority': 5, 'callback_url': 'https://x/cb', 'safety_identifier': 'u1',
  'execution_expires_after': 3600, 'omni_reference_task_type': 'auto',
  'tools': [{ type: 'x' }], 'output_format': 'mov',
};
for (const [k, v] of Object.entries(unsupportedCases)) {
  const res = await call({ model: MODEL, content: [text()], resolution: '480p', duration: 5, [k]: v, extra_body: { aivideomaker_dry_run: true } });
  note(`${k}=${JSON.stringify(v)}`, res, (j) => `unsupported=${JSON.stringify(j.unsupported)}`);
}
const all = await call({
  model: MODEL, content: [text()], resolution: '480p', duration: 5,
  generate_audio: true, watermark: false, seed: 7, camera_fixed: true, return_last_frame: true,
  draft: false, service_tier: 'default', priority: 1, callback_url: 'https://x/cb',
  safety_identifier: 'u1', execution_expires_after: 3600, omni_reference_task_type: 'auto',
  tools: [{ type: 'x' }], output_format: 'mov', extra_body: { aivideomaker_dry_run: true },
});
note('全部不支持参数一起传', all, (j) => `共 ${j.unsupported.length} 项: ${j.unsupported.join(', ')}`);

console.log('############ F. tier / model 映射 ############\n');
for (const m of ['doubao-seedance-2-5-260628', 'doubao-seedance-2-0-260128', 'doubao-seedance-1-0-pro-250528', 'unknown-model']) {
  const res = await call({ model: m, content: [text()], resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true } });
  note(`model=${m}`, res, (j) => `tier=${j.effective.tier} billed=${j.effective.billed}`);
}
const baseTier = await call({ model: MODEL, content: [text()], resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true, aivideomaker_tier: 'base' } });
note('extra_body.aivideomaker_tier=base', baseTier, (j) => `tier=${j.effective.tier} billed=${j.effective.billed} warn=${j.warnings.join(' | ').slice(0, 50)}`);
const badTier = await call({ model: MODEL, content: [text()], resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true, aivideomaker_tier: 'normal' } });
note('extra_body.aivideomaker_tier=normal(非法)', badTier, (j) => `tier=${j.effective.tier} warn=${j.warnings.join(' | ').slice(0, 60)}`);

console.log('############ G. base64 data URI 图片 ############\n');
const PNG_1PX = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
const b64 = await call({ model: MODEL, content: [text(), { type: 'image_url', role: 'first_frame', image_url: { url: PNG_1PX } }], resolution: '480p', duration: 5, extra_body: { aivideomaker_dry_run: true } });
note('首帧传 base64 data URI', b64, (j) => `重写为 ${String(j.upstream_payload.imageUrl).slice(0, 62)}...`);

console.log('\n\n================ 汇总 ================');
const w = [Math.max(...rows.map((r) => r[0].length)), 26];
for (const [a, b, c] of rows) console.log(a.padEnd(w[0]) + '  ' + b.padEnd(w[1]) + '  ' + c);
console.log(`\n共 ${rows.length} 个用例；全部为 dry_run，未提交任何任务、未消耗额度。`);
