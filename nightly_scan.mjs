import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Research thresholds, not optimized or proven trading rules.
export const rules = Object.freeze({ recentDays: 20, themeGain: 2, breadth: 0.6, leaders: 2,
  leaderGain: 7, maxThemes: 10, turnover: 10e9, highDistance: 0.05, watchDistance: 0.15, minPicks: 3, maxPicks: 5,
  surgeGain: 0.10, surgeVolume: 2, restMin: 2, restMax: 10,
  restVolume: 0.7, maxBaseRange: 0.10, maxPullback: 0.10, maxDayGain: 0.15 });
const root = path.dirname(fileURLToPath(import.meta.url));
const origin = 'https://stock.naver.com';
const day = d => new Date(new Date(d).getTime() + 9 * 3600000).toISOString().slice(0, 10);
const ago = (d, n) => new Date(Date.parse(`${d}T00:00:00Z`) - n * 86400000).toISOString().slice(0, 10);
const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
const median = a => { const b = [...a].sort((x, y) => x - y); return (b[Math.floor((b.length - 1) / 2)] + b[Math.floor(b.length / 2)]) / 2; };
const num = value => { assert(value !== null && value !== undefined && String(value).trim() !== '', 'Missing number'); const n = Number(value); assert(Number.isFinite(n), `Invalid number: ${value}`); return n; };
const pct = n => `${(n * 100).toFixed(2)}%`;
const won = n => Math.round(n).toLocaleString('ko-KR');
const clean = s => String(s).replace(/[|\r\n]/g, ' ');
const position = distance => `${pct(Math.abs(distance))} ${distance < 0 ? '위' : '아래'}`;

export function parseBars(text) {
  const bars = [...text.matchAll(/\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)/g)].map(m => ({
    date: `${m[1].slice(0, 4)}-${m[1].slice(4, 6)}-${m[1].slice(6)}`,
    open: +m[2], high: +m[3], low: +m[4], close: +m[5], volume: +m[6],
  }));
  assert(bars.length, 'No daily bars');
  for (const [i, b] of bars.entries()) {
    assert(!i || b.date > bars[i - 1].date, 'Duplicate or unordered dates');
    assert(b.volume >= 0 && Object.values(b).slice(1).every(Number.isFinite), 'Invalid OHLCV');
    // Zero OHLC is retained so suspended sessions cannot silently count as a normal base.
    if (b.open > 0) assert(b.low > 0 && b.high >= Math.max(b.open, b.close) && b.low <= Math.min(b.open, b.close), 'Invalid price range');
  }
  return bars;
}

export function marketDates(payload, now = new Date()) {
  const statuses = payload.exchanges?.find(x => x.exchange === 'krx')?.statuses?.filter(x => x.stockType === 'stock' && ['KOSPI', 'KOSDAQ'].includes(x.marketType));
  assert(statuses?.length === 2, 'KRX market calendar unavailable');
  assert(day(payload.serverTime) === day(now), 'Stale market calendar');
  const asOf = statuses[0].latest?.tradeBaseAt, next = statuses[0].next?.tradeBaseAt;
  assert(/^\d{4}-\d{2}-\d{2}$/.test(asOf) && /^\d{4}-\d{2}-\d{2}$/.test(next), 'Missing trading dates');
  assert(statuses.every(x => x.latest.tradeBaseAt === asOf && x.next.tradeBaseAt === next && x.latest.session.marketStatusDetailType === 'close'), 'Markets not closed or dates disagree');
  assert(asOf <= day(now) && next > day(now), 'Not a completed nightly session');
  return { asOf, next, runDate: day(now) };
}

export function safeStock(s) {
  return /^[0-9A-Z]{6}$/.test(s.itemcode) && ['0', '1'].includes(s.sosok)
    && s.tradeStopYn === 'N' && s.manageStatusGb === '0' && s.tradableStatus === 'tradable'
    && s.marketAlertType === '00' && !s.etfType && !/스팩|SPAC/i.test(s.itemname);
}

