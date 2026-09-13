// Actually submit the free doc examples (480p / 5s / turbo), waiting out the
// captcha gate and the 2-task concurrency cap, then poll to completion.
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788';
const V3 = BASE + '/api/v3';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';

const CASES = [
  {
    label: '示例2 视频编辑 → 强制 480p/5s',
    body: {
      model: 'doubao-seedance-2-5-260628',
      content: [
        { type: 'text', text: '视频编辑：删除 @视频1中的所有人，除了主角。' },
        { type: 'video_url', video_url: { url: 'https://arkdocs.tos-cn-beijing.volces.com/videos/video-generation/seedance2.5_edit_input.mov' }, role: 'reference_video' },
      ],
      generate_audio: true, ratio: 'adaptive', duration: 5, resolution: '480p',
      omni_reference_task_type: 'edit', output_format: 'mov',
    },
  },
  {
    label: '示例3 首尾帧 → 强制 480p/5s',
    body: {
      model: 'doubao-seedance-1-5-pro-251215',
      content: [
        { type: 'text', text: '图中女孩对着镜头说"茄子"，360度环绕运镜' },
        { type: 'image_url', image_url: { url: 'https://ark-project.tos-cn-beijing.volces.com/doc_image/seepro_first_frame.jpeg' }, role: 'first_frame' },
        { type: 'image_url', image_url: { url: 'https://ark-project.tos-cn-beijing.volces.com/doc_image/seepro_last_frame.jpeg' }, role: 'last_frame' },
      ],
      generate_audio: true, ratio: 'adaptive', duration: 5, resolution: '480p', watermark: false,
    },
  },
];

const log = (...a) => console.log(new Date().toISOString(), ...a);

for (const c of CASES) {
  log('--- ' + c.label + ' ---');
  let id = null;
  const deadline = Date.now() + 20 * 60_000;
  while (!id && Date.now() < deadline) {
    const h = await fetch(`${BASE}/healthz`).then((r) => r.json()).catch(() => ({}));
    if (h.needsCaptcha !== false) { await new Promise((r) => setTimeout(r, 40_000)); continue; }
    const r = await fetch(`${V3}/contents/generations/tasks`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(c.body),
    });
    const j = await r.json();
    if (j.id) { id = j.id; log('created', id); break; }
    if (j.error?.code === 'RateLimitExceeded' || j.error?.code === 'TaskQueueFull') {
      log(j.error.code, 'retry in 30s'); await new Promise((r) => setTimeout(r, 30_000)); continue;
    }
    log('REJECTED', JSON.stringify(j)); break;
  }
  if (!id) { log('gave up'); continue; }

  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < 600_000) {
    const r = await fetch(`${V3}/contents/generations/tasks/${id}`, { headers: { Authorization: `Bearer ${KEY}` } });
    last = await r.json();
    if (['succeeded', 'failed', 'cancelled'].includes(last.status)) break;
    await new Promise((r) => setTimeout(r, 12_000));
  }
  log('RESULT', last.status, '| 成片:', last.content?.video_url || '(无)');
  log('  effective:', JSON.stringify(last.effective));
  log('  warnings :', JSON.stringify(last.warnings));
  log('  上游记录 :', JSON.stringify({
    id: last.aivideomaker?.id, source: last.aivideomaker?.source,
    res: last.aivideomaker?.kelingKeyId, dur: last.aivideomaker?.duration,
    credits: last.aivideomaker?.credits, paid: last.aivideomaker?.paid,
    aspect: last.aivideomaker?.aspectRatio,
  }));
  await new Promise((r) => setTimeout(r, 10_000));
}
log('ALL DONE');
