import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const serverScript = fileURLToPath(new URL('../../.agents/skills/impeccable/scripts/live-server.mjs', import.meta.url));

/** Start the real HTTP server in an isolated project and clean it up after the test. */
async function startServer(t, setup = () => {}) {
  const temporary = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'live-source-')));
  const cwd = path.join(temporary, 'project');
  fs.mkdirSync(cwd);
  const options = setup({ cwd, temporary }) || {};
  const child = spawn(process.execPath, [...(options.nodeArgs || []), serverScript], {
    cwd: options.cwd || cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit');
      child.kill();
      await exited;
    }
    fs.rmSync(temporary, { recursive: true, force: true });
  });
  const origin = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(new Error(`Server startup timed out: ${output}`)), 5000);
    child.once('error', reject);
    child.once('exit', () => {
      clearTimeout(timer);
      reject(new Error(`Server exited during startup: ${output}`));
    });
    child.stderr.on('data', data => { output += data; });
    child.stdout.on('data', data => {
      output += data;
      const match = output.match(/running on (http:\/\/localhost:\d+)/);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    });
  });
  // Obtain the token through the same public script available to preview code.
  const script = await (await fetch(`${origin}/live.js`)).text();
  const token = script.match(/window\.__IMPECCABLE_TOKEN__ = '([^']+)'/)[1];
  return {
    cwd,
    temporary,
    source: (file, suppliedToken = token) => fetch(`${origin}/source?${new URLSearchParams({ token: suppliedToken, path: file })}`),
  };
}

test('source rejects a project symlink to an external file', { timeout: 10000 }, async t => {
  const server = await startServer(t, ({ cwd, temporary }) => {
    const secret = path.join(temporary, 'secret.txt');
    fs.writeFileSync(secret, 'external sentinel');
    fs.symlinkSync(secret, path.join(cwd, 'leak.html'));
    fs.writeFileSync(path.join(cwd, 'index.html'), '<main>ordinary source</main>');
  });
  const ordinary = await server.source('index.html');
  assert.equal(ordinary.status, 200);
  assert.equal(await ordinary.text(), '<main>ordinary source</main>');
  const response = await server.source('leak.html');
  assert.equal(response.status, 403);
  assert.doesNotMatch(await response.text(), /external sentinel/);
});

test('source rejects parent links, chained links, and sibling-prefix escapes', { timeout: 10000 }, async t => {
  const server = await startServer(t, ({ cwd }) => {
    const sibling = `${cwd}-private`;
    fs.mkdirSync(sibling);
    fs.writeFileSync(path.join(sibling, 'secret.html'), 'external sentinel');
    fs.symlinkSync(sibling, path.join(cwd, 'linked-directory'), 'junction');
    fs.symlinkSync('linked-directory/secret.html', path.join(cwd, 'chain.html'));
  });
  for (const file of ['linked-directory/secret.html', 'chain.html']) {
    const response = await server.source(file);
    assert.equal(response.status, 403, file);
    assert.doesNotMatch(await response.text(), /external sentinel/);
  }
  const absolute = await server.source(`${server.cwd}-private/secret.html`);
  assert.equal(absolute.status, 400);
  assert.doesNotMatch(await absolute.text(), /external sentinel/);
  const relativeSibling = await server.source(`../${path.basename(server.cwd)}-private/secret.html`);
  assert.equal(relativeSibling.status, 403);
  assert.doesNotMatch(await relativeSibling.text(), /external sentinel/);
});

