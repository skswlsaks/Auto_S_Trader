import test from 'node:test';
import assert from 'node:assert/strict';
import { candidatesFromScan, validateEvidence, validateResearch, runResearch } from './research.mjs';

const input = () => ({ asOf: '2026-10-29T08:50:00+09:00', candidates: [{ symbol: '005930', scanDate: '2026-10-28', name: 'example' }],
  sources: [{ id: 's1', url: 'https://example.com/filing', title: 'Example filing', text: 'Fixture evidence only', publishedAt: '2026-10-28T15:00:00+09:00', knownAt: '2026-10-28T16:00:00+09:00' }] });
const result = () => ({ marketSummary: 'Fixture review', reviews: [{ symbol: '005930', route: 'individual_strength', reason: 'Independent stock strength, fixture only', sourceIds: ['s1'] }] });

test('existing jongga scan import retains individual stocks outside strong sectors', () => {
  const scan = { ruleVersion: 'jongga-v1', phase: 'close', asOf: '2026-10-28', errors: [], groups: {
    entry: [{ code: '005930', name: 'example', sectorGrade: '중립' }], above_point: [], waiting_breakout: [], ipo: [] } };
  const c = candidatesFromScan(scan);
  assert.equal(c.length, 1); assert.equal(c[0].executablePlan, false); assert.equal(c[0].sectorGrade, '중립');
  assert.throws(() => candidatesFromScan({ ...scan, replay: true }));
  assert.throws(() => candidatesFromScan({ ...scan, errors: ['missing data'] }));
});

test('research evidence rejects future information, credential URLs and missing sources', () => {
  const i = input(); assert.equal(validateEvidence(i), i);
  i.sources[0].knownAt = '2026-10-30T10:00:00+09:00'; assert.throws(() => validateEvidence(i));
  const bad = input(); bad.sources[0].url = 'https://user:password@example.com'; assert.throws(() => validateEvidence(bad));
  assert.throws(() => validateEvidence({ ...input(), sources: [] }));
});

test('research cannot omit candidates, invent evidence or authorize orders', () => {
  const r = validateResearch(input(), result()); assert.equal(r.executablePlan, false); assert.equal(r.validatedStrategy, false);
  assert.throws(() => validateResearch(input(), { ...result(), reviews: [] }));
  const bad = result(); bad.reviews[0].sourceIds = ['invented']; assert.throws(() => validateResearch(input(), bad));
  const unsupported = result(); unsupported.reviews[0].sourceIds = []; assert.throws(() => validateResearch(input(), unsupported));
});

test('Bedrock review uses structured output and a shell-free CLI invocation', async () => {
  const reviewed = await runResearch(input(), { modelId: 'test-model', execute: async (command, args, options) => {
    assert.equal(command, 'aws'); assert.equal(options.shell, undefined);
    const request = JSON.parse(args[args.indexOf('--cli-input-json') + 1]);
    assert.equal(request.modelId, 'test-model'); assert.equal(request.toolConfig.toolChoice.tool.name, 'record_research');
    return { stdout: JSON.stringify({ output: { message: { content: [{ toolUse: { name: 'record_research', input: result() } }] } } }) };
  } });
  assert.equal(reviewed.reviews[0].route, 'individual_strength');
});