export function analyze(s, bars, asOf) {
  const last = bars.at(-1), cutoff = ago(asOf, 364);
  if (last?.date !== asOf) return { reason: '일봉 기준일 불일치' };
  if (bars.length < rules.recentDays + 1) return { reason: '최근 20거래일 고가 계산 이력 부족' };
  if (bars.slice(-rules.recentDays - 1).some(b => b.open <= 0 || b.close <= 0 || b.volume <= 0)) return { reason: '최근 거래정지/무거래/가격 누락' };
  if (num(s.tradeAmount) < rules.turnover) return { reason: '거래대금 100억원 미만' };
  const year = bars.filter(b => b.date >= cutoff);
  const high52 = bars[0].date <= cutoff && year.length >= 200 ? Math.max(...year.map(b => b.high)) : null;
  const prior = bars.slice(-rules.recentDays - 1, -1);
  const high20 = Math.max(...prior.map(b => b.high));
  const highDate = prior.findLast(b => b.high === high20).date;
  const distance = 1 - last.close / high20, change = last.close / bars.at(-2).close - 1;
  const ma5 = mean(bars.slice(-5).map(b => b.close)), ma20 = mean(bars.slice(-20).map(b => b.close));
  if (last.close * 100 < high20 * (100 - rules.watchDistance * 100)) return { reason: '최근 20거래일 고가보다 15% 넘게 아래' };
  if (last.close * 100 > high20 * (100 + rules.highDistance * 100)) return { reason: '최근 고가를 5% 넘게 돌파: 추격 제외' };
  if (last.close < ma5 || ma5 < ma20) return { reason: '종가 ≥ 5일선 ≥ 20일선 미충족' };
  if (change >= rules.maxDayGain) return { reason: '당일 15% 이상 급등: 추격 제외' };
  // The 52-week high is context only; it must never select or exclude a recent-high setup.
  const dataWarnings = high52 && Number(s.week52HighPrice) > 0 && Math.abs(Number(s.week52HighPrice) / high52 - 1) > 0.03
    ? ['참고용 52주 고가의 공급자 기준 차이: 수정주가/기업행사 확인 필요'] : [];
  let surge = null;
  for (let i = bars.length - 1 - rules.restMin; i >= Math.max(20, bars.length - 1 - rules.restMax); i--) {
    const b = bars[i], gain = b.close / bars[i - 1].close - 1;
    const volumeRatio = b.volume / mean(bars.slice(i - 20, i).map(x => x.volume));
    const rest = bars.slice(i + 1), restVolume = mean(rest.slice(-3).map(x => x.volume)) / b.volume;
    const baseRange = Math.max(...rest.slice(-3).map(x => x.high)) / Math.min(...rest.slice(-3).map(x => x.low)) - 1;
    const peak = Math.max(...bars.slice(i).map(x => x.high));
    if (gain >= rules.surgeGain && volumeRatio >= rules.surgeVolume && restVolume <= rules.restVolume && baseRange <= rules.maxBaseRange && 1 - last.close / peak <= rules.maxPullback) {
      surge = { date: b.date, gain, volumeRatio, restDays: rest.length, restVolume, baseRange }; break;
    }
  }
  const tier = last.close * 100 < high20 * (100 - rules.highDistance * 100) ? 'C' : surge && distance >= 0 ? 'A' : 'B';
  return { code: s.itemcode, name: s.itemname, tier,
    setup: tier === 'C' ? 'C 확장 관찰·최근 고가 5~15% 아래' : tier === 'A' ? 'A 급등 후 휴식·최근 고가 재도전' : distance < 0 ? 'B 최근 고가 돌파 후 안착 관찰' : 'B 최근 고가 근접·휴식 미확인',
    asOf, close: last.close, change, high52, high20, recentHigh: high20, highDate, highBasis: 'recent20', distance, dataWarnings,
    highState: last.close > high20 ? '최근 20일 고가 종가 돌파' : last.high > high20 ? '장중 최근 고가 돌파·종가 미안착' : '최근 20일 고가 미돌파',
    baseLow: Math.min(...bars.slice(-3).map(b => b.low)), ma5, ma20,
    turnover: num(s.tradeAmount), surge, themeReason: s.itemInfo || '분류 근거 확인 필요',
    url: `${origin}/domestic/stock/${s.itemcode}/price` };
}

export async function collectPages(fetchPage, key, stopAfterPage) {
  const rows = [], seen = new Set();
  for (let page = 0; page < 30; page++) {
    const batch = await fetchPage(page);
    assert(Array.isArray(batch), 'Unexpected list format');
    for (const row of batch) { assert(row[key] && !seen.has(row[key]), 'Missing ID or repeated pagination'); seen.add(row[key]); rows.push(row); }
    if (batch.length < 100 || stopAfterPage?.(batch)) return rows;
  }
  throw new Error('Pagination limit reached; incomplete universe');
}

