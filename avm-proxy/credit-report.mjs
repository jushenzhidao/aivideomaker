import fs from 'node:fs';
import { AvmClient } from './client.mjs';

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const l = await c.list({ limit: 100 });
const ms = (l.models || []).sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));

// task ids created by this session's automated probing
const MINE = new Set([
  'zict8zwntxu70ry', 'qaul40pcx9emuc8', 'uxml26jtfygfdud', 'e6o3376382zzwsy',
  '6szj51ed19jl3h9', 'iq4ipf5j6lpt48n', 'gejww1f1bmwjkye', '3hn0lf9uok08ldq',
  'vh05olo9b6bccbq', 't5tk34m9xfplgda', 'fnmiohol6e6v2m8', '34cexvtfxpwpujp',
  '3gb0tnp2buj5ovg', 's24mijo8w0kv9tv', '22o77yfqlqcvb0a', 'o2386isvqt30n8l',
  'vgygmni8ck9izbv',
]);

console.log('id'.padEnd(19) + '时间(UTC)'.padEnd(22) + 'res'.padEnd(6) + 'dur'.padEnd(5) + 'credits'.padEnd(9) + 'paid'.padEnd(7) + '归属');
let totAll = 0, totMine = 0, nAll = 0, nMine = 0, paidMine = 0, paidAll = 0;
for (const m of ms) {
  const mine = MINE.has(m.id);
  totAll += m.credits || 0; nAll++;
  if (m.paid) paidAll++;
  if (mine) { totMine += m.credits || 0; nMine++; if (m.paid) paidMine++; }
  console.log(
    m.id.padEnd(19) + m.createdAt.padEnd(22) + String(m.kelingKeyId).padEnd(6) + String(m.duration).padEnd(5) +
    String(m.credits).padEnd(9) + String(m.paid).padEnd(7) + (mine ? '本次测试' : '你的浏览器'),
  );
}
console.log('\n总任务数: ' + nAll + ' (总数 total=' + l.total + ')');
console.log('全部 credits 合计: ' + totAll + '  其中 paid=true(订阅额度覆盖): ' + paidAll + ' 条');
console.log('本次自动化测试: ' + nMine + ' 条, credits 合计 ' + totMine + ', paid=true ' + paidMine + ' 条');
fs.writeFileSync('/tmp/credits.json', JSON.stringify({ totAll, nAll, paidAll, totMine, nMine, paidMine }, null, 2));
