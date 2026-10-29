import assert from 'node:assert/strict';
import { combineResearch, researchMarkdown } from './closing_research.mjs';

const stock = { code: '007660', name: 'fixture', priority: 1, turnover: 1, observedPrice: 1, setup: 'test' };
const scan = { rules: { version: 'lecture-v3-news-all' }, errors: [], generatedAt: '2026-10-11T03:00:00Z', asOf: '2026-10-08', selected: [stock], candidates: [stock], themes: [], stockCount: 1 };
const research = { ruleVersion: scan.rules.version, scanGeneratedAt: scan.generatedAt, asOf: scan.asOf, informationAsOf: '2026-10-11', sources: [
  { id: 'industry', kind: 'industry', primary: true, publishedDate: '2026-10-01', publisher: 'fixture', title: 'industry', url: 'https://example.com/industry' },
  { id: 'company', kind: 'company', primary: true, publishedDate: '2026-05-01', publisher: 'fixture', title: 'company', url: 'https://example.com/company' }
], reviews: [{ code: stock.code, bucket: 'supported', industryDirection: 'positive', companyExposure: 'direct', riskCoverage: 'partial',
  priceConflict: false, materialRiskUnresolved: false, assessment: 'test', catalyst: 'industry, not a company order', nextCheck: 'test', risks: ['unverified'], sourceIds: ['industry', 'company'] }] };
assert.equal(combineResearch(scan, research).counts.supported, 1);
assert.equal(combineResearch(scan, research).confirmedBuySignals.length, 0);
assert(researchMarkdown(combineResearch(scan, research)).includes('fixture 007660'));
const broken = mutate => { const r = structuredClone(research); mutate(r); assert.throws(() => combineResearch(scan, r)); };
broken(r => r.reviews.pop());
broken(r => r.reviews.push(r.reviews[0]));
broken(r => r.scanGeneratedAt = 'old');
broken(r => r.reviews[0].sourceIds.push('missing'));
broken(r => r.sources[0].publishedDate = '2026-10-12');
broken(r => r.sources[0].publishedDate = '2026-02-30');
broken(r => r.informationAsOf = '2099-01-01');
broken(r => r.sources[0].publishedDate = '2025-01-01');
broken(r => r.reviews[0].industryDirection = 'negative');
broken(r => r.reviews[0].companyExposure = 'unknown');
broken(r => r.reviews[0].priceConflict = true);
broken(r => r.reviews[0].materialRiskUnresolved = true);
broken(r => r.sources[1].primary = false);
broken(r => r.sources[0].url = 'javascript:alert(1)');
const held = structuredClone(research); held.reviews[0].bucket = 'hold'; held.reviews[0].priceConflict = true;
assert.equal(combineResearch(scan, held).counts.hold, 1);
const many = Array.from({ length: 12 }, (_, i) => ({ ...stock, code: String(i).padStart(6, '0') }));
const manyResearch = { ...research, reviews: many.map(s => ({ ...research.reviews[0], code: s.code })) };
assert.equal(combineResearch({ ...scan, candidates: many, selected: many }, manyResearch).rows.length, 12);
assert.throws(() => combineResearch({ ...scan, errors: ['partial'] }, research));
assert.throws(() => combineResearch({ ...scan, candidates: many }, research));
assert.throws(() => combineResearch({ ...scan, candidates: many, selected: many.map(() => many[0]) }, manyResearch));
console.log('news research checks passed');
