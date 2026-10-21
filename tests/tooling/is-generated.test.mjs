import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { isGeneratedFile } from '../../.agents/skills/impeccable/scripts/lib/is-generated.mjs';
import { findSourceFile } from '../../.agents/skills/impeccable/scripts/live/source-search.mjs';

function workspace(t, { git = true } = {}) {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'generated-file-checks-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  if (git) execFileSync('git', ['init', '--quiet', cwd]);
  return cwd;
}

test('source discovery treats shell substitutions in filenames as literal data', {
  skip: process.platform === 'win32',
}, (t) => {
  const cwd = workspace(t);
  fs.mkdirSync(path.join(cwd, 'src'));
  for (const [name, marker] of [
    ['$(touch dollar-marker).html', 'dollar-marker'],
    ['`touch backtick-marker`.html', 'backtick-marker'],
  ]) {
    const file = path.join(cwd, 'src', name);
    const query = `Source content for ${marker}`;
    fs.writeFileSync(file, query);

    assert.equal(findSourceFile({
      cwd,
      query,
      extensions: ['.html'],
      fileFilter: (candidate) => !isGeneratedFile(candidate, { cwd }),
    }), file);
    assert.equal(fs.existsSync(path.join(cwd, marker)), false);
  }
});

test('generated-file classification preserves literal filenames and Git ignore semantics', (t) => {
  const cwd = workspace(t);
  fs.mkdirSync(path.join(cwd, 'ignored'));
  fs.writeFileSync(path.join(cwd, '.gitignore'), 'ignored/\n');
  const names = [
    'plain.html',
    'space name.html',
    "single'quote.html",
    '--help.html',
    '$HOME.html',
    'страница.html',
    ...(process.platform === 'win32' ? [] : [
      'double"quote.html',
      'line\nbreak.html',
      'back\\slash.html',
    ]),
  ];
  for (const name of names) {
    for (const dir of ['', 'ignored']) {
      const relative = path.join(dir, name);
      const absolute = path.join(cwd, relative);
      fs.writeFileSync(absolute, '<p>Editable source</p>');
      for (const input of [relative, absolute]) {
        assert.equal(isGeneratedFile(input, { cwd }), dir === 'ignored', input);
      }
    }
  }

  const tracked = path.join('ignored', 'tracked.html');
  fs.writeFileSync(path.join(cwd, tracked), '<p>Tracked source</p>');
  execFileSync('git', ['add', '--force', '--', tracked], { cwd });
  assert.equal(isGeneratedFile(tracked, { cwd }), false);
});

test('shell substitutions in parent directories remain literal path components', {
  skip: process.platform === 'win32',
}, (t) => {
  const cwd = workspace(t);
  for (const [name, marker] of [
    ['$(touch parent-dollar)', 'parent-dollar'],
    ['`touch parent-backtick`', 'parent-backtick'],
  ]) {
    const dir = path.join(cwd, name);
    fs.mkdirSync(dir);
    const file = path.join(dir, 'page.html');
    fs.writeFileSync(file, '<p>Editable source</p>');

    assert.equal(isGeneratedFile(path.relative(cwd, file), { cwd }), false);
    assert.equal(isGeneratedFile(file, { cwd }), false);
    assert.equal(fs.existsSync(path.join(cwd, marker)), false);
  }
});

test('generated headers and fallback behavior remain available without Git', (t) => {
  const cwd = workspace(t, { git: false });
  for (const [index, header] of [
    '@generated', 'GENERATED FILE', 'AUTO-GENERATED', 'DO NOT EDIT',
  ].entries()) {
    const name = `generated-${index}.html`;
    fs.writeFileSync(path.join(cwd, name), `<!-- ${header} -->`);
    assert.equal(isGeneratedFile(name, { cwd }), true);
  }
  fs.writeFileSync(path.join(cwd, 'source.html'), '<p>Editable source</p>');
  fs.writeFileSync(path.join(cwd, 'late-marker.html'), `${' '.repeat(300)}DO NOT EDIT`);
  assert.equal(isGeneratedFile('source.html', { cwd }), false);
  assert.equal(isGeneratedFile('late-marker.html', { cwd }), false);
  assert.equal(isGeneratedFile('missing.html', { cwd }), false);
});
