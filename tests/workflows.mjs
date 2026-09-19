import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parse } from 'yaml';

const workflow = async (name) => parse(await readFile(`.github/workflows/${name}.yml`, 'utf8'));
const ci = await workflow('ci');
const gate = ci.jobs.required;
assert.equal(gate.if, 'always()');
assert.deepEqual(new Set(gate.needs), new Set(Object.keys(ci.jobs).filter((id) => id !== 'required')));
assert.deepEqual(new Set(gate.steps.find((step) => step.uses === './actions/gate').with.required.split(' ')), new Set(gate.needs));
assert.ok(Object.hasOwn(ci.on, 'pull_request'));
assert.deepEqual(ci.on.push.branches, ['main']);
assert.ok(!ci.on.pull_request?.paths && !ci.on.pull_request?.['paths-ignore']);

const bun = await workflow('bun');
const steps = bun.jobs.verify.steps;
const validate = steps.find((step) => step.name === 'Validate lockfile root');
const install = steps.find((step) => step.run === 'bun install --frozen-lockfile');
const installDirectory = '${{ inputs.install-directory || inputs.working-directory }}';
assert.equal(validate['working-directory'], installDirectory);
assert.equal(install['working-directory'], installDirectory);
assert.equal(bun.jobs.verify.defaults.run['working-directory'], '${{ inputs.working-directory }}');
const cacheKey = steps.find((step) => step.uses?.startsWith('actions/cache@')).with.key;
assert.ok(!cacheKey.includes('**/'));
assert.ok(cacheKey.includes("format('{0}/bun.lock', inputs.install-directory || inputs.working-directory)"));
assert.ok(cacheKey.includes("format('{0}/bun.lockb', inputs.install-directory || inputs.working-directory)"));

function shell(command, cwd, success = true) {
  const result = spawnSync('bash', ['--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', command], { cwd, encoding: 'utf8' });
  if (success) assert.equal(result.status, 0, result.stdout + result.stderr);
  else assert.notEqual(result.status, 0, 'Expected failure: ' + command);
}
async function json(path, value) {
  await writeFile(path, JSON.stringify(value));
}

const fixtures = await mkdtemp(join(tmpdir(), 'automation-bun-'));
try {
  // Real, network-free frozen installs. The workflow must install at the lock root
  // while allowing callers to execute checks in a nested independent app or member.
  for (const [layout, binary] of [['root', false], ['root', true], ['nested', false], ['workspace', false], ['workspace', true]]) {
    const root = join(fixtures, `${layout}-${binary}`);
    const installRoot = layout === 'nested' ? join(root, 'frontend') : root;
    const commandRoot = layout === 'workspace' ? join(root, 'packages/app') : installRoot;
    await mkdir(join(installRoot, 'local'), { recursive: true });
    await mkdir(commandRoot, { recursive: true });
    await json(join(installRoot, 'local/package.json'), { name: 'fixture-dep', version: '1.0.0', main: 'index.js' });
    await writeFile(join(installRoot, 'local/index.js'), 'module.exports = 42;\n');
    const manifest = { name: 'fixture', dependencies: { 'fixture-dep': 'file:./local' } };
    if (layout === 'workspace') {
      manifest.workspaces = ['packages/*'];
      await json(join(commandRoot, 'package.json'), { name: 'fixture-app', version: '1.0.0' });
    }
    await json(join(installRoot, 'package.json'), manifest);
    if (binary) await writeFile(join(installRoot, 'bunfig.toml'), '[install]\nsaveTextLockfile = false\n');
    shell('bun install --ignore-scripts', installRoot);
    const lock = join(installRoot, binary ? 'bun.lockb' : 'bun.lock');
    const original = await readFile(lock);
    if (layout === 'nested') {
      // An independent root lock must not influence the selected nested project.
      await writeFile(join(root, 'bun.lock'), 'unrelated lock\n');
    }
    await rm(join(installRoot, 'node_modules'), { recursive: true, force: true });
    shell(validate.run, installRoot);
    shell(install.run, installRoot);
    shell('bun -e \'if (require("fixture-dep") !== 42) process.exit(1)\'', commandRoot);
    assert.deepEqual(await readFile(lock), original);
    if (layout === 'nested') assert.equal(await readFile(join(root, 'bun.lock'), 'utf8'), 'unrelated lock\n');
    await json(join(installRoot, 'package.json'), { ...manifest, dependencies: { ...manifest.dependencies, missing: 'file:./missing' } });
    shell(install.run, installRoot, false);
  }
  const invalid = join(fixtures, 'invalid');
  await mkdir(invalid);
  await json(join(invalid, 'package.json'), { name: 'fixture' });
  for (const other of ['package-lock.json', 'pnpm-lock.yaml', 'yarn.lock']) {
    await writeFile(join(invalid, other), '{}');
  }
  shell(validate.run, invalid, false);
  await writeFile(join(invalid, 'bun.lock'), '{}');
  await writeFile(join(invalid, 'bun.lockb'), 'binary');
  shell(validate.run, invalid, false);
} finally {
  await rm(fixtures, { recursive: true, force: true });
}
console.log('Workflow contracts and real Bun root, nested, workspace, text/binary frozen installs passed.');

// Metadata policy must refresh on holds and reviews without checking out consumer code.
const policyWrapper = await workflow('policy');
assert.deepEqual(policyWrapper.permissions, { contents: 'read', 'pull-requests': 'read' });
assert.ok(!policyWrapper.on.pull_request_target);
for (const type of ['synchronize', 'edited', 'labeled', 'unlabeled', 'review_requested', 'review_request_removed', 'converted_to_draft']) {
  assert.ok(policyWrapper.on.pull_request.types.includes(type));
}
assert.deepEqual(policyWrapper.on.pull_request_review.types, ['submitted', 'edited', 'dismissed']);
assert.equal(policyWrapper.concurrency.group, 'pr-policy-${{ github.event.pull_request.number }}');
assert.equal(policyWrapper.concurrency['cancel-in-progress'], true);
assert.equal(policyWrapper.jobs.policy.name, 'ci / policy');
const policyWorkflow = await workflow('pr-policy');
assert.deepEqual(policyWorkflow.permissions, { contents: 'read', 'pull-requests': 'read' });
assert.equal(policyWorkflow.jobs.policy.name, 'ci / policy');
assert.ok(!policyWorkflow.jobs.policy.steps.some((step) => step.uses?.startsWith('actions/checkout@')));
assert.deepEqual(policyWorkflow.jobs.policy.steps.map((step) => step.uses), ['edbfi/automation/actions/pr-policy@v3.0.1']);
