// 用官方文档里的三条完整示例请求体，先走 dry_run 零成本校验。
const BASE = process.env.ARK_BASE || 'http://127.0.0.1:8788/api/v3';
const KEY = process.env.ARK_KEY || 'sk-avm-demo';

const CASE1 = {
  model: 'doubao-seedance-2-5-260628',
  content: [
    { type: 'text', text: '明亮多彩的广告片风格，果味饼干为主角，包含草莓、苹果、葡萄、橙子四种口味，草莓味参考@图像1，饼干与对应水果以强秩序感的几何阵列排布，整体画面干净、高级、节奏强。开场水果快速建立视觉聚焦，参考@视频1的构图，音乐重拍切入。随后不同口味饼干整齐排列，切特写，参考@视频2的动态和运镜。高潮段一块饼干被折断，瞬间进入慢动作，果味夹心爆开，碎屑飞溅，果汁感与颗粒冲击被放大展示，参考@视频3的冲击感。横向阵列，形成节奏抛物感，参考@视频4的运动，突出秩序美感与产品丰富度。随后迅速回到快节奏剪辑。结尾英文文字 One bite of crispness, a heart full of delight 快速分词切换入画，配合强节奏文字运动与产品定格，参考@视频5，最终品牌感收束，饼干和水果向四周发散，参考@视频6画面充满年轻、活力、好吃、想分享的广告氛围。' },
    { type: 'image_url', image_url: { url: 'https://arkdocs.tos-cn-beijing.volces.com/images/video-generation/seedance2.5_reference1.png' }, role: 'reference_image' },
    ...[2, 3, 4, 5, 6, 7].map((n) => ({ type: 'video_url', video_url: { url: `https://arkdocs.tos-cn-beijing.volces.com/videos/video-generation/seedance2.5_reference${n}.mp4` }, role: 'reference_video' })),
  ],
  generate_audio: true, ratio: '16:9', duration: 15,
  omni_reference_task_type: 'reference', output_format: 'mov',
};

const CASE2 = {
  model: 'doubao-seedance-2-5-260628',
  content: [
    { type: 'text', text: '视频编辑：删除 @视频1中的所有人，除了主角。' },
    { type: 'video_url', video_url: { url: 'https://arkdocs.tos-cn-beijing.volces.com/videos/video-generation/seedance2.5_edit_input.mov' }, role: 'reference_video' },
  ],
  generate_audio: true, ratio: 'adaptive', duration: -1,
  omni_reference_task_type: 'edit', output_format: 'mov',
};

const CASE3 = {
  model: 'doubao-seedance-1-5-pro-251215',
  content: [
    { type: 'text', text: '图中女孩对着镜头说"茄子"，360度环绕运镜' },
    { type: 'image_url', image_url: { url: 'https://ark-project.tos-cn-beijing.volces.com/doc_image/seepro_first_frame.jpeg' }, role: 'first_frame' },
    { type: 'image_url', image_url: { url: 'https://ark-project.tos-cn-beijing.volces.com/doc_image/seepro_last_frame.jpeg' }, role: 'last_frame' },
  ],
  generate_audio: true, ratio: 'adaptive', duration: 5, watermark: false,
};

async function call(body, label) {
  const r = await fetch(`${BASE}/contents/generations/tasks`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, extra_body: { ...(body.extra_body || {}), aivideomaker_dry_run: true } }),
  });
  const j = await r.json();
  console.log('='.repeat(72));
  console.log('### ' + label);
  console.log('='.repeat(72));
  if (j.error) { console.log('ERR', JSON.stringify(j.error)); return; }
  console.log('requested.ratio      :', j.requested.ratio);
  console.log('requested.resolution :', j.requested.resolution);
  console.log('requested.duration   :', j.requested.duration);
  console.log('---');
  console.log('effective            :', JSON.stringify(j.effective));
  console.log('upstream_payload     :', JSON.stringify({
    content: String(j.upstream_payload.content).slice(0, 40) + '...',
    imageUrl: j.upstream_payload.imageUrl,
    lastFrameUrl: j.upstream_payload.lastFrameUrl,
    referenceImageUrls: j.upstream_payload.referenceImageUrls,
    referenceVideoUrl: j.upstream_payload.referenceVideoUrl,
    referenceAudioUrls: j.upstream_payload.referenceAudioUrls,
    aspectRatio: j.upstream_payload.aspectRatio,
    duration: j.upstream_payload.duration,
    resolution: j.upstream_payload.resolution,
    tier: j.upstream_payload.tier,
  }, null, 2));
  console.log('warnings             :', JSON.stringify(j.warnings, null, 2));
  console.log('unsupported          :', JSON.stringify(j.unsupported));
  console.log();
}

await call(CASE1, '示例1：多素材参考（1 图 + 6 视频，15s，16:9）');
await call(CASE2, '示例2：视频编辑（adaptive / duration=-1）');
await call(CASE3, '示例3：首尾帧（Seedance 1.5 pro，adaptive / 5s）');
