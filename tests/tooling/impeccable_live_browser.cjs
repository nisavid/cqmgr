// Exercise the complete installed overlay through SSE messages and user input.
// No agent process or real helper server is started. Layout is fixed because
// jsdom implements the DOM, not a rendering engine.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const scripts = path.resolve(__dirname, '../../.agents/skills/impeccable/scripts');

async function until(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('overlay did not reach the expected protocol state');
}

async function overlay({ delayRenewal = false, offscreen = false } = {}) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM('<!doctype html><body><section id="user">User selection</section><section id="agent">Agent target</section>', {
    url: 'http://localhost:3000', runScripts: 'outside-only',
    pretendToBeVisual: true, virtualConsole: console,
  });
  const w = dom.window;
  w.__IMPECCABLE_TOKEN__ = 'fixture-token';
  w.__IMPECCABLE_PORT__ = 9876;
  w.CSS = { escape: value => value };
  w.document.elementFromPoint = () => w.document.getElementById('user');
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.focus = w.scrollTo = () => {};
  w.confirm = () => true;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.HTMLElement.prototype.getBoundingClientRect = function () {
    const top = offscreen && this.id === 'agent' ? 2000 : 10;
    return { top, bottom: top + 100, left: 10, right: 310, x: 10, y: top, width: 300, height: 100 };
  };
  const claims = [], events = [], results = [];
  let releaseRenewal;
  w.fetch = async (url, options) => {
    const body = options?.body ? JSON.parse(options.body) : null;
    if (url.includes('/agent-target-claim?')) {
      claims.push(body);
      if (delayRenewal && body.eligible && claims.filter(c => c.eligible).length === 2) {
        await new Promise(resolve => { releaseRenewal = resolve; });
      }
    }
    if (url.includes('/manual-edit-commit?')) {
      return { ok: true, status: 202, json: async () => ({ status: 'started' }) };
    }
    if (url.includes('/agent-target-result?')) results.push(body);
    if (url.includes('/events?')) events.push(body);
    return { ok: true, json: async () => ({ ok: true, granted: true, pending: true, files: [], sessions: [], count: 0 }), text: async () => '' };
  };
  let source;
  w.EventSource = class {
    constructor() { source = this; this.readyState = 1; }
    close() {}
  };
  for (const file of ['live-browser-dom.js', 'live-browser-session.js', 'live-browser.js']) {
    w.eval(fs.readFileSync(path.join(scripts, file), 'utf8'));
  }
  await until(() => source?.onmessage);
  const core = w.__IMPECCABLE_LIVE_CHROME_CORE__;
  const send = message => source.onmessage({ data: JSON.stringify(message) });
  const sendTarget = (extra = {}) => send({ type: 'agent_target', targetId: 'target-1', selector: '#agent', action: 'polish', count: 1, ...extra });
  const configureUser = () => {
    core.getById('impeccable-live-pick-toggle').click();
    const user = w.document.getElementById('user');
    user.dispatchEvent(new w.MouseEvent('mousemove', { bubbles: true, clientX: 20, clientY: 20 }));
    user.click();
    assert.equal(core.debugState().state, 'CONFIGURING');
  };
  const userGo = () => {
    core.getById('impeccable-live-input').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    assert.equal(core.debugState().state, 'GENERATING');
    return core.debugState().currentSessionId;
  };
  return { w, dom, core, claims, events, results, errors, send, sendTarget, configureUser, userGo, renewed: () => !!releaseRenewal, release: () => releaseRenewal() };
}

for (const boundary of ['scroll', 'renewal']) {
  test(`a user generation survives a target delayed at ${boundary}`, async () => {
    const o = await overlay({ delayRenewal: true, offscreen: boundary === 'scroll' });
    try {
      o.configureUser();
      o.sendTarget();
      if (boundary === 'scroll') {
        await until(() => o.claims.length === 1);
      } else {
        await until(o.renewed);
      }
      const userSession = o.userGo();
      if (boundary === 'scroll') {
        o.w.dispatchEvent(new o.w.Event('scrollend'));
        await until(o.renewed);
      }
      o.release();
      await until(() => o.claims.some(c => c.eligible === false) || o.results.length);
      assert.equal(o.core.debugState().currentSessionId, userSession);
      assert.equal(o.core.debugState().state, 'GENERATING');
      assert.equal(o.events.filter(e => e.type === 'generate').length, 1);
      assert.equal(o.core.debugState().savedSession.pickedAnchor.id, 'user');
      assert.ok(o.claims.some(c => c.eligible === false && c.reason === 'session_active'));
      assert.equal(o.results.length, 0);
      assert.deepEqual(o.errors, []);
    } finally { o.dom.window.close(); }
  });
}

test('an idle target still starts exactly one generation', async () => {
  const o = await overlay();
  try {
    o.sendTarget();
    await until(() => o.results.length);
    assert.equal(o.results[0].ok, true);
    const generate = o.events.filter(e => e.type === 'generate');
    assert.equal(generate.length, 1);
    assert.equal(generate[0].element.id, 'agent');
    assert.equal(generate[0].id, o.core.debugState().currentSessionId);
    assert.deepEqual(o.errors, []);
  } finally { o.dom.window.close(); }
});

for (const boundary of ['scroll', 'renewal']) {
  test(`manual apply survives a target delayed at ${boundary}`, async () => {
    const o = await overlay({ delayRenewal: true, offscreen: boundary === 'scroll' });
    try {
      o.send({ type: 'manual_edit_stashed', pageUrl: '/', pendingCount: 1 });
      o.sendTarget();
      if (boundary === 'scroll') await until(() => o.claims.length === 1);
      else await until(o.renewed);
      const dock = o.core.getById('impeccable-live-pending-dock');
      dock.querySelector('button[aria-label^="Apply "]').click();
      assert.equal(o.core.debugState().pendingApplyInFlight, true);
      if (boundary === 'scroll') {
        o.w.dispatchEvent(new o.w.Event('scrollend'));
        await until(o.renewed);
      }
      o.release();
      await until(() => o.claims.some(c => c.eligible === false) || o.results.length);
      assert.equal(o.core.debugState().pendingApplyInFlight, true);
      assert.equal(o.core.debugState().currentSessionId, null);
      assert.ok(o.claims.some(c => c.eligible === false && c.reason === 'manual_apply_in_flight'));
      assert.equal(o.events.filter(e => e.type === 'generate').length, 0);
      assert.equal(o.results.length, 0);
      assert.deepEqual(o.errors, []);
    } finally { o.dom.window.close(); }
  });
}
