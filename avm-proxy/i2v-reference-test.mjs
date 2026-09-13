// Focus on 图生 (image-to-video) and 参考素材 (omni reference) modes,
// validated through X-Avm-Dry-Run so nothing is generated.
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';

const IMG = 'https://static.img2video.ai/1789061428452-87d59cf4-7024-4fa1-aaf4-f913d5db452e-image-1789061428085.jpg';
const VID = 'https://static.img2video.ai/1789061713442-df1bea93-bc5e-4865-b59a-3bde586ea317-test-video.mp4';
const AUD = 'https://static.img2video.ai/1789061721326-dd3fcdf6-85f2-4a2b-93dd-1c9bfaa51726-test-tone.mp3';

const cases = [
  ['图生（imageUrl 单图）', { content: 'a car', imageUrl: IMG, aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
  ['首尾帧（imageUrl + lastFrameUrl）', { content: 'a car', imageUrl: IMG, lastFrameUrl: IMG, aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
  ['参考素材·多图（referenceImageUrls）', { content: 'a car', referenceImageUrls: [IMG, IMG], aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
  ['参考素材·图+视频', { content: 'a car', imageUrl: IMG, referenceVideoUrl: VID, aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
  ['参考素材·图+视频+音频', { content: 'a car', imageUrl: IMG, referenceVideoUrl: VID, referenceAudioUrls: [AUD], aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
  ['参考素材·外链视频需转存', { content: 'a car', imageUrl: IMG, referenceVideoUrl: 'https://arkdocs.tos-cn-beijing.volces.com/videos/video-generation/seedance2.5_reference2.mp4', aspectRatio: '16:9', duration: 5, resolution: '480p', tier: 'turbo' }],
];

for (const [label, body] of cases) {
  const r = await fetch(`${BASE}/v1/video_generation`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json', 'X-Avm-Dry-Run': '1' },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  console.log('='.repeat(74));
  console.log('### ' + label);
  if (j.base_resp && j.base_resp.status_code !== 0) { console.log('ERR', JSON.stringify(j.base_resp)); continue; }
  const p = j.upstream_payload;
  console.log('  imageUrl          :', p.imageUrl || '—');
  console.log('  lastFrameUrl      :', p.lastFrameUrl || '—');
  console.log('  referenceImageUrls:', p.referenceImageUrls?.length ?? 0, '项');
  console.log('  referenceVideoUrl :', p.referenceVideoUrl || '—');
  console.log('  referenceAudioUrls:', p.referenceAudioUrls?.length ?? 0, '项');
  console.log('  aspectRatio/dur/res/tier:', p.aspectRatio, '/', p.duration, '/', p.resolution, '/', p.tier);
  if (p.mediaMeta?.length) console.log('  mediaMeta         :', JSON.stringify(p.mediaMeta));
}