export function belowThemeCutoff(page) {
  assert(page.length && page.every((t, i) => Number.isFinite(num(t.changeRate)) && (!i || num(page[i - 1].changeRate) >= num(t.changeRate))), 'Theme ranking is not descending');
  return num(page.at(-1).changeRate) < rules.themeGain;
}

export function shortlist(rows) {
  const counts = new Map(), selected = [];
  const sorted = [...rows].sort((a, b) => a.tier.localeCompare(b.tier) || Math.abs(a.distance) - Math.abs(b.distance) || b.turnover - a.turnover);
  for (const row of sorted) {
    if (row.tier === 'C' && selected.length >= rules.minPicks) break;
    // Count every overlapping theme, not just the primary label.
    if (row.themes.some(t => (counts.get(t) || 0) >= 2)) continue;
    selected.push(row); for (const t of row.themes) counts.set(t, (counts.get(t) || 0) + 1);
    if (selected.length === rules.maxPicks) break;
  }
  return selected;
}

export function explainCandidate(s, r) {
  const percent = n => Number.isFinite(n) ? pct(n) : '미확인';
  const price = n => Number.isFinite(n) ? `${won(n)}원` : '미확인';
  const tier = s.tier || s.setup?.[0];
  const recent = s.highBasis === 'recent20' || r.ruleVersion >= 3;
  const highLabel = recent ? '최근 20거래일 고가' : '52주 고가';
  const targetHigh = recent ? s.recentHigh ?? s.high20 : s.high52;
  const distance = s.distance ?? (targetHigh > 0 ? 1 - s.close / targetHigh : null);
  const themes = (s.themes || []).map(name => {
    const t = r.themes?.find(t => t.name === name);
    return t && Number.isFinite(t.changeRate) ? `${name} ${t.changeRate > 0 ? '+' : ''}${t.changeRate.toFixed(2)}%` : name;
  }).join(', ');
  let reason = `${themes || '테마 미확인'}. 종가는 ${highLabel} 대비 ${Number.isFinite(distance) ? position(distance) : '미확인'}.`;
  if (Number.isFinite(s.turnover)) reason += ` 거래대금 ${(s.turnover / 1e8).toFixed(0)}억원.`;
  if (s.surge) reason += ` ${s.surge.date} ${percent(s.surge.gain)} 급등 후 ${s.surge.restDays}거래일, 최근 거래량은 급등일의 ${percent(s.surge.restVolume)}.`;
  let recentRange = null;
  const raw = r.evidence?.find(e => e.url.includes(`symbol=${s.code}&`));
  if (raw) {
    const bars = parseBars(raw.raw).filter(b => b.date <= r.asOf).slice(-3);
    if (bars.length === 3 && bars.every(b => b.low > 0)) recentRange = Math.max(...bars.map(b => b.high)) / Math.min(...bars.map(b => b.low)) - 1;
  }
  const caution = tier === 'C' ? '고가와 거리가 5% 초과. A/B가 3개 미만일 때만 보충 가능한 확장 관찰.'
    : tier === 'B' ? (recent && distance < 0 ? '이미 최근 고가를 종가로 돌파함. 재도전 대기형과 구분하고 안착 여부 확인.' : recentRange > rules.maxBaseRange ? `최근 3일 가격폭 ${percent(recentRange)}로 휴식 기준 10% 초과.` : '급등 후 거래량 축소·좁은 휴식 조건을 모두 확인하지 못함.')
    : `${s.surge?.restDays ?? '미확인'}거래일 휴식 표본. 재돌파 성공 여부는 미확인.`;
  return { reason, caution,
    watchFor: `${highLabel} ${price(targetHigh)} 재도전·안착 여부. 09:15 이후 첫 15분 양봉·시가 유지·돌파 거래량 확인.`,
    invalidate: `최근 3일 저점 ${price(s.baseLow)} 이탈 시 구조 재검토. 실제 손절가는 아님.`,
    sourceAt: r.generatedAt, sourceDate: r.asOf };
}

export function prepareLists(r) {
  const selectedCodes = new Set(r.selected.map(s => s.code));
  const counts = new Map();
  for (const s of r.selected) for (const t of s.themes || []) counts.set(t, (counts.get(t) || 0) + 1);
  const selected = r.selected.map(s => ({ ...s, explanation: explainCandidate(s, r) }));
  const watchlist = [...(r.candidates || [])].filter(s => !selectedCodes.has(s.code))
    .sort((a, b) => (a.tier || a.setup[0]).localeCompare(b.tier || b.setup[0]) || Math.abs(a.distance) - Math.abs(b.distance) || b.turnover - a.turnover)
    .slice(0, 5).map(s => ({ ...s, explanation: explainCandidate(s, r),
      notSelected: (s.themes || []).some(t => (counts.get(t) || 0) >= 2) ? '동일 테마 선정 한도(2개)로 제외' : '오늘 선정 수·우선순위에 따라 대기' }));
  return { generatedAt: r.generatedAt, asOf: r.asOf, next: r.next, selected, watchlist };
}

