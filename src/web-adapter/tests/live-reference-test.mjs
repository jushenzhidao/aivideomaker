// Live test of 参考素材 / omni-reference mode: does the upstream actually honour
// referenceVideoUrl / referenceAudioUrls / multiple referenceImageUrls?
//
// Only the FREE combination is used: tier=turbo + duration<=8s + 480p (=> 5s).
// Every case asserts the upstream record reports paid=false.
import fs from 'node:fs';

const BASE = process.env.ADR_BASE || 'http://127.0.0.1:8788';
const V3 = BASE + '/api/v3/contents/generations/tasks';
const KEY = process.env.ADR_KEY || 'sk-avm-demo';
const MODEL = 'doubao-seedance-2-5-260628';

const IMG = 'https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg';
const IMG2 = 'https://static.img2video.ai/1789061713084-b7a86dd2-e5a0-4df0-b10c-0df9d534a410-probe.mp4'; // second media (video) reused as a URL only in R1 below
const VID = 'https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4';
const AUD = 'https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3';

const text = (s) => ({ type: 'text', text: s });
const img = (role, url) => ({ type: 'image_url', role, image_url: { url } });
const vid = (url) => ({ type: 'video_url', role: 'reference_video', video_url: { url } });
const aud = (url) => ({ type: 'audio_url', role: 'reference_audio', audio_url: { url } });

const CASES = [
  {
    label: 'R1 参考素材·多图（role=reference_image ×2）',
    body: { model: MODEL, content: [text('两个物体放在同一画面里'), img('reference_image', IMG), img('reference_image', IMG)], ratio: '16:9', resolution: '480p', duration: 5 },
  },
  // NOTE: upstream rejects mixing reference assets with first/last frame —
  //   "Cannot mix reference assets with first/last frame. Use either
  //    frame-to-video or reference-to-video"
  // so reference mode must NOT carry a first_frame.
  {
    label: 'R2 参考素材·图+视频（reference_image + reference_video）',
    body: { model: MODEL, content: [text('参考视频的运镜方式'), img('reference_image', IMG), vid(VID)], ratio: '16:9', resolution: '480p', duration: 5 },
  },
  {
    label: 'R3 参考素材·图+视频+音频（+ reference_audio）',
    body: { model: MODEL, content: [text('口型与音频对齐'), img('reference_image', IMG), vid(VID), aud(AUD)], ratio: '16:9', resolution: '480p', duration: 5 },
  },
  {
    label: 'R4 参考素材·完整素材集（图×2 + 视频 + 音频）',
    body: { model: MODEL, content: [text('综合参考全部素材，保持主体一致并跟随运镜与声音节奏'), img('reference_image', IMG), img('reference_image', IMG), vid(VID), aud(AUD)], ratio: '16:9', resolution: '480p', duration: 5 },
  },
];

let pass = 0, fail = 0;
const only = process.argv.slice(2).filter((a) => /^R\d/.test(a));
const CASES_TO_RUN = only.length ? CASES.filter((c) => only.some((o) => c.label.startsWith(o))) : CASES;
const check = (n, c, d = '') => { c ? (pass++, console.log('  ✓ ' + n + (d ? '  — ' + d : ''))) : (fail++, console.log('  ✗ ' + n + (d ? '  — ' + d : ''))); };

async function waitGate(maxMs = 900_000) {
  const t0 = Date.now();
  for (;;) {
    const h = await fetch(`${BASE}/healthz`).then((r) => r.json()).catch(() => ({}));
    if (h.needsCaptcha === false) return true;
    if (Date.now() - t0 > maxMs) return false;
    console.log(`    … 闸门关闭（needsCaptcha=${h.needsCaptcha}），30s 后重试`);
    await new Promise((r) => setTimeout(r, 30_000));
  }
}

const results = [];

for (const c of CASES_TO_RUN) {
  console.log('\n' + '='.repeat(74));
  console.log('### ' + c.label);
  console.log('='.repeat(74));

  // 1) dry_run first: confirm the mapping
  const dry = await fetch(V3, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json', 'X-Avm-Dry-Run': '1' },
    body: JSON.stringify(c.body),
  }).then((r) => r.json());
  const p = dry.upstream_payload || {};
  console.log('  [映射] imageUrl=' + (p.imageUrl ? 'Y' : '-') +
    ' lastFrameUrl=' + (p.lastFrameUrl ? 'Y' : '-') +
    ' referenceImageUrls=' + (p.referenceImageUrls?.length ?? 0) +
    ' referenceVideoUrl=' + (p.referenceVideoUrl ? 'Y' : '-') +
    ' referenceAudioUrls=' + (p.referenceAudioUrls?.length ?? 0));
  console.log('  [计费预告] billed=' + dry.effective?.billed);
  if (dry.effective?.billed !== false) { check(c.label + ' 计费预告为免费', false, 'billed=' + dry.effective?.billed); continue; }

  // 2) real submit
  if (!(await waitGate())) { check(c.label + ' 提交（闸门超时）', false); continue; }
  const r = await fetch(V3, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(c.body),
  });
  const j = await r.json();
  if (!j.id) { check(c.label + ' 提交', false, JSON.stringify(j).slice(0, 160)); results.push({ label: c.label, error: j }); continue; }
  console.log('  [提交] taskId = ' + j.id);
  check(c.label + ' 提交成功', true, j.id);

  // 3) poll
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < 600_000) {
    last = await fetch(`${V3}/${j.id}`, { headers: { Authorization: `Bearer ${KEY}` } }).then((x) => x.json());
    if (['succeeded', 'failed', 'cancelled'].includes(last.status)) break;
    console.log(`    ${last.status} …（${Math.round((Date.now() - t0) / 1000)}s）`);
    await new Promise((x) => setTimeout(x, 12_000));
  }

  check(c.label + ' 状态终态', ['succeeded', 'failed'].includes(last?.status), last?.status);
  const up = last?.aivideomaker || {};
  const url = last?.content?.video_url || '';
  if (url) console.log('  [成片] ' + url);
  if (last?.status === 'failed') console.log('  [失败原因] ' + JSON.stringify(last.error));
  check(c.label + ' 未计费（paid=false）', up.paid === false, `paid=${up.paid} credits=${up.credits} source=${up.source}`);

  results.push({ label: c.label, taskId: j.id, status: last?.status, url, paid: up.paid, credits: up.credits, source: up.source, upstreamId: up.id });
  await new Promise((x) => setTimeout(x, 8_000));
}

console.log('\n' + '='.repeat(74));
console.log(`结果：通过 ${pass} 项，失败 ${fail} 项`);
console.log('='.repeat(74));
console.log('\n### 真实任务清单\n');
console.log('| 用例 | Ark taskId | 上游 id | 状态 | paid | 成片 |');
console.log('| --- | --- | --- | --- | --- | --- |');
for (const r of results) {
  console.log(`| ${r.label} | ${r.taskId || '—'} | ${r.upstreamId || '—'} | ${r.status || 'ERROR'} | ${r.paid} | ${r.url ? r.url : '—'} |`);
}
fs.writeFileSync('/tmp/live-ref-results.json', JSON.stringify(results, null, 2));
