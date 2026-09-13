// Append a full task/video-link appendix to TESTCASES.md from the account list.
import fs from 'node:fs';
import { AvmClient } from '../client.mjs';

const DOC = process.env.TESTCASES_PATH || '../../docs/web-reverse/TESTCASES.md';
const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const l = await c.list({ limit: 100 });
const rows = (l.models || []).sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));

const lines = [];
lines.push('');
lines.push('---');
lines.push('');
lines.push('## 附录：全部生成任务与视频链接');
lines.push('');
lines.push(`数据来源：\`model.listModel\`（账号共 ${l.total} 条，取回 ${rows.length} 条）。`);
lines.push('时间均为 UTC。`paid` 为 false 表示免费，true 表示计费。');
lines.push('');
lines.push('| # | 任务 ID | 创建时间 (UTC) | 来源 | 分辨率 | 时长 | 比例 | credits | paid | 成片链接 |');
lines.push('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |');
rows.forEach((m, i) => {
  const url = m.url ? `[播放](${m.url})` : '—';
  lines.push(
    `| ${i + 1} | \`${m.id}\` | ${m.createdAt} | ${m.source ?? '—'} | ${m.kelingKeyId ?? '—'} | ${m.duration ?? '—'} | ${m.aspectRatio ?? '—'} | ${m.credits} | ${m.paid} | ${url} |`,
  );
});

const free = rows.filter((m) => m.paid === false).length;
const billed = rows.filter((m) => m.paid === true).length;
lines.push('');
lines.push(`**汇总**：共 ${rows.length} 条，免费 ${free} 条、计费 ${billed} 条；${rows.filter((m) => m.url).length} 条已产出成片。`);
lines.push('');
lines.push('> 上游视频 URL 有效期 24 小时，且 Seedance 2.5 的链接下载次数上限 100 次，请及时转存。');
lines.push('');

const marker = '## 附录：全部生成任务与视频链接';
let doc = fs.readFileSync(DOC, 'utf8');
const at = doc.indexOf(marker);
if (at !== -1) doc = doc.slice(0, at).replace(/\n---\n\s*$/, '\n');
fs.writeFileSync(DOC, doc.replace(/\s*$/, '\n') + lines.join('\n'));
console.log('已写入附录:', rows.length, '条');