export function report(r) {
  const lists = prepareLists(r);
  const lines = [`# ${r.next} 거래일 준비 — 야간 관찰 목록`, '',
    `생성: ${r.generatedAt} · 실제 가격 기준일: ${r.asOf} · 규칙 v${r.ruleVersion} (검증 전 가설)`, '',
    `상태: ${r.status} · 조회 테마 ${r.themeCount}개 / 상세 확인 ${r.themes.length}개 / 종목 ${r.stockCount}개 / 일봉 검증 ${r.analyzedCount}개`, '',
    '자동매매가 아닙니다. v3 기준은 기준일 이전 20거래일 고가 재도전입니다. A는 급등 후 휴식·고가 아래 대기, B는 휴식 미확인 또는 소폭 돌파 후 안착 관찰, C는 최근 고가보다 5~15% 아래의 확장 관찰입니다. A/B가 3개 미만일 때만 C로 보충합니다. 52주 고가는 참고이며 필터가 아닙니다.', '',
    '## 강한 테마', '', '| 테마 | 제공 등락률 | 상승 비율 | 구성종목 중간 등락률 | +7% 이상 종목 | 판정 |', '|---|---:|---:|---:|---:|---|'];
  for (const t of r.themes) lines.push(`| ${clean(t.name)} | ${t.changeRate}% | ${pct(t.breadth)} | ${t.medianChange?.toFixed(2) ?? '미확인'}% | ${t.leaders ?? '미확인'} | ${t.strong ? '통과' : clean(t.reason)} |`);
  lines.push('', `## 선정 종목 ${r.selected.length}개`, '', '당일 선별 목록이며 매수 확정 신호는 아닙니다.', '', '| 유형·종목 | 테마 | 기준일 종가 | 최근 20일 고가 / 위치 | 최근 고가일 | 최근 3일 저점 |', '|---|---|---:|---|---|---:|');
  for (const s of r.selected) lines.push(`| ${clean(s.setup)} · ${clean(s.name)} ${s.code} | ${s.themes.map(clean).join(', ')} | ${won(s.close)} | ${won(s.high20)} / ${position(s.distance)} | ${s.highDate || '미확인'} | ${won(s.baseLow)} |`);
  if (!r.selected.length) lines.push('| 조건 충족 종목 없음 — 신규 관찰 목록 비움 | | | | | |');
  for (const s of lists.selected) lines.push('', `### ${clean(s.name)} (${s.code})`, '',
    `- 선정 이유: ${clean(s.explanation.reason)}`,
    `- 부족한 조건·주의: ${clean(s.explanation.caution)}`,
    `- 다음 관찰: ${clean(s.explanation.watchFor)}`,
    `- 구조 재검토: ${clean(s.explanation.invalidate)}`,
    `- ${s.highState}. 참고용 52주 고가: ${s.high52 ? `${won(s.high52)}원` : '전체 이력 부족·미확인'} (선별에 사용하지 않음).`,
    ...((s.dataWarnings || []).map(w => `- 자료 주의: ${clean(w)}`)),
    `- ${s.surge ? `선행 급등 ${s.surge.date}: ${pct(s.surge.gain)}, 이전 20일 평균 대비 거래량 ${s.surge.volumeRatio.toFixed(2)}배. 이후 ${s.surge.restDays}거래일, 최근 휴식 거래량은 급등일의 ${pct(s.surge.restVolume)}, 최근 휴식 가격폭 ${pct(s.surge.baseRange)}.` : '급등·휴식 조건 미확인. A와 같은 확신으로 취급하지 않음.'}`,
    `- 테마 분류 근거(공급자 설명, 당일 상승 원인은 아님): ${clean(s.themeReason)}`,
    `- 시세 확인: ${s.url}`);
  lines.push('', '## 추가 관찰 종목 — 선정 목록과 별도', '', '아래 종목은 당일 추천 수익률 집계에 포함하지 않습니다. 선정되지 않은 이유와 다음 확인 조건을 함께 남깁니다.', '',
    '| 종목 | 관찰 이유 | 이번 선정에서 빠진 이유 | 다음 확인 |', '|---|---|---|---|');
  for (const s of lists.watchlist) lines.push(`| ${clean(s.name)} ${s.code} | ${clean(s.explanation.reason)} | ${clean(s.notSelected)}. ${clean(s.explanation.caution)} | ${clean(s.explanation.watchFor)} ${clean(s.explanation.invalidate)} |`);
  if (!lists.watchlist.length) lines.push('| 추가 관찰 없음 | | | |');
  lines.push('', '## 다음 날 실행 전 확인', '',
    '- 09:15 이후 첫 15분 양봉·시가 유지, 해당 테마 대장 동반 강세, 돌파 거래량을 직접 확인. 밤에 진입 확정으로 표시하지 않음.',
    '- 실제 진입가 E와 첫 15분 저점 L이 확인된 뒤만 계획 수립. 0 < (E-L)/E ≤ 4%일 때만 구조손절 후보로 검토. 초과하면 손절을 억지로 좁히지 않고 제외.',
    '- 야간 기준 최근 3일 저점은 준비용 무효화 기준이며, 다음 날 첫 15분 저점이나 보장된 손절 체결가가 아님. 갭·급변·호가단위·세금·수수료는 별도 확인.',
    '- 첨부에서 논의한 청산 가설: +5%에서 절반 익절, 잔여분 진입가 기준 방어. +10% 도달 전 5분 음봉 장악 또는 5거래일 경과 시 잔여 청산 검토. +10% 이후 고점 대비 5% 추적, 최대 10거래일. 야간 작업은 이 조건을 실시간 감시하지 않음.',
    '- 논의 중인 비중 상한은 종목당 20%·최대 5개, 동일/중복 테마 40% 이하. 목표 할당량이 아니며 현금 유지 가능. 실제 보유·체결내역 없이는 잔여 한도나 보유일수를 판단하지 않음.',
    '', '## 확인 필요 / 데이터 한계', '',
    '- 테마 등락·거래대금은 네이버 야간 스냅샷(시간외/시장 집계 포함 가능), 고점·휴식 계산은 일봉. 동일한 거래세션의 수익률로 혼합 계산하지 않음.',
    '- 공개 웹 데이터는 안정성·수정주가 처리를 보장하지 않음. 후보는 MTS의 수정주가 차트와 공시로 재확인. 전 종목 전수검색이 아니라 조건을 통과한 상위 10개 테마 내부 검색.',
    '- 당일 상승 재료·최신 공시·Threads 대조는 에이전트의 추가 확인 항목. 이 기계 보고서만으로 확인 완료라고 하지 않음.',
    ...r.errors.map(e => `- ${clean(e)}`), '', '## 수집 출처', '', ...r.sources.map(u => `- ${u}`), '');
  return lines.join('\n');
}