test('source preserves templates, generated JSON, safe aliases, and live rereads', { timeout: 10000 }, async t => {
  const files = {
    'src/page.html': '<main>first revision</main>',
    'src/page.astro': '---\nconst title = "Example";\n---\n<h1>{title}</h1>',
    'src/page.vue': '<template><h1>Example</h1></template>',
    'src/page.tsx': 'export default () => <h1>Example</h1>;',
    'node_modules/.impeccable-live/session/manifest.json': '{"id":"session"}',
    'node_modules/.impeccable-live/session/params.json': '{"1":[]}',
    'node_modules/.impeccable-live/session/v1.svelte': '<h1>Example</h1><style>h1{color:red}</style>',
    'src/version..html': '<h1>Dots in a filename</h1>',
    'src/%2e%2e%2fpage.html': '<h1>Literal encoded filename</h1>',
  };
  const server = await startServer(t, ({ cwd, temporary }) => {
    for (const [file, content] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(cwd, file)), { recursive: true });
      fs.writeFileSync(path.join(cwd, file), content);
    }
    fs.symlinkSync('src/page.html', path.join(cwd, 'alias.html'));
    fs.symlinkSync(path.join(cwd, 'src'), path.join(cwd, 'alias-directory'), 'junction');
    const alias = path.join(temporary, 'project-alias');
    fs.symlinkSync(cwd, alias, 'junction');
    return { cwd: alias };
  });
  for (const [file, content] of Object.entries(files)) {
    const response = await server.source(file);
    assert.equal(response.status, 200, file);
    assert.equal(response.headers.get('content-type'), 'text/html; charset=utf-8');
    assert.equal(await response.text(), content);
  }
  for (const file of ['alias.html', 'alias-directory/page.html', './src/page.html']) {
    const response = await server.source(file);
    assert.equal(response.status, 200, file);
    assert.equal(await response.text(), files['src/page.html']);
  }
  fs.writeFileSync(path.join(server.cwd, 'src/page.html'), '<main>second revision</main>');
  assert.equal(await (await server.source('src/page.html')).text(), '<main>second revision</main>');
});

test('source rejects invalid paths and non-files while retaining authentication and missing-file responses', { timeout: 10000 }, async t => {
  const server = await startServer(t, ({ cwd, temporary }) => {
    fs.writeFileSync(path.join(temporary, 'secret.html'), 'external sentinel');
    fs.mkdirSync(path.join(cwd, 'directory'));
    fs.symlinkSync('missing', path.join(cwd, 'dangling.html'));
    fs.symlinkSync('loop.html', path.join(cwd, 'loop.html'));
    if (process.platform !== 'win32') execFileSync('mkfifo', [path.join(cwd, 'pipe.html')]);
  });
  const cases = [
    ['', 400], ['\0', 400], ['/etc/passwd', 400], ['C:\\private\\secret', 400],
    ['\\\\server\\share\\secret', 400], ['../secret.html', 403], ['./../secret.html', 403],
    ['.', 403], ['directory', 403], ['loop.html', 403],
    ['missing.html', 404], ['dangling.html', 404],
  ];
  if (process.platform !== 'win32') cases.push(['pipe.html', 403]);
  for (const [file, expectedStatus] of cases) {
    const response = await server.source(file);
    assert.equal(response.status, expectedStatus, file);
    assert.doesNotMatch(await response.text(), /external sentinel/);
  }
  const unauthorized = await server.source('../secret.html', 'wrong-token');
  assert.equal(unauthorized.status, 401);
});

for (const replacement of ['file-symlink', 'parent-symlink', 'regular-file']) {
  test(`source rejects ${replacement} substitution at file open`, { timeout: 10000 }, async t => {
    const server = await startServer(t, ({ cwd, temporary }) => {
      const directory = path.join(cwd, 'src');
      fs.mkdirSync(directory);
      const target = path.join(directory, 'page.html');
      fs.writeFileSync(target, '<main>ordinary source</main>');
      const outside = path.join(temporary, 'private');
      fs.mkdirSync(outside);
      fs.writeFileSync(path.join(outside, 'page.html'), 'external sentinel');
      const preload = path.join(temporary, 'replace-at-open.cjs');
      // Interpose only at the OS file-open boundary to make the race deterministic.
      fs.writeFileSync(preload, `
        const fs = require('node:fs');
        const originalOpen = fs.openSync;
        let replaced = false;
        fs.openSync = function(file, ...args) {
          if (!replaced && file === ${JSON.stringify(target)}) {
            replaced = true;
            const mode = ${JSON.stringify(replacement)};
            if (mode === 'parent-symlink') {
              fs.renameSync(${JSON.stringify(directory)}, ${JSON.stringify(`${directory}-saved`)});
              fs.symlinkSync(${JSON.stringify(outside)}, ${JSON.stringify(directory)}, 'junction');
            } else {
              fs.renameSync(file, file + '.saved');
              if (mode === 'file-symlink') fs.symlinkSync(${JSON.stringify(path.join(outside, 'page.html'))}, file);
              else fs.copyFileSync(${JSON.stringify(path.join(outside, 'page.html'))}, file);
            }
          }
          return originalOpen.call(this, file, ...args);
        };
      `);
      return { nodeArgs: ['--require', preload] };
    });
    const response = await server.source('src/page.html');
    assert.equal(response.status, 403);
    assert.doesNotMatch(await response.text(), /external sentinel/);
  });
}
