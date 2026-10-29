import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { marketDates, parseBars, explainCandidate, prepareLists } from './nightly_scan.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const directory = path.join(root, 'outputs', 'tracking');
const ledgerPath = path.join(directory, 'ledger.json');
const kstDay = value => new Date(new Date(value).getTime() + 9 * 3600000).toISOString().slice(0, 10);
const validPrice = b => b && b.open > 0 && b.low > 0 && b.high >= Math.max(b.open, b.close) && b.low <= Math.min(b.open, b.close) && b.close > 0 && b.volume > 0;

export function recordRecommendations(ledger, report, sourceFile) {
  assert(report.asOf < report.next && kstDay(report.generatedAt) < report.next, 'Recommendation must precede its target session');
  assert(Array.isArray(report.selected), 'Missing selections');
  for (const s of report.selected) {
    assert(/^[0-9A-Z]{6}$/.test(s.code) && s.close > 0 && Number.isFinite(s.close), 'Invalid recommendation');
    const id = `${report.next}/${s.code}${report.ruleVersion >= 3 ? `/v${report.ruleVersion}` : ''}`;
    if (ledger.recommendations.some(r => r.id === id)) continue;
    ledger.recommendations.push({ id, recommendedAt: report.generatedAt, targetDate: report.next,
      referenceDate: report.asOf, referenceClose: s.close, code: s.code, name: s.name,
      tier: s.tier || s.setup[0], setup: s.setup, themes: s.themes, ruleVersion: report.ruleVersion,
      high52: s.high52, recentHigh: s.recentHigh ?? null, highBasis: s.highBasis || '52week', sourceFile: path.basename(sourceFile), sourceUrl: s.url,
      explanation: explainCandidate(s, report),
      review: '시세 조건만 확인·최신 공시/Threads 별도 확인', observations: [], metrics: null });
  }
}

export function measure(record, sessions, asOf, error = null) {
  const expected = sessions.filter(d => d >= record.targetDate && d <= asOf);
  if (error) return { status: error, sessionCount: expected.length };
  if (!expected.length) return { status: '첫 거래일 대기', sessionCount: 0 };
  const byDate = new Map(record.observations.map(b => [b.date, b]));
  const bars = expected.map(d => byDate.get(d));
  if (error || expected[0] !== record.targetDate || bars.some(b => !validPrice(b)))
    return { status: error || '시세 누락/거래정지 확인 필요', sessionCount: expected.length };
  const entry = bars[0].open, last = bars.at(-1);
  const holdingReturns = Object.fromEntries([1, 3, 5, 10].map(n => [n, bars.length >= n ? bars[n - 1].close / entry - 1 : null]));
  return { status: '가격 추적 중', sessionCount: bars.length, entryDate: expected[0], entryOpen: entry,
    latestDate: last.date, latestClose: last.close, referenceReturn: last.close / record.referenceClose - 1,
    openReturn: last.close / entry - 1, holdingReturns,
    maximumReturn: Math.max(...bars.map(b => b.high)) / entry - 1,
    minimumReturn: Math.min(...bars.map(b => b.low)) / entry - 1,
    hit5: bars.some(b => b.high >= entry * 1.05), hit10: bars.some(b => b.high >= entry * 1.10) };
}

export function mergeObservations(record, bars, asOf, fetchedAt, sourceUrl) {
  const baseline = bars.find(b => b.date === record.referenceDate);
  if (!baseline || Math.abs(baseline.close / record.referenceClose - 1) > 1e-8)
    return '추천 기준가격 수정/누락·기업행사 확인 필요';
  for (const b of bars.filter(b => b.date >= record.targetDate && b.date <= asOf)) {
    const old = record.observations.find(x => x.date === b.date);
    if (old && ['open', 'high', 'low', 'close', 'volume'].some(k => old[k] !== b[k]))
      return '과거 관찰가격 수정 발견·원본 보존';
  }
  const existing = new Set(record.observations.map(b => b.date));
  record.observations.push(...bars.filter(b => b.date >= record.targetDate && b.date <= asOf && !existing.has(b.date)).map(b => ({ ...b, fetchedAt, sourceUrl })));
  record.observations.sort((a, b) => a.date.localeCompare(b.date));
  return null;
}

