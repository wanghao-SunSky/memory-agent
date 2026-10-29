// Run the actual inline script with controlled responses, without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(0, 'utf8');

function harness() {
  class Element {
    constructor() {
      this.value = ''; this.textContent = ''; this.hidden = false;
      this.disabled = false; this.children = []; this.listeners = {};
      this.classList = { toggle() {} };
    }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
  }
  const elements = new Map(), requests = [], timers = [];
  const el = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  el('space').value = 'work';
  el('content').value = 'first identity private message';
  el('question').value = 'question';
  el('method').value = 'recall';
  el('answer').hidden = true;
  const context = vm.createContext({
    document: { getElementById: el, createElement: () => new Element() },
    AbortController, confirm: () => true,
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    clearTimeout() {},
    fetch: (path, options) => new Promise((resolve, reject) => {
      requests.push({ path, options, reject, taken: false,
        resolve: (data, code = 200) => resolve({ ok: code < 400, status: code, json: async () => data }) });
    }),
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  const take = path => {
    const req = requests.find(r => !r.taken && r.path === path);
    assert.ok(req, 'expected request: ' + path);
    req.taken = true;
    return req;
  };
  const submit = id => el(id).listeners.submit({ preventDefault() {} });
  return { el, run, take, submit, requests, timers };
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const cases = {
  async connect_success() {
    const h = harness(), pending = h.run('connect()'), req = h.take('/me');
    h.run('clearPrivate()');
    req.resolve({ owner: 'previous-owner' }); await pending;
    assert.equal(h.el('owner').textContent, '');
    assert.equal(h.el('authStatus').textContent, '');
    assert.ok(!h.requests.some(r => r.path.startsWith('/memories')));
  },
  async connect_failure() {
    const h = harness(), pending = h.run('connect()'), req = h.take('/me');
    h.run('clearPrivate()');
    h.el('authStatus').textContent = 'new connection';
    req.reject(Error('old connection error')); await pending;
    assert.equal(h.el('authStatus').textContent, 'new connection');
    assert.equal(h.el('memoryStatus').textContent, '');
  },
  async write_success() {
    const h = harness(), old = h.submit('writeForm'), oldReq = h.take('/episodes');
    h.run('clearPrivate()');
    assert.equal(h.el('writeButton').disabled, false);
    h.el('content').value = 'new identity message';
    const next = h.submit('writeForm'), newReq = h.take('/episodes');
    oldReq.resolve({ job_id: 101 }); await old;
    assert.equal(h.run('currentJob'), null);
    assert.equal(h.el('writeStatus').textContent, '');
    assert.equal(h.el('writeButton').disabled, true, 'old finally must not re-enable a new request');
    assert.ok(!h.requests.some(r => r.path === '/jobs/101'));
    newReq.resolve({ job_id: 202 }); await tick();
    h.take('/jobs/202').resolve({ status: 'done', result: { ops: [{}] } }); await tick();
    h.take('/memories?space=work').resolve({ memories: [] }); await next;
    assert.equal(h.run('currentJob'), 202);
    assert.equal(h.el('writeButton').disabled, false);
    assert.match(h.el('writeStatus').textContent, /已保存并完成提取/);
  },
  async write_failure() {
    const h = harness(), pending = h.submit('writeForm'), req = h.take('/episodes');
    h.run('clearPrivate()'); h.el('writeStatus').textContent = 'new work';
    req.reject(Error('old write error')); await pending;
    assert.equal(h.el('writeStatus').textContent, 'new work');
    assert.equal(h.el('checkJob').hidden, true);
  },
  async delete_before_response() {
    const h = harness(), pending = h.run("removeMemory(7, document.getElementById('deleteButton'))");
    const req = h.take('/memories/7');
    h.run('clearPrivate()'); h.el('memoryStatus').textContent = 'new space'; h.el('answer').hidden = false;
    req.resolve({ result: 'verified', scope: { sibling_facts_kept: [314159] } }); await pending;
    assert.equal(h.el('memoryStatus').textContent, 'new space');
    assert.equal(h.el('answer').hidden, false);
    assert.ok(!h.requests.some(r => r.path.includes('?space=')));
  },
  async delete_during_reload() {
    const h = harness(), pending = h.run("removeMemory(7, document.getElementById('deleteButton'))");
    h.take('/memories/7').resolve({ result: 'verified', scope: { sibling_facts_kept: [314159] } }); await tick();
    const reload = h.take('/memories?space=work');
    h.run('clearPrivate()'); h.el('memoryStatus').textContent = 'new space'; h.el('answer').hidden = false;
    reload.resolve({ memories: [] }); await pending;
    assert.equal(h.el('memoryStatus').textContent, 'new space');
    assert.equal(h.el('answer').hidden, false);
  },
  async delete_failure() {
    const h = harness(), pending = h.run("removeMemory(7, document.getElementById('deleteButton'))");
    const req = h.take('/memories/7');
    h.run('clearPrivate()'); h.el('memoryStatus').textContent = 'new space';
    req.reject(Error('old delete error')); await pending;
    assert.equal(h.el('memoryStatus').textContent, 'new space');
  },
  async retry_success() {
    const h = harness(); h.run('currentJob=101');
    const pending = h.el('retryJob').listeners.click(), req = h.take('/jobs/101/retry');
    h.run('clearPrivate(); currentJob=202'); h.el('writeStatus').textContent = 'new job';
    req.resolve({ ok: true }); await pending;
    assert.equal(h.el('writeStatus').textContent, 'new job');
    assert.ok(!h.requests.some(r => r.path === '/jobs/202' || r.path === '/jobs/101'));
  },
  async retry_failure() {
    const h = harness(); h.run('currentJob=101');
    const pending = h.el('retryJob').listeners.click(), req = h.take('/jobs/101/retry');
    h.run('clearPrivate()'); h.el('writeStatus').textContent = 'new job';
    req.reject(Error('old retry error')); await pending;
    assert.equal(h.el('writeStatus').textContent, 'new job');
    assert.equal(h.el('retryJob').disabled, false);
  },
  async poll_after_switch() {
    const h = harness(); h.run('currentJob=101');
    const pending = h.run('pollJob()');
    h.take('/jobs/101').resolve({ status: 'running', attempts: 1 }); await tick();
    const sleep = h.timers.find(t => t.delay === 1000);
    assert.ok(sleep);
    h.run('clearPrivate()'); h.el('writeStatus').textContent = 'new job';
    sleep.fn(); await pending;
    assert.equal(h.requests.filter(r => r.path === '/jobs/101').length, 1, 'must not poll old job with new identity');
    assert.equal(h.el('writeStatus').textContent, 'new job');
  },
  async poll_failure() {
    const h = harness(); h.run('currentJob=101');
    const pending = h.run('pollJob()'), req = h.take('/jobs/101');
    h.run('clearPrivate()'); h.el('writeStatus').textContent = 'new job';
    req.reject(Error('old polling error')); await pending;
    assert.equal(h.el('writeStatus').textContent, 'new job');
    assert.equal(h.el('checkJob').hidden, true);
  },
  async question_finally() {
    const h = harness(), old = h.submit('questionForm'), req = h.take('/recall');
    h.run('clearPrivate()'); assert.equal(h.el('askButton').disabled, false);
    const next = h.submit('questionForm'), newReq = h.take('/recall');
    req.resolve({ answer: 'old private answer' }); await old;
    assert.equal(h.el('askButton').disabled, true);
    assert.equal(h.el('answer').hidden, true);
    newReq.resolve({ answer: 'new answer', abstain: true }); await next;
    assert.equal(h.el('askButton').disabled, false);
    assert.equal(h.el('answer').children[0].textContent, 'new answer');
  },
};

(async () => {
  const name = process.argv[2];
  assert.ok(cases[name], 'unknown scenario: ' + name);
  await cases[name]();
  process.stdout.write(name + ': passed\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
