import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const exec = promisify(execFile), cli = fileURLToPath(new URL('./cli.mjs', import.meta.url));
const run = args => exec(process.execPath, [cli, ...args], { timeout: 10000 });

test('CLI demo, status, replay twice, export and pause work across real process restarts', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 's-trader-cli-')), demo = path.join(dir, 'demo');
  try {
    await run(['demo', '--out', demo]);
    const config = path.join(demo, 'config.json'), events = path.join(demo, 'events.jsonl'), db = path.join(dir, 'replay.sqlite');
    const args = ['replay', '--config', config, '--events', events, '--db', db];
    const first = JSON.parse((await run(args)).stdout), second = JSON.parse((await run(args)).stdout);
    assert.deepEqual(second, first); assert.equal(first.fills, 6); assert.equal(first.mode, 'paper'); assert.equal(first.positions.length, 0);
    assert.deepEqual(JSON.parse((await run(['status', '--config', config, '--db', db])).stdout), first);
    const exported = path.join(dir, 'events.jsonl');
    await run(['export-events', '--config', config, '--db', db, '--out', exported]);
    assert.equal(await readFile(exported, 'utf8'), await readFile(events, 'utf8'));
    const paused = JSON.parse((await run(['control', '--config', config, '--db', db, '--action', 'pause'])).stdout);
    assert.equal(paused.paused, true);
    await assert.rejects(run(['demo', '--out', demo])); // Never overwrite a prior experiment.
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('an unconfigured template fails before requesting credentials or connecting to Toss', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 's-trader-config-'));
  try {
    const template = fileURLToPath(new URL('./config.template.json', import.meta.url));
    await assert.rejects(run(['toss', '--config', template, '--db', path.join(dir, 'paper.sqlite')]), error => error.stderr.includes('initialCashKrw'));
  } finally { await rm(dir, { recursive: true, force: true }); }
});
