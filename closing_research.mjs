import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const buckets = { supported: '산업·기업 근거 지지', conditional: '조건부 관찰', hold: '검증 보류', excluded: '뉴스·위험 기준 제외' };
const clean = s => String(s).replace(/[|\r\n]/g, ' ');
const price = n => n == null ? '미확인' : Number(n).toLocaleString('ko-KR', { maximumFractionDigits: 1 });
const date = s => /^\d{4}-\d{2}-\d{2}$/.test(s || '') && Number.isFinite(Date.parse(s)) && new Date(s).toISOString().slice(0, 10) === s;

// Research is evidence-backed agent judgment, not automated news sentiment or order execution.
export function combineResearch(scan, research) {
  assert(scan.rules?.version === 'lecture-v3-news-all' && scan.errors?.length === 0, 'Complete v3 scan required');
  assert(research.ruleVersion === scan.rules.version && research.scanGeneratedAt === scan.generatedAt && research.asOf === scan.asOf, 'Research/scan identity mismatch');
  assert(date(research.informationAsOf) && research.informationAsOf >= scan.asOf, 'Invalid information cutoff');
  assert(research.informationAsOf <= new Date(Date.now() + 9 * 3600000).toISOString().slice(0, 10), 'Future research cutoff');
  const sources = new Map();
  for (const s of research.sources || []) {
    const url = new URL(s.url);
    assert(s.id && !sources.has(s.id) && url.protocol === 'https:' && !url.username && !url.password, 'Invalid/duplicate source');
    assert(s.title && s.publisher && typeof s.primary === 'boolean' && ['industry', 'company', 'news', 'risk', 'price'].includes(s.kind), 'Source metadata missing');
    assert(date(s.publishedDate) && s.publishedDate <= research.informationAsOf, 'Future/undated source');
    sources.set(s.id, s);
  }
  const candidateCodes = new Set(scan.candidates.map(s => s.code));
  assert(candidateCodes.size === scan.candidates.length && scan.selected.length === candidateCodes.size && new Set(scan.selected.map(s => s.code)).size === candidateCodes.size && scan.selected.every(s => candidateCodes.has(s.code)), 'Scan omitted qualified candidates');
  const reviews = new Map();
  for (const r of research.reviews || []) {
    assert(candidateCodes.has(r.code) && !reviews.has(r.code), 'Unknown/duplicate researched stock');
    assert(Object.hasOwn(buckets, r.bucket) && ['positive', 'mixed', 'negative', 'unknown'].includes(r.industryDirection), 'Invalid classification');
    assert(['direct', 'indirect', 'unknown'].includes(r.companyExposure) && ['complete', 'partial'].includes(r.riskCoverage), 'Missing exposure/risk scope');
    assert(typeof r.priceConflict === 'boolean' && typeof r.materialRiskUnresolved === 'boolean', 'Risk flags required');
    assert(r.assessment && r.catalyst && r.nextCheck && Array.isArray(r.risks) && r.risks.length && Array.isArray(r.sourceIds), 'Missing research rationale');
    assert(r.sourceIds.every(id => sources.has(id)), 'Unresolved evidence reference');
    if (r.bucket === 'supported') {
      assert(r.industryDirection === 'positive' && r.companyExposure === 'direct' && !r.priceConflict && !r.materialRiskUnresolved, 'Unsupported positive classification');
      const evidence = r.sourceIds.map(id => sources.get(id));
      assert(evidence.some(s => s.primary && s.kind === 'company'), 'Primary company exposure evidence required');
      assert(evidence.some(s => s.primary && s.kind === 'industry' && Date.parse(research.informationAsOf) - Date.parse(s.publishedDate) <= 90 * 86400000), 'Recent primary industry evidence required');
    }
    if (r.priceConflict || r.materialRiskUnresolved) assert(['hold', 'excluded'].includes(r.bucket), 'Unresolved material risk must remain on hold');
    reviews.set(r.code, r);
  }
  assert(reviews.size === candidateCodes.size, 'Every chart-qualified stock must be researched or explicitly held');
  const rows = scan.selected.map(s => ({ ...s, research: reviews.get(s.code) }))
    .sort((a, b) => Object.keys(buckets).indexOf(a.research.bucket) - Object.keys(buckets).indexOf(b.research.bucket) || a.priority - b.priority || b.turnover - a.turnover);
  return { asOf: scan.asOf, informationAsOf: research.informationAsOf, scanGeneratedAt: scan.generatedAt,
    ruleVersion: scan.rules.version, replay: !!scan.replay, selectionStage: 'news-reviewed-observation',
    brokerSync: 'not_connected', generatedAt: new Date().toISOString(), stockCount: scan.stockCount, themeCount: scan.themes.length,
    confirmedBuySignals: [], confirmationLimit: '강의의 미확정 정의·정규장 시세·최신 공시 전체 대조 전. 근거 지지와 매수 신호는 다름.',
    sources: research.sources, rows,
    counts: Object.fromEntries(Object.keys(buckets).map(k => [k, rows.filter(s => s.research.bucket === k).length])) };
}

