import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { createReadStream } from 'node:fs';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { PaperStore } from './store.mjs';
import { validateConfig } from './engine.mjs';
import { demoConfig, demoEvents } from './demo.mjs';
import { runTossFeed } from './toss.mjs';
import { candidatesFromScan, runResearch } from './research.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const json = async p => JSON.parse(await fs.readFile(path.resolve(p), 'utf8'));
const stringify = x => JSON.stringify(x, null, 2) + '\n';
const help = `s_trader paper v0 (Node 24.14+; NO live-order implementation)
  demo [--out DIR]                         synthetic reproducible scenario; creates a new directory
  replay --config FILE --events FILE --db FILE [--out DIR]
  toss --config FILE --db FILE             read-only Toss feed + local paper fills (credentials via env)
  status --config FILE --db FILE [--out DIR]
  export-events --config FILE --db FILE --out FILE
  control --config FILE --db FILE --action pause|resume|flatten
  import-scan --input FILE --out FILE       import existing jongga-v1 JSON as non-executable candidates
  research --input FILE --out FILE          supplied-evidence review via Bedrock / AWS CLI
  help
All numeric demo parameters are unvalidated hypotheses. Real prices are not real orders.`;

export async function exportRun(store, dir) {
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'summary.json'), stringify(store.summary()));
  await fs.writeFile(path.join(dir, 'ledger.jsonl'), store.actions().map(x => JSON.stringify(x)).join('\n') + '\n');
}

export async function main(argv = process.argv.slice(2)) {
  const [command = 'help', ...args] = argv, options = {};
  assert(args.length % 2 === 0, 'Options must be --name value pairs');
  for (let i = 0; i < args.length; i += 2) {
    assert(/^--[a-z]+$/.test(args[i]) && !Object.hasOwn(options, args[i].slice(2)), 'Invalid/duplicate option');
    options[args[i].slice(2)] = args[i + 1];
  }
  const required = key => { assert(options[key], `--${key} is required`); return options[key]; };
  if (command === 'help') { console.log(help); return; }
  if (command === 'import-scan') {
    const input = await json(required('input')), candidates = candidatesFromScan(input);
    await fs.writeFile(required('out'), stringify({ asOf: input.asOf, executablePlan: false, candidates }), { flag: 'wx' });
    console.log(`Imported ${candidates.length} observation candidates; explicit plans are still required.`); return;
  }
  if (command === 'research') {
    const result = await runResearch(await json(required('input')), { modelId: process.env.BEDROCK_MODEL_ID, region: process.env.AWS_REGION ?? 'ap-northeast-2' });
    await fs.writeFile(required('out'), stringify(result), { flag: 'wx' }); console.log(`Research written to ${options.out}`); return;
  }
  if (command === 'demo') {
    const dir = path.resolve(options.out ?? path.join(root, 'outputs', 'paper', `demo-${new Date().toISOString().replace(/[:.]/g, '-')}-${process.pid}`));
    await fs.mkdir(path.dirname(dir), { recursive: true });
    await fs.mkdir(dir, { recursive: false });
    const config = demoConfig(), events = demoEvents();
    await fs.writeFile(path.join(dir, 'config.json'), stringify(config));
    await fs.writeFile(path.join(dir, 'events.jsonl'), events.map(e => JSON.stringify(e)).join('\n') + '\n');
    const store = new PaperStore(path.join(dir, 'paper.sqlite'), config);
    try { for (const event of events) store.process(event); await exportRun(store, dir); console.log(stringify({ output: dir, ...store.summary() })); }
    finally { store.close(); }
    return;
  }
  assert(['replay', 'toss', 'status', 'control', 'export-events'].includes(command), 'Unknown command; use help');
  const config = validateConfig(await json(required('config')));
  const store = new PaperStore(required('db'), config);
  try {
    if (command === 'export-events') {
      const file = await fs.open(required('out'), 'wx');
      try { for (const payload of store.eventRows()) await file.write(payload + '\n'); }
      finally { await file.close(); }
      console.log(`Exported normalized events to ${options.out}`); return;
    } else if (command === 'replay') {
      const lines = createInterface({ input: createReadStream(required('events')), crlfDelay: Infinity });
      for await (const line of lines) if (line.trim()) store.process(JSON.parse(line));
    } else if (command === 'control') {
      store.process({ id: randomUUID(), type: 'control', at: new Date().toISOString(), action: required('action') });
    } else if (command === 'toss') {
      const controller = new AbortController();
      const stop = () => controller.abort();
      process.once('SIGINT', stop); process.once('SIGTERM', stop);
      console.log('Toss market-data connection only. All fills are local paper simulations.');
      try {
        await runTossFeed({ symbols: config.plans.map(p => p.symbol), signal: controller.signal,
          onEvent: event => { for (const action of store.process(event)) console.log(JSON.stringify(action)); },
          onStatus: (ready, reason) => {
            store.process({ id: randomUUID(), type: 'feed', at: new Date().toISOString(), ready });
            console.log(JSON.stringify({ type: 'FEED', ready, ...(reason ? { reason } : {}) }));
          } });
      } finally {
        process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop);
        store.process({ id: randomUUID(), type: 'feed', at: new Date().toISOString(), ready: false });
      }
    }
    if (options.out) await exportRun(store, path.resolve(options.out));
    console.log(stringify(store.summary()));
  } finally { store.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(`PAPER RUN FAILED: ${error.message}`); process.exitCode = 1; });
}
