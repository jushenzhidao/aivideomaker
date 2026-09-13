// Unit-test the submit queue against a mock upstream. No real API calls.
import { SubmitQueue } from './submit-queue.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function mockAvm({ pollUntilDone = 3, failFullTimes = 0 } = {}) {
  let n = 0;
  let fullLeft = failFullTimes;
  const state = new Map();
  const log = [];
  return {
    log,
    createCalls: 0,
    async create() {
      this.createCalls++;
      if (fullLeft > 0) {
        fullLeft--;
        throw new Error('ai.minimaxH3: The queue is full. The premium plan can only run 2 task at a time.');
      }
      const id = 't' + ++n;
      state.set(id, 0);
      return id;
    },
    async getModel(id) {
      const k = (state.get(id) ?? 0) + 1;
      state.set(id, k);
      return { id, taskStatus: k >= pollUntilDone ? 'succeed' : 'processing' };
    },
  };
}

function mk(avm, opts = {}) {
  return new SubmitQueue({
    avm,
    maxConcurrent: 2,
    pollMs: 30,
    baseDelayMs: 40,
    maxAttempts: 6,
    log: (m) => avm.log.push(m),
    ...opts,
  });
}

console.log('=== 用例1：5 个提交，并发上限 2 ===');
{
  const avm = mockAvm({ pollUntilDone: 3 });
  const q = mk(avm);
  let peak = 0;
  const timer = setInterval(() => { peak = Math.max(peak, q.running.size); }, 5);
  const ids = await Promise.all(Array.from({ length: 5 }, () => q.submit({ content: 'x' })));
  // wait for all slots to drain
  while (q.running.size > 0 || q.pending.length > 0) await sleep(30);
  clearInterval(timer);
  console.log('  taskIds           :', ids.join(', '));
  console.log('  peak running      :', peak, peak <= 2 ? '<= 2 ✓' : '✗ 超出并发上限');
  console.log('  served / rejected :', q.served, '/', q.rejected);
  console.log('  延迟提示打印次数   :', avm.log.filter((m) => m.includes('delayed until a slot')).length);
  console.log('  全部完成          :', ids.length === 5 ? 'YES ✓' : 'NO ✗');
}

console.log('\n=== 用例2：上游先回 2 次 "queue is full"，再成功 ===');
{
  const avm = mockAvm({ pollUntilDone: 2, failFullTimes: 2 });
  const q = mk(avm);
  const t0 = Date.now();
  const id = await q.submit({ content: 'x' });
  console.log('  taskId            :', id);
  console.log('  耗时              :', Math.round((Date.now() - t0) / 1000 * 10) / 10 + 's（含退避等待）');
  console.log('  create 调用次数    :', avm.createCalls, '(1 次成功 + 2 次被拒)');
  console.log('  delayed_total     :', q.delayed);
  console.log('  打印的回退日志     :');
  for (const m of avm.log.filter((m) => m.includes('UPSTREAM FULL'))) console.log('    ' + m);
}

console.log('\n=== 用例3：上游持续满，超过重试上限后失败 ===');
{
  const avm = mockAvm({ failFullTimes: 99 });
  const q = mk(avm, { maxAttempts: 3 });
  try {
    await q.submit({ content: 'x' });
    console.log('  未按预期失败 ✗');
  } catch (e) {
    console.log('  最终错误          :', String(e.message).slice(0, 70));
    console.log('  重试次数          :', avm.createCalls);
    console.log('  giving-up 日志    :', avm.log.filter((m) => m.includes('giving up')).length ? '已打印 ✓' : '未打印 ✗');
    console.log('  rejected_total    :', q.rejected);
  }
}

console.log('\n=== 用例4：槽位在任务结束后释放 ===');
{
  const avm = mockAvm({ pollUntilDone: 2 });
  const q = mk(avm);
  await q.submit({ content: 'a' });
  console.log('  提交后 running    :', q.running.size);
  while (q.running.size > 0) await sleep(30);
  console.log('  任务终态后 running:', q.running.size, q.running.size === 0 ? '✓ 已释放' : '✗');
}

console.log('\n全部用例执行完毕（无真实 API 调用）。');
