// Exercise the complete installed overlay through SSE messages and user input.
// No agent process or real helper server is started. Layout is fixed because
// jsdom implements the DOM, not a rendering engine.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');
const scripts = path.resolve(__dirname, '../../.agents/skills/impeccable/scripts');

async function until(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('overlay did not reach the expected protocol state');
}

async function overlay({ delayRenewal = false, offscreen = false, component = null } = {}) {
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
    if (component && url.includes('/source?')) {
      const sourcePath = new URL(url).searchParams.get('path');
      if (sourcePath.endsWith('manifest.json') && component.holdManifest) {
        await new Promise(resolve => { component.releaseManifest = resolve; });
      }
      if (sourcePath.endsWith('manifest.json')) component.manifestReads = (component.manifestReads || 0) + 1;
      return { ok: true, text: async () => JSON.stringify(sourcePath.endsWith('manifest.json') ? component.manifest : {}) };
    }
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
    const context = dom.getInternalVMContext();
    const script = new vm.Script(fs.readFileSync(path.join(scripts, file), 'utf8'), {
      filename: file,
      // The compiled modules/dev-server boundary is a fixture. The complete
      // overlay's selection, error card, manifest refresh and Retry stay real.
      importModuleDynamically: async url => {
        assert.ok(component, 'unexpected component import');
        let exports;
        if (new URL(url).pathname.endsWith('/runtime.js')) {
          exports = {
            mount(Component, { target }) {
              const section = target.ownerDocument.createElement('section');
              section.id = 'user';
              section.textContent = `Rendered variant ${Component.variant}`;
              section.dataset.fixtureVariant = String(Component.variant);
              target.replaceChildren(section);
              return { target };
            },
            unmount(instance) { instance.target.replaceChildren(); },
          };
        } else {
          const variant = Number(new URL(url).pathname.match(/v(\d+)\.svelte$/)?.[1]);
          assert.ok(variant > 0, 'unexpected component module');
          component.imports.push(variant);
          if (variant === (component.holdVariant || 2) && component.holdImport) {
            await new Promise(resolve => { component.releaseImport = resolve; });
            component.importCompleted = true;
          }
          if (variant === (component.brokenVariant || 2) && component.broken) throw new Error('fixture variant failed to compile');
          exports = { default: { variant } };
        }
        const module = new vm.SyntheticModule(Object.keys(exports), function () {
          for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
        }, { context });
        await module.link(() => assert.fail('fixture modules have no imports'));
        await module.evaluate();
        return module;
      },
    });
    script.runInContext(context);
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

for (const repaired of [false, true]) {
  test(`Retry targets the failed component variant when repair ${repaired ? 'succeeds' : 'still fails'}`, async () => {
    const component = {
      broken: true, imports: [],
      manifest: {
        id: null, previewMode: 'svelte-component', framework: 'svelte', count: 2,
        componentDir: 'variants', runtimeModule: '/variants/runtime.js',
        originalMarkup: '<section id="user">User selection</section>',
      },
    };
    const o = await overlay({ component });
    try {
      o.configureUser();
      const sessionId = o.userGo();
      component.manifest.id = sessionId;
      o.send({ type: 'done', id: sessionId, previewMode: 'svelte-component', previewFile: 'variants/manifest.json' });
      await until(() => o.core.debugState().state === 'CYCLING');
      assert.equal(o.core.debugState().mountedSvelteVariant, 1);
      o.core.getById('impeccable-live-variant-next').click();
      await until(() => o.events.some(e => e.type === 'variant_mount_failed' && e.variant === 2)
        && o.core.debugState().visibleVariant === 1 && o.core.debugState().mountedSvelteVariant === 1);
      const priorAttempts = component.imports.length;
      component.broken = !repaired;
      o.core.root().querySelector('[data-impeccable-mount-retry]').click();
      await until(() => component.imports.length > priorAttempts);
      assert.equal(component.imports[priorAttempts], 2, 'Retry must import the failed variant, not the fallback');
      if (repaired) {
        await until(() => o.core.debugState().mountedSvelteVariant === 2);
        assert.equal(o.core.debugState().visibleVariant, 2);
        assert.equal(o.w.document.getElementById('user').dataset.fixtureVariant, '2');
        assert.equal(o.core.root().querySelector('[data-impeccable-mount-retry]'), null);
      } else {
        await until(() => o.events.filter(e => e.type === 'variant_mount_failed' && e.variant === 2).length === 2);
        await until(() => o.core.debugState().visibleVariant === 1 && o.core.debugState().mountedSvelteVariant === 1);
        assert.equal(o.w.document.getElementById('user').dataset.fixtureVariant, '1');
        assert.ok(o.core.root().querySelector('[data-impeccable-mount-retry]'), 'failed retry must keep its error card');
      }
      assert.equal(o.core.debugState().currentSessionId, sessionId);
      assert.equal(o.events.filter(e => e.type === 'generate').length, 1);
      assert.deepEqual(o.errors, []);
    } finally { o.dom.window.close(); }
  });
}

for (const boundary of ['manifest', 'import']) {
  for (const action of ['Discard', 'Accept', 'Exit', 'new generation']) {
    test(`${action} wins over Retry delayed at ${boundary}`, async () => {
      const component = {
        broken: true, imports: [],
        manifest: {
          id: null, previewMode: 'svelte-component', framework: 'svelte', count: 2,
          componentDir: 'variants', runtimeModule: '/variants/runtime.js',
          originalMarkup: '<section id="user">User selection</section>',
        },
      };
      const o = await overlay({ component });
      try {
        o.configureUser();
        const sessionId = o.userGo();
        component.manifest.id = sessionId;
        o.send({ type: 'done', id: sessionId, previewMode: 'svelte-component', previewFile: 'variants/manifest.json' });
        await until(() => o.core.debugState().state === 'CYCLING');
        o.core.getById('impeccable-live-variant-next').click();
        await until(() => o.events.some(e => e.type === 'variant_mount_failed' && e.variant === 2)
          && o.core.debugState().visibleVariant === 1 && o.core.debugState().mountedSvelteVariant === 1);
        component.holdManifest = boundary === 'manifest';
        component.holdImport = boundary === 'import';
        component.broken = false;
        const reads = component.manifestReads;
        o.core.root().querySelector('[data-impeccable-mount-retry]').click();
        await until(() => boundary === 'manifest' ? component.releaseManifest : component.releaseImport);
        const terminal = action === 'new generation' ? 'Discard' : action;
        const buttons = [...o.core.root().querySelectorAll('button')];
        const button = terminal === 'Discard' ? buttons.find(b => b.title === 'Discard all variants')
          : terminal === 'Exit' ? buttons.find(b => b.title === 'Exit live mode')
            : buttons.find(b => b.textContent === '\u2713 Accept');
        assert.ok(button, `${terminal} must be available while Retry is pending`);
        button.click();
        await until(() => o.core.debugState().currentSessionId === null);
        let replacement = null;
        if (action === 'new generation') {
          // Discard leaves picking active; select the original again through the UI.
          const user = o.w.document.getElementById('user');
          user.dispatchEvent(new o.w.MouseEvent('mousemove', { bubbles: true, clientX: 20, clientY: 20 }));
          user.click();
          assert.equal(o.core.debugState().state, 'CONFIGURING');
          replacement = o.userGo();
          assert.notEqual(replacement, sessionId);
        }
        const mountEvents = () => o.events.filter(e => e.id === sessionId && ['variant_mounted', 'variant_mount_failed'].includes(e.type)).length;
        const priorEvents = mountEvents();
        if (boundary === 'manifest') {
          component.releaseManifest();
          await until(() => component.manifestReads > reads);
        } else {
          component.releaseImport();
          await until(() => component.importCompleted);
        }
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(o.core.debugState().currentSessionId, replacement);
        assert.equal(o.core.debugState().state, replacement ? 'GENERATING' : terminal === 'Exit' ? 'IDLE' : 'PICKING');
        assert.equal(o.core.debugState().hasSvelteComponentSession, false);
        assert.equal(o.core.debugState().savedSession?.id || null, replacement);
        assert.equal(o.w.document.getElementById('user').dataset.fixtureVariant, terminal === 'Accept' ? '1' : undefined);
        assert.equal(o.core.root().querySelector('[data-impeccable-mount-retry]'), null);
        assert.equal(mountEvents(), priorEvents, 'stale completion must not report a mount or failure');
        assert.deepEqual(o.errors, []);
      } finally { o.dom.window.close(); }
    });
  }
}


test('a newer variant selection wins over a held Retry import', async () => {
  const component = {
    broken: true, imports: [],
    manifest: {
      id: null, previewMode: 'svelte-component', framework: 'svelte', count: 2,
      componentDir: 'variants', runtimeModule: '/variants/runtime.js',
      originalMarkup: '<section id="user">User selection</section>',
    },
  };
  const o = await overlay({ component });
  try {
    o.configureUser();
    const sessionId = o.userGo();
    component.manifest.id = sessionId;
    o.send({ type: 'done', id: sessionId, previewMode: 'svelte-component', previewFile: 'variants/manifest.json' });
    await until(() => o.core.debugState().state === 'CYCLING');
    o.core.getById('impeccable-live-variant-next').click();
    await until(() => o.events.some(e => e.type === 'variant_mount_failed' && e.variant === 2)
      && o.core.debugState().visibleVariant === 1 && o.core.debugState().mountedSvelteVariant === 1);
    component.broken = false;
    component.holdImport = true;
    o.core.root().querySelector('[data-impeccable-mount-retry]').click();
    await until(() => component.releaseImport);
    const previousMounts = o.events.filter(e => e.type === 'variant_mounted').length;
    o.core.getById('impeccable-live-variant-prev').click();
    await until(() => o.events.filter(e => e.type === 'variant_mounted').length > previousMounts);
    component.releaseImport();
    await until(() => component.importCompleted);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(o.core.debugState().currentSessionId, sessionId);
    assert.equal(o.core.debugState().visibleVariant, 1);
    assert.equal(o.core.debugState().mountedSvelteVariant, 1);
    assert.equal(o.w.document.getElementById('user').dataset.fixtureVariant, '1');
    assert.equal(o.events.filter(e => e.type === 'variant_mounted').length, previousMounts + 1);
    assert.deepEqual(o.errors, []);
  } finally { o.dom.window.close(); }
});


test('a superseded automatic repair cannot remove the newer Retry preview', async () => {
  const component = {
    broken: true, brokenVariant: 1, imports: [], holdVariant: 1,
    manifest: {
      id: null, previewMode: 'svelte-component', framework: 'svelte', count: 2,
      componentDir: 'variants', runtimeModule: '/variants/runtime.js',
      originalMarkup: '<section id="user">User selection</section>',
    },
  };
  const o = await overlay({ component });
  try {
    o.configureUser();
    const sessionId = o.userGo();
    component.manifest.id = sessionId;
    const done = { type: 'done', id: sessionId, previewMode: 'svelte-component', previewFile: 'variants/manifest.json' };
    o.send(done);
    await until(() => o.core.root().querySelector('[data-impeccable-mount-retry]')
      && !o.core.debugState().hasSvelteComponentSession);
    component.broken = false;
    component.holdImport = true;
    o.send(done);
    await until(() => component.releaseImport);
    component.holdImport = false;
    o.core.root().querySelector('[data-impeccable-mount-retry]').click();
    await until(() => o.core.debugState().mountedSvelteVariant === 1 && o.core.debugState().state === 'CYCLING');
    const mounts = o.events.filter(e => e.type === 'variant_mounted').length;
    component.releaseImport();
    await until(() => component.importCompleted);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(o.core.debugState().currentSessionId, sessionId);
    assert.equal(o.core.debugState().hasSvelteComponentSession, true);
    assert.equal(o.core.debugState().mountedSvelteVariant, 1);
    assert.equal(o.w.document.getElementById('user').dataset.fixtureVariant, '1');
    assert.equal(o.core.root().querySelector('[data-impeccable-mount-retry]'), null);
    assert.equal(o.events.filter(e => e.type === 'variant_mounted').length, mounts);
    assert.deepEqual(o.errors, []);
  } finally { o.dom.window.close(); }
});
