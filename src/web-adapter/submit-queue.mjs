// Upstream submit gate.
//
// aivideomaker caps concurrent generations per plan:
//   "The queue is full. The premium plan can only run 2 task at a time."
//
// A slot is occupied for the WHOLE lifetime of a task (until it reaches a
// terminal state), not just while the create request is in flight.  So a naive
// proxy that fires every request straight through gets 429/queue-full errors as
// soon as a third request arrives.
//
// This queue:
//   1. holds at most `maxConcurrent` tasks upstream (default 2, env AVM_MAX_CONCURRENT);
//   2. DELAYS the rest instead of failing — they start as slots free up;
//   3. when upstream still reports "queue is full" (e.g. tasks started outside
//      this process, like in the browser), logs it explicitly and retries with
//      linear backoff up to `maxAttempts`.
//
// The captcha gate is a separate, deliberately opt-in retry (AVM_RETRY_CAPTCHA=1)
// because waiting one out can take minutes and then spend money unexpectedly.

const TERMINAL = new Set(['succeed', 'success', 'completed', 'failed', 'error', 'cancelled', 'canceled']);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class SubmitQueue {
  /**
   * @param {object} deps
   * @param {object} deps.avm        AvmClient
   * @param {number} [deps.maxConcurrent]  upstream slots (default 2)
   * @param {number} [deps.maxAttempts]    retries when upstream says "queue is full"
   * @param {number} [deps.baseDelayMs]    first backoff step
   * @param {number} [deps.maxDelayMs]     backoff ceiling
   * @param {boolean} [deps.retryCaptcha]  also wait out the captcha gate
   * @param {Function} [deps.log]
   */
  constructor({
    avm,
    maxConcurrent = 2,
    pollMs = 15_000,
    watchTimeoutMs = 60 * 60_000,
    maxAttempts = 8,
    baseDelayMs = 20_000,
    maxDelayMs = 120_000,
    retryCaptcha = false,
    log = (...a) => console.warn(...a),
  } = {}) {
    this.avm = avm;
    this.maxConcurrent = maxConcurrent;
    this.pollMs = pollMs;
    this.watchTimeoutMs = watchTimeoutMs;
    this.maxAttempts = maxAttempts;
    this.baseDelayMs = baseDelayMs;
    this.maxDelayMs = maxDelayMs;
    this.retryCaptcha = retryCaptcha;
    this.log = log;

    this.running = new Set(); // entries holding an upstream slot
    this.pending = []; // queued submissions, FIFO
    this.served = 0;
    this.delayed = 0; // times we had to wait because upstream was full
    this.rejected = 0;
  }

  stats() {
    return {
      max_concurrent: this.maxConcurrent,
      running: this.running.size,
      queued: this.pending.length,
      running_tasks: [...this.running].map((e) => e.taskId).filter(Boolean),
      served_total: this.served,
      delayed_total: this.delayed,
      rejected_total: this.rejected,
      retry_captcha: this.retryCaptcha,
    };
  }

  /**
   * Queue a create.  Resolves with the task id as soon as upstream accepted it
   * (the caller's HTTP request does not have to wait for the video to finish).
   * Rejects only if the submission ultimately fails.
   */
  submit(params) {
    return new Promise((resolve, reject) => {
      this.pending.push({ params, resolve, reject, enqueuedAt: Date.now() });
      if (this.pending.length + this.running.size > this.maxConcurrent) {
        this.log(
          `[queue] upstream concurrency is capped at ${this.maxConcurrent}; ` +
            `${this.pending.length} request(s) delayed until a slot frees`,
        );
      }
      this._pump();
    });
  }

  _pump() {
    while (this.running.size < this.maxConcurrent && this.pending.length > 0) {
      this._start(this.pending.shift());
    }
  }

  async _start(item) {
    const entry = { taskId: null, params: item.params, startedAt: Date.now() };
    const waited = Date.now() - item.enqueuedAt;
    if (waited > 1_000) this.log(`[queue] starting delayed request (waited ${Math.round(waited / 1000)}s)`);
    this.running.add(entry);

    let id;
    try {
      id = await this._createWithRetry(item.params);
    } catch (e) {
      this.running.delete(entry);
      this.rejected++;
      item.reject(e);
      this._pump();
      return;
    }

    entry.taskId = id;
    this.served++;
    item.resolve(id);
    this._watch(entry); // releases the slot when the task reaches a terminal state
  }

  async _createWithRetry(params) {
    let attempt = 0;
    for (;;) {
      attempt++;
      try {
        const id = await this.avm.create(params);
        if (!id) {
          // The site signals "rejected" with an empty string rather than an error.
          const err = new Error('upstream rejected the request (empty task id)');
          err.code = 'REJECTED';
          throw err;
        }
        return id;
      } catch (e) {
        const msg = String(e?.message || '');
        const full = /queue is full/i.test(msg);
        const captcha = e?.code === 'CAPTCHA_REQUIRED';

        if (full && attempt < this.maxAttempts) {
          this.delayed++;
          const delay = Math.min(this.baseDelayMs * attempt, this.maxDelayMs);
          this.log(
            `[queue] UPSTREAM FULL (attempt ${attempt}/${this.maxAttempts}) — delaying ` +
              `${Math.round(delay / 1000)}s then retrying. upstream said: ${msg}`,
          );
          await sleep(delay);
          continue;
        }
        if (full) {
          this.log(`[queue] giving up after ${attempt} attempts — upstream still full: ${msg}`);
        }
        if (captcha && this.retryCaptcha && attempt < this.maxAttempts) {
          this.delayed++;
          const delay = Math.min(this.baseDelayMs * attempt, this.maxDelayMs);
          this.log(
            `[queue] CAPTCHA GATE CLOSED (attempt ${attempt}/${this.maxAttempts}) — delaying ` +
              `${Math.round(delay / 1000)}s then retrying`,
          );
          await sleep(delay);
          continue;
        }
        throw e;
      }
    }
  }

  async _watch(entry) {
    const t0 = Date.now();
    while (Date.now() - t0 < this.watchTimeoutMs) {
      await sleep(this.pollMs);
      let t = null;
      try {
        t = await this.avm.getModel(entry.taskId);
      } catch {
        continue; // transient; the slot is still ours
      }
      const st = String(t?.taskStatus || '').toLowerCase();
      if (TERMINAL.has(st)) {
        this.log(`[queue] slot released (${entry.taskId} -> ${st}) running=${this.running.size} queued=${this.pending.length}`);
        break;
      }
    }
    this.running.delete(entry);
    this._pump();
  }
}
