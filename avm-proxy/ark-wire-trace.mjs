// Emit real wire traces for Protocol A: what the caller sends (Ark native),
// what the adapter sends upstream (aivideomaker tRPC), and both responses.
// Uses X-Avm-Dry-Run so no task is created.
import { readFileSync } from 'node:fs';

const BASE = process.env.ADR_BASE || 'http://127.0.0.1:8788';
const KEY = process.env.ADR_KEY || 'sk-avm-demo';
const IMG = 'https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg';
const VID = 'https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4';
const AUD = 'https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3';

const cases = [
  ['A-1 文生视频（仅 text）', {
    model: 'doubao-seedance-2-5-260628',
    content: [{ type: 'text', text: 'a paper boat circling in a rain puddle' }],
    ratio: '16:9', resolution: '480p', duration: 5,
  }],
  ['A-2 图生（role=first_frame）', {
    model: 'doubao-seedance-2-5-260628',
    content: [
      { type: 'text', text: 'a car' },
      { type: 'image_url', role: 'first_frame', image_url: { url: IMG } },
    ],
    ratio: 'adaptive', resolution: '480p', duration: 5,
  }],
  ['A-3 首尾帧（first + last）', {
    model: 'doubao-seedance-1-5-pro-251215',
    content: [
      { type: 'text', text: '360度环绕运镜' },
      { type: 'image_url', role: 'first_frame', image_url: { url: IMG } },
      { type: 'image_url', role: 'last_frame', image_url: { url: IMG } },
    ],
    generate_audio: true, ratio: 'adaptive', duration: 5, watermark: false,
  }],
  ['A-4 参考素材（图+视频+音频）', {
    model: 'doubao-seedance-2-5-260628',
    content: [
      { type: 'text', text: '视频编辑：删除 @视频1中的所有人，除了主角。' },
      { type: 'image_url', role: 'reference_image', image_url: { url: IMG } },
      { type: 'video_url', role: 'reference_video', video_url: { url: VID } },
      { type: 'audio_url', role: 'reference_audio', audio_url: { url: AUD } },
    ],
    generate_audio: true, ratio: '16:9', duration: 5,
    omni_reference_task_type: 'reference', output_format: 'mov',
  }],
];

console.log('# 协议 A 双向 wire trace（dry_run，未产生任务）\n');
for (const [label, body] of cases) {
  const res = await fetch(`${BASE}/api/v3/contents/generations/tasks`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json', 'X-Avm-Dry-Run': '1' },
    body: JSON.stringify(body),
  });
  const j = await res.json();
  const p = j.upstream_payload || {};

  console.log('## ' + label + '\n');
  console.log('**① 调用方 → 适配层**（火山原生格式）\n');
  console.log('```http');
  console.log('POST /api/v3/contents/generations/tasks');
  console.log('Authorization: Bearer <ARK_API_KEY>');
  console.log('Content-Type: application/json');
  console.log('X-Avm-Dry-Run: 1     # 仅校验，不提交');
  console.log('```');
  console.log('```json');
  console.log(JSON.stringify(body, null, 2));
  console.log('```\n');

  console.log('**② 适配层 → aivideomaker**（站点 tRPC 格式）\n');
  console.log('```http');
  console.log('POST https://aivideomaker.ai/api/ai.minimaxH3?batch=1');
  console.log('Cookie: auth_session=<session>');
  console.log('Content-Type: application/json');
  console.log('```');
  const upstreamBody = { 0: { json: p } };
  delete upstreamBody[0].json.imageMeta;
  delete upstreamBody[0].json.mediaMeta;
  console.log('```json');
  console.log(JSON.stringify(upstreamBody, null, 2));
  console.log('```\n');
  if (p.imageMeta?.length || p.mediaMeta?.length) {
    console.log('> 转存元信息：' + JSON.stringify([...(p.imageMeta || []), ...(p.mediaMeta || [])]) + '\n');
  }

  console.log('**③ 适配层 → 调用方**（本次为 dry_run，故返回校验结果；真实提交见下）\n');
  console.log('```json');
  console.log(JSON.stringify({
    dry_run: j.dry_run, ok: j.ok,
    effective: j.effective, warnings: j.warnings, unsupported: j.unsupported,
  }, null, 2));
  console.log('```\n');
  console.log('---\n');
}

console.log('## A-9 参数校验失败（缺 model）\n');
{
  const res = await fetch(`${BASE}/api/v3/contents/generations/tasks`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: [{ type: 'text', text: 'x' }] }),
  });
  console.log('```http\nHTTP/1.1 ' + res.status + '\n```');
  console.log('```json\n' + JSON.stringify(await res.json(), null, 2) + '\n```\n');
}
