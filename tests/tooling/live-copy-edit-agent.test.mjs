import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  buildCopyEditBatchPrompt,
  runCopyEditPostApplyChecks,
} from '../../.agents/skills/impeccable/scripts/live-copy-edit-agent.mjs';
import { commitManualEdits } from '../../.agents/skills/impeccable/scripts/live-commit-manual-edits.mjs';
import { readBuffer, stageEntry } from '../../.agents/skills/impeccable/scripts/live/manual-edits-buffer.mjs';

function workspace(t, script) {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'copy-edit-checks-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  fs.writeFileSync(path.join(cwd, 'package.json'), JSON.stringify({
    scripts: { 'impeccable:manual-edit-validate': script },
  }));
  return cwd;
}

test('post-apply checks do not execute checkout-defined shell commands', (t) => {
  const cwd = workspace(t, 'echo executed > hook-ran.txt');
  fs.writeFileSync(path.join(cwd, 'page.html'), '<h1>Updated copy</h1>');

  const result = runCopyEditPostApplyChecks({ cwd, files: ['page.html'] });

  assert.equal(fs.existsSync(path.join(cwd, 'hook-ran.txt')), false);
  assert.equal(result.ok, true);
  assert.deepEqual(result.failures, []);
  assert.equal(result.warnings.some((warning) =>
    warning.file === 'package.json' && warning.reason === 'manual_edit_validation_skipped'), true);
});

test('an external validator is skipped with no files and after a built-in failure', (t) => {
  const cwd = workspace(t, `"${process.execPath}" payload.cjs`);
  fs.writeFileSync(path.join(cwd, 'payload.cjs'),
    "require('node:fs').writeFileSync('hook-ran.txt', 'executed');");
  fs.writeFileSync(path.join(cwd, 'broken.json'), '{');

  assert.equal(runCopyEditPostApplyChecks({ cwd }).ok, true);
  const result = runCopyEditPostApplyChecks({ cwd, files: ['broken.json'] });

  assert.equal(fs.existsSync(path.join(cwd, 'hook-ran.txt')), false);
  assert.equal(result.ok, false);
  assert.equal(result.failures[0].reason, 'invalid_json');
  assert.equal(JSON.stringify(result.warnings).includes('payload.cjs'), false);
});

test('built-in checks accept valid edits and reject syntax damage and markers', (t) => {
  const cwd = workspace(t, 'exit 1');
  fs.writeFileSync(path.join(cwd, 'copy.mjs'), 'export const copy = "Updated copy";');
  fs.writeFileSync(path.join(cwd, 'copy.json'), '{"copy":"Updated copy"}');
  const valid = runCopyEditPostApplyChecks({ cwd, files: ['copy.mjs', 'copy.json'] });
  assert.equal(valid.ok, true);

  fs.writeFileSync(path.join(cwd, 'copy.mjs'), 'export const copy = ;');
  fs.writeFileSync(path.join(cwd, 'copy.json'), '{');
  fs.writeFileSync(path.join(cwd, 'page.html'), '<h1 data-impeccable-editable="true">Copy</h1>');
  const invalid = runCopyEditPostApplyChecks({ cwd, files: ['copy.mjs', 'copy.json', 'page.html'] });
  assert.equal(invalid.ok, false);
  assert.deepEqual(invalid.failures.map((failure) => failure.reason), [
    'invalid_js', 'invalid_json', 'leftover_impeccable_marker',
  ]);
});

test('projects without a usable hook keep the no-hook result contract', (t) => {
  const cwd = workspace(t, null);
  for (const script of [null, false, 1, [], {}, '', '   ']) {
    fs.writeFileSync(path.join(cwd, 'package.json'), JSON.stringify({
      scripts: { 'impeccable:manual-edit-validate': script },
    }));
    assert.deepEqual(runCopyEditPostApplyChecks({ cwd }), { ok: true, failures: [], warnings: [] });
  }
  for (const content of ['{}', '{']) {
    fs.writeFileSync(path.join(cwd, 'package.json'), content);
    assert.deepEqual(runCopyEditPostApplyChecks({ cwd }), { ok: true, failures: [], warnings: [] });
  }
  fs.unlinkSync(path.join(cwd, 'package.json'));
  assert.deepEqual(runCopyEditPostApplyChecks({ cwd }), { ok: true, failures: [], warnings: [] });
});

test('normal and repair prompts withhold authorization to run checkout validation', () => {
  for (const batch of [{ entries: [] }, { entries: [], repair: { failures: [] } }]) {
    const prompt = buildCopyEditBatchPrompt(batch);
    assert.match(prompt, /Do not run checkout-defined validation commands/);
    assert.match(prompt, /Applying copy edits does not authorize executing project code/);
    assert.doesNotMatch(prompt, /it must pass after edits/);
  }
});

for (const mode of ['normal', 'automatic repair', 'repair only']) {
  test(`${mode} applies and clears copy edits without executing the package hook`, async (t) => {
    const cwd = workspace(t, 'echo executed > hook-ran.txt');
    const source = path.join(cwd, 'page.html');
    const entry = {
      id: 'copy-entry',
      pageUrl: '/',
      ops: [{
        ref: 'heading',
        originalText: 'Original heading',
        newText: 'Updated heading',
        sourceHint: { file: 'page.html', line: 1 },
      }],
    };
    const clean = '<h1>Updated heading</h1>';
    const damaged = '<h1 data-impeccable-editable="true">Updated heading</h1>';
    fs.writeFileSync(source, mode === 'repair only' ? damaged : '<h1>Original heading</h1>');
    stageEntry(cwd, entry);
    let attempts = 0;

    const result = await commitManualEdits({
      cwd,
      pageUrl: '/',
      provider: 'chat',
      repairOnly: mode === 'repair only',
      applyBatchToSource: async (batch) => {
        attempts += 1;
        fs.writeFileSync(source, mode === 'automatic repair' && !batch.repair ? damaged : clean);
        return { status: 'done', appliedEntryIds: [entry.id], files: ['page.html'], notes: [] };
      },
    });

    assert.equal(fs.existsSync(path.join(cwd, 'hook-ran.txt')), false);
    assert.equal(fs.readFileSync(source, 'utf-8'), clean);
    assert.equal(result.cleared, 1);
    assert.equal(result.applied.length, 1);
    assert.deepEqual(result.failed, []);
    assert.deepEqual(readBuffer(cwd).entries, []);
    assert.equal(result.warnings.some((warning) => warning.reason === 'manual_edit_validation_skipped'), true);
    assert.equal(attempts, mode === 'automatic repair' ? 2 : 1);
    if (mode !== 'normal') assert.equal(result.repair.status, 'repaired');
  });
}