export async function run() {
  const evidence = [], errors = [], now = new Date();
  async function get(url, json = true) {
    const response = await fetch(url, { signal: AbortSignal.timeout(20000), headers: { 'User-Agent': 'Mozilla/5.0', Referer: `${origin}/` } });
    assert(response.ok, `HTTP ${response.status}: ${url}`);
    const raw = await response.text(); evidence.push({ url, fetchedAt: new Date().toISOString(), raw });
    return json ? JSON.parse(raw) : raw;
  }
  async function daily(code, asOf) {
    assert(/^[0-9A-Z]{6}$/.test(code));
    const url = `https://api.finance.naver.com/siseJson.naver?symbol=${code}&requestType=1&startTime=${ago(asOf, 430).replaceAll('-', '')}&endTime=${asOf.replaceAll('-', '')}&timeframe=day`;
    return parseBars(await get(url, false));
  }
  const dates = marketDates(await get(`${origin}/api/stockSecurity/exchanges/market-status?exchanges=krx`), now);
  const benchmark = await daily('005930', dates.asOf);
  assert(benchmark.at(-1).date === dates.asOf && benchmark.length > rules.recentDays, 'Benchmark daily/calendar mismatch');
  const listedBefore = benchmark.at(-rules.recentDays - 1).date.replaceAll('-', '');
  // Stop after all gain-eligible themes are covered; lower-ranked ties can overlap page boundaries.
  const allThemes = await collectPages(p => get(`${origin}/api/domestic/market/theme/list?startIdx=${p}&pageSize=100&sortType=changeRate`), 'no', belowThemeCutoff);
  assert(allThemes.length >= 100, 'Theme universe unexpectedly small');
  assert(allThemes.some(t => t.thistime?.slice(0, 8) === dates.asOf.replaceAll('-', '')), 'Entire theme snapshot is stale');
  const eligible = allThemes.filter(t => num(t.totalCnt) >= 5 && num(t.changeRate) >= rules.themeGain && num(t.riseCnt) / num(t.totalCnt) >= rules.breadth && !/신규상장|스팩/.test(t.name))
    .sort((a, b) => num(b.changeRate) - num(a.changeRate)).slice(0, rules.maxThemes);
  const stocks = new Map(), themes = [];
  for (const t of eligible) {
    const summary = { name: t.name, code: t.no, changeRate: num(t.changeRate), breadth: num(t.riseCnt) / num(t.totalCnt), strong: false };
    themes.push(summary);
    try {
      assert(t.thistime?.slice(0, 8) === dates.asOf.replaceAll('-', ''), '테마 기준일 불일치');
      const members = await collectPages(p => get(`${origin}/api/domestic/market/theme/${t.no}/stocklist?marketType=ALL&orderType=priceTop&startIdx=${p}&pageSize=100`), 'itemcode');
      assert(members.length === num(t.totalCnt), '테마 구성종목 수 불일치');
      const seasoned = members.filter(s => safeStock(s) && /^\d{8}$/.test(s.listedDate) && s.listedDate <= listedBefore);
      summary.medianChange = seasoned.length ? median(seasoned.map(s => num(s.prevChangeRate))) : null;
      summary.leaders = seasoned.filter(s => num(s.prevChangeRate) >= rules.leaderGain && num(s.tradeAmount) >= rules.turnover).length;
      summary.strong = seasoned.length >= 3 && summary.medianChange >= 1 && summary.leaders >= rules.leaders;
      summary.reason = summary.strong ? '' : '단일 급등/신규상장 효과 또는 동반 강세 부족';
      if (!summary.strong) continue;
      for (const s of seasoned) {
        const previous = stocks.get(s.itemcode);
        if (previous) previous.themes.push(t.name); else stocks.set(s.itemcode, { ...s, themes: [t.name] });
      }
    } catch (e) { summary.reason = e.message; errors.push(`${t.name}: ${e.message}`); }
  }
  const candidates = [], excluded = [], list = [...stocks.values()];
  let analyzedCount = 0;
  // ponytail: four public-data requests at once; use a licensed feed if rate/availability guarantees are needed.
  for (let i = 0; i < list.length; i += 4) await Promise.all(list.slice(i, i + 4).map(async s => {
    try {
      if (num(s.tradeAmount) < rules.turnover) { excluded.push({ code: s.itemcode, reason: '거래대금 100억원 미만' }); return; }
      const result = analyze(s, await daily(s.itemcode, dates.asOf), dates.asOf); analyzedCount++;
      if (result.reason) excluded.push({ code: s.itemcode, reason: result.reason });
      else candidates.push({ ...result, themes: s.themes });
    } catch (e) { errors.push(`${s.itemname} ${s.itemcode}: ${e.message}`); }
  }));
  const result = { ...dates, generatedAt: now.toISOString(), ruleVersion: 3, rules,
    status: errors.length ? '부분 데이터 — 확인 필요' : '기계 스캔 완료 · 재료/공시/Threads 미검증',
    themeCount: allThemes.length, themeCoverage: '등락률 하한까지 조회, 전체 테마 수가 아님', themes, stockCount: list.length, analyzedCount,
    candidates, selected: shortlist(candidates), excluded, errors, sources: evidence.map(x => x.url) };
  const out = path.join(root, 'outputs', 'nightly'); await fs.mkdir(out, { recursive: true });
  const stem = path.join(out, `${dates.runDate}_${now.toISOString().replace(/[:.]/g, '-')}`);
  await fs.writeFile(`${stem}.json`, JSON.stringify({ ...result, evidence }, null, 2), { flag: 'wx' });
  await fs.writeFile(`${stem}.md`, report(result), { flag: 'wx' });
  console.log(JSON.stringify({ report: `${stem}.md`, data: `${stem}.json`, ...dates, status: result.status, themes: themes.length, candidates: candidates.length, selected: result.selected.map(s => ({ code: s.code, name: s.name, setup: s.setup })), errors }, null, 2));
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) run().catch(e => { console.error(`SCAN FAILED — 새 관찰 목록을 발행하지 않습니다: ${e.message}`); process.exitCode = 1; });