export function researchMarkdown(r) {
  const lines = [`# ${r.asOf} 가격 기준 — 뉴스·산업을 반영한 전체 관찰 목록`, '',
    `정보 확인 기준일 ${r.informationAsOf} · ${r.ruleVersion} · ${r.replay ? '휴장일 사후 재검사, 당시 실시간 추천 아님' : '현재 실행'}`, '',
    `강세 조건 통과 ${r.themeCount}개 테마의 ${r.stockCount}개 종목에서 차트 통과 ${r.rows.length}개 전부 검토. 종목·테마당 개수 한도 없음. 시장 전체 전수검색 아님.`, '',
    `${r.confirmationLimit} 토스 관심목록은 변경하지 않았습니다.`, '',
    '시크릿은 최신 기준봉 몸통 위에서 1/3인 높은 선을 사용한 임시 해석입니다. 가격은 수집 일봉 관측값이며 공식 KRX 정규장 체결 기준으로 단정하지 않습니다.', ''];
  for (const [bucket, label] of Object.entries(buckets)) {
    lines.push(`## ${label} ${r.counts[bucket]}개`, '',
      '| 종목 | 최신 기준봉 | 기준일 관측가 | 시크릿선 | 단계 |', '|---|---|---:|---:|---|');
    for (const s of r.rows.filter(s => s.research.bucket === bucket)) {
      lines.push(`| ${clean(s.name)} ${s.code} | ${s.reference?.date ?? '미정'} | ${price(s.observedPrice)} | ${price(s.reference?.secretUpper)} | ${clean(s.setup)} |`);
    }
    if (!r.counts[bucket]) lines.push('| 없음 | | | | |');
    for (const s of r.rows.filter(s => s.research.bucket === bucket)) {
      lines.push('', `### ${clean(s.name)} ${s.code}`, '',
        `- 산업 방향/기업 연결: ${s.research.industryDirection} / ${s.research.companyExposure}. ${clean(s.research.assessment)}`,
        `- 재료: ${clean(s.research.catalyst)}`,
        `- 다음 확인: ${clean(s.research.nextCheck)}`,
        `- 기준 가격: 포인트 ${price(s.point?.level)}, 20일선 ${price(s.ma20)}, 240일선 ${price(s.ma240)}. 실제 손절가 아님.`,
        `- 위험·한계: ${s.research.risks.map(clean).join(' / ')}. 공시 검토 범위: ${s.research.riskCoverage}.`,
        `- 근거: ${s.research.sourceIds.map(id => `[${id}]`).join(', ')}`, '');
    }
  }
  lines.push('## 출처 — 발행일과 분석일 구분', '');
  for (const s of r.sources) lines.push(`- [${s.id}] ${s.publishedDate} · ${clean(s.publisher)} · ${clean(s.title)} · ${s.primary ? '1차 자료' : '보조 자료'} · ${s.url}`);
  return lines.join('\n') + '\n';
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    assert(process.argv.length === 4, 'Usage: node closing_research.mjs <scan.json> <research.json>');
    const [scanPath, researchPath] = process.argv.slice(2).map(p => path.resolve(p));
    assert(scanPath.endsWith('.json') && scanPath !== researchPath, 'Separate JSON input files required');
    const result = combineResearch(JSON.parse(await fs.readFile(scanPath, 'utf8')), JSON.parse(await fs.readFile(researchPath, 'utf8')));
    const stem = scanPath.slice(0, -5) + '_reviewed';
    await fs.writeFile(stem + '.json', JSON.stringify(result, null, 2), { flag: 'wx' });
    await fs.writeFile(stem + '.md', researchMarkdown(result), { flag: 'wx' });
    console.log(JSON.stringify({ report: stem + '.md', data: stem + '.json', rows: result.rows.length, counts: result.counts }, null, 2));
  } catch (e) { console.error(`NEWS REVIEW FAILED — 미완료 연구를 최종 목록으로 발행하지 않습니다: ${e.message}`); process.exitCode = 1; }
}