export async function updateTracking(reportFiles = []) {
  await fs.mkdir(directory, { recursive: true });
  // Exclusive file prevents concurrent writers from losing recommendation history.
  const lockPath = path.join(directory, 'update.lock');
  const lock = await fs.open(lockPath, 'wx');
  try {
    let ledger;
    try { ledger = JSON.parse(await fs.readFile(ledgerPath, 'utf8')); }
    catch (e) { if (e.code !== 'ENOENT') throw e; ledger = { version: 1, recommendations: [], googleSheets: { status: '관리자 정책으로 연결 불가', url: null } }; }
    assert(ledger.version === 1 && Array.isArray(ledger.recommendations), 'Invalid ledger; refusing to overwrite');
    const now = new Date(), evidence = [], errors = [];
    async function get(url) {
      const response = await fetch(url, { signal: AbortSignal.timeout(20000), headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://stock.naver.com/' } });
      assert(response.ok, `HTTP ${response.status}`);
      const text = await response.text(); evidence.push({ url, fetchedAt: new Date().toISOString(), raw: text }); return text;
    }
    const dates = marketDates(JSON.parse(await get('https://stock.naver.com/api/stockSecurity/exchanges/market-status?exchanges=krx')), now);
    for (const file of reportFiles) {
      const r = JSON.parse(await fs.readFile(file, 'utf8'));
      assert(new Date(r.generatedAt) <= now, 'Future recommendation report');
      recordRecommendations(ledger, r, file);
      if (!ledger.currentList || r.generatedAt >= ledger.currentList.generatedAt) ledger.currentList = prepareLists(r);
    }
    // Backfill explanations only from each original signal snapshot; never re-label history from today's list.
    for (const record of ledger.recommendations.filter(r => !r.explanation)) {
      const original = JSON.parse(await fs.readFile(path.join(root, 'outputs/nightly', path.basename(record.sourceFile)), 'utf8'));
      const signal = original.selected.find(s => s.code === record.code);
      assert(signal?.close === record.referenceClose && original.generatedAt === record.recommendedAt, 'Original recommendation mismatch');
      record.explanation = { ...explainCandidate(signal, original), documentedAt: now.toISOString() };
    }
    ledger.recommendations.sort((a, b) => a.recommendedAt.localeCompare(b.recommendedAt) || a.id.localeCompare(b.id));
    const firstDate = ledger.recommendations.map(r => r.referenceDate).sort()[0] || dates.asOf;
    const dailyUrl = code => `https://api.finance.naver.com/siseJson.naver?symbol=${code}&requestType=1&startTime=${firstDate.replaceAll('-', '')}&endTime=${dates.asOf.replaceAll('-', '')}&timeframe=day`;
    const benchmark = parseBars(await get(dailyUrl('005930')));
    assert(benchmark.at(-1).date === dates.asOf && benchmark[0].date === firstDate, 'Incomplete market-session calendar');
    const sessions = benchmark.map(b => b.date), codes = [...new Set(ledger.recommendations.map(r => r.code))];
    for (let i = 0; i < codes.length; i += 4) await Promise.all(codes.slice(i, i + 4).map(async code => {
      const records = ledger.recommendations.filter(r => r.code === code);
      try {
        const sourceUrl = dailyUrl(code), bars = parseBars(await get(sourceUrl));
        for (const record of records) {
          const error = mergeObservations(record, bars, dates.asOf, now.toISOString(), sourceUrl);
          record.metrics = measure(record, sessions, dates.asOf, error);
          if (error || /확인 필요/.test(record.metrics.status)) errors.push(`${record.id}: ${record.metrics.status}`);
        }
      } catch (e) {
        errors.push(`${code}: ${e.message}`);
        for (const r of records) r.metrics = { status: `수집 실패: ${e.message}` };
      }
    }));
    ledger.updatedAt = now.toISOString(); ledger.priceAsOf = dates.asOf; ledger.nextSession = dates.next; ledger.errors = errors;
    const archive = path.join(directory, `${now.toISOString().replace(/[:.]/g, '-')}.json`);
    await fs.writeFile(archive, JSON.stringify({ ...ledger, evidence }, null, 2), { flag: 'wx' });
    const temporary = path.join(directory, `ledger-${process.pid}.tmp`);
    await fs.writeFile(temporary, JSON.stringify(ledger, null, 2), { flag: 'wx' });
    await fs.rename(temporary, ledgerPath);
    console.log(JSON.stringify({ ledger: ledgerPath, archive, priceAsOf: dates.asOf, recommendations: ledger.recommendations.length,
      observations: ledger.recommendations.reduce((n, r) => n + r.observations.length, 0), errors, googleSheets: ledger.googleSheets }, null, 2));
    return ledger;
  } finally { await lock.close(); await fs.unlink(lockPath); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) updateTracking(process.argv.slice(2)).catch(e => { console.error(`TRACKER FAILED: ${e.message}`); process.exitCode = 1; });
