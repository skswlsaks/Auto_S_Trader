import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseBars, safeStock, collectPages } from './nightly_scan.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const origin = 'https://stock.naver.com';
export const rules = Object.freeze({ version: 'lecture-v3-news-all', referencePolicy: 'latest-confirmed-only', referenceDays: 60,
  bullishGain: 0.058, bearishHighGain: 0.12, themeGain: 2, breadth: 0.6,
  minTurnover: 10e9, selectionPolicy: 'all-qualified-with-news-review' });
const kstDate = d => new Date(new Date(d).getTime() + 9 * 3600000).toISOString().slice(0, 10);
const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
const ma = (bars, i, n) => i + 1 < n ? null : mean(bars.slice(i - n + 1, i + 1).map(b => b.close));
const finite = v => v !== null && v !== undefined && String(v).trim() !== '' && Number.isFinite(Number(v));
const price = v => v == null ? '미확인' : Number(v).toLocaleString('ko-KR', { maximumFractionDigits: 1 });
const clean = s => String(s).replace(/[|\r\n]/g, ' ');

export function session(payload, phase, now = new Date(), replay = false) {
  assert(['preview', 'close'].includes(phase), 'phase must be preview or close');
  const today = kstDate(now);
  assert(kstDate(payload.serverTime) === today && Math.abs(Date.parse(payload.serverTime) - now.getTime()) < 120000, '시장 일정 시각 지연/불일치');
  const statuses = payload.exchanges?.find(e => e.exchange === 'krx')?.statuses?.filter(s => s.stockType === 'stock' && ['KOSPI', 'KOSDAQ'].includes(s.marketType));
  assert(statuses?.length === 2 && new Set(statuses.map(s => s.marketType)).size === 2, '시장 일정 누락');
  assert(statuses.every(s => s.today?.date === today && typeof s.today.isTradingDay === 'boolean'), '오늘 거래일 확인 불가');
  assert(statuses.every(s => s.today.isTradingDay === statuses[0].today.isTradingDay && s.latest?.tradeBaseAt === statuses[0].latest?.tradeBaseAt), '시장별 일정 불일치');
  const local = new Date(now.getTime() + 9 * 3600000);
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  if (!statuses[0].today.isTradingDay) {
    if (!replay) return { skipped: '휴장일', runDate: today, phase };
    assert(phase === 'close' && statuses.every(s => s.latest.session.marketStatusDetailType === 'close'), '재현은 마감된 일봉만 가능');
    const asOf = statuses[0].latest.tradeBaseAt;
    assert(/^\d{4}-\d{2}-\d{2}$/.test(asOf) && asOf < today, '지난 거래일 불명확');
    return { asOf, runDate: today, phase, replay: true };
  }
  assert(!replay, '거래일에는 재현 모드 대신 현재 세션 사용');
  assert(statuses.every(s => s.latest.tradeBaseAt === today), '오늘 세션 미확인');
  if (phase === 'preview') {
    assert(minute >= 14 * 60 + 50 && minute < 15 * 60 + 20, '잠정 스캔은 14:50~15:19에만 발행');
    assert(statuses.every(s => s.latest.session.marketSessionType === 'regularMarket' && s.latest.session.marketStatusDetailType === 'open'), '정규장 진행 여부 미확인');
  } else {
    assert(minute >= 16 * 60 + 10 && minute <= 21 * 60, '일봉 재검사는 16:10~21:00에만 발행');
    assert(statuses.every(s => s.latest.session.marketSessionType === 'afterMarket' || s.latest.session.marketStatusDetailType === 'close'), '정규장 종료 미확인: 특별 개장일은 보류');
  }
  return { asOf: today, runDate: today, phase, replay: false };
}

export function reference(b, previous) {
  if (b.close > b.open && b.close / previous.close - 1 >= rules.bullishGain - 1e-12) {
    return { date: b.date, type: '양봉', level: b.close,
      secretLower: b.open + (b.close - b.open) / 3, secretUpper: b.close - (b.close - b.open) / 3 };
  }
  if (b.close < b.open && b.high / previous.close - 1 >= rules.bearishHighGain - 1e-12) {
    return { date: b.date, type: '음봉', level: b.open, secretLower: null, secretUpper: null };
  }
  return null;
}

export function analyzeLecture(stock, bars, dates) {
  if (!safeStock(stock)) return { reason: '거래정지/관리/시장경보/스팩/ETF 또는 안전 정보 미확인' };
  const i = bars.length - 1, last = bars[i];
  if (!last || last.date !== dates.asOf) return { reason: '일봉 기준일 불일치', dataIssue: true };
  const used = bars.slice(-Math.max(241, rules.referenceDays + 20));
  if (used.some((b, n) => !/^\d{4}-\d{2}-\d{2}$/.test(b.date) || n && b.date <= used[n - 1].date ||
    ![b.open, b.high, b.low, b.close, b.volume].every(v => Number.isFinite(v) && v > 0) ||
    b.high < Math.max(b.open, b.close) || b.low > Math.min(b.open, b.close))) return { reason: '일봉 누락/무거래/비정상 OHLCV', dataIssue: true };
  if (!finite(stock.tradeAmount) || Number(stock.tradeAmount) < rules.minTurnover) return { reason: '거래대금 100억원 미만 또는 미확인' };
  if (dates.phase === 'preview' && (stock.marketStatus !== 'OPEN' || stock.tradingSessionType !== 'REGULAR_MARKET' ||
    Number(stock.nowPrice) !== last.close)) return { reason: '장중 시세·일봉 불일치 또는 정규장 상태 미확인', dataIssue: true };
  const ma5 = ma(bars, i, 5), ma20 = ma(bars, i, 20), ma240 = ma(bars, i, 240);
  const prev20 = ma(bars, i - 1, 20), prev240 = ma(bars, i - 1, 240);
  const row = { code: stock.itemcode, name: stock.itemname, themes: stock.themes || [], asOf: dates.asOf,
    observedPrice: last.close, priceState: dates.phase === 'preview' ? '장중 잠정·종가 아님' : '공개 일봉·KRX 원장 미대조',
    turnover: Number(stock.tradeAmount), ma5, ma20, ma240, phase: dates.phase,
    ma20Recovered: prev20 !== null && bars[i - 1].close <= prev20 && last.close > ma20,
    ma240Crossed: prev240 !== null && bars[i - 1].close <= prev240 && last.close > ma240,
    reference: null, point: null, priority: 3,
    pendingReference: dates.phase === 'preview' && i > 0 ? reference(last, bars[i - 1]) : null,
    warnings: ['강의 해석 미확정·관찰용', '공시·상승 재료·수정주가·KRX/NXT 기준 수동 확인 필요'],
    sourceUrl: `${origin}/domestic/stock/${stock.itemcode}/price` };
  // IPO day counts require the actual listing date; a truncated history is not an IPO.
  const listed = /^\d{8}$/.test(stock.listedDate || '') ? stock.listedDate : null;
  const sinceListing = listed && bars[0].date.replaceAll('-', '') === listed ? bars.length : null;
  if ([1, 5].includes(sinceListing) && last.close > last.open && (sinceListing === 1 || last.close > ma5)) {
    return { ...row, setup: `신규상장 ${sinceListing}거래일 양봉${sinceListing === 5 ? '·5일선 위' : ''}`, priority: 2,
      warnings: [...row.warnings, '신규상장 별도 관찰: 20/240일선 전략과 분리, 테마 편입 종목만 검색'] };
  }
  if (ma20 === null) return { reason: '20일선 이력 부족·신규상장 별도 조건 미충족' };
  if (last.close < ma20) return { reason: '20일선 아래: 관찰 제외, 회복 후 재평가' };
  // The forming candle cannot replace a confirmed reference before the close.
  const referenceEnd = dates.phase === 'preview' ? i - 1 : i;
  for (let b = referenceEnd; b >= Math.max(19, i - rules.referenceDays); b--) {
    const ref = reference(bars[b], bars[b - 1]);
    if (!ref) continue;
    const after = bars.slice(b + 1);
    // A failed latest reference terminates this setup; never search older survivors.
    for (let n = 0; n < after.length; n++) {
      const c = after[n], average = ma(bars, b + n + 1, 20);
      const secretBroken = ref.secretUpper !== null && c.close < ref.secretUpper;
      if (!secretBroken && c.close >= average) continue;
      const invalidation = { date: c.date, type: secretBroken ? 'secret' : 'ma20', close: c.close,
        level: secretBroken ? ref.secretUpper : average, provisional: dates.phase === 'preview' && c.date === dates.asOf };
      return { reference: ref, invalidation,
        reason: `최근 기준봉 ${ref.date}: ${c.date} ${invalidation.provisional ? '장중 잠정가격' : '종가'} ${price(c.close)}원 < ${secretBroken ? '시크릿' : '20일선'} ${price(invalidation.level)}원 — 이전 기준봉으로 되돌리지 않음` };
    }
    let pointIndex = -1;
    for (let p = b + 1; p <= i; p++) if (bars[p - 1].close <= ref.level && bars[p].close > ref.level) { pointIndex = p; break; }
    const point = pointIndex < 0 ? null : { date: bars[pointIndex].date, level: bars[pointIndex].close };
    const crossed = pointIndex >= 0 && pointIndex < i && bars[i - 1].close <= point.level && last.close > point.level;
    const maintained = pointIndex >= 0 && pointIndex < i && last.close > point.level && !crossed;
    const gapHeld = after.length >= 3 && after.length <= 5 && after.every(c => c.low > bars[b].high);
    return { ...row, reference: ref, point, gapHeld,
      setup: b === i ? '당일 새 기준봉·후속 포인트 대기' : crossed ? '포인트 종가 첫/재돌파 조건' : maintained ? '이미 돌파 후 유지·신규 돌파 아님' : point ? '포인트 형성·후속 돌파 대기' : '최근 기준봉 유지·포인트 대기',
      priority: crossed ? 0 : point && !maintained ? 1 : 2,
      warnings: [...row.warnings, ...(ref.type === '음봉' ? ['음봉 시크릿선은 노트에 정의되지 않음'] : ['1/3 방향 미확정: 두 선 중 높은 선을 보수적 유지 조건으로 임시 사용'])] };
  }
  if (row.ma20Recovered || row.ma240Crossed) return { ...row,
    setup: row.ma20Recovered ? '20일선 회복·강한 반등 여부 미정' : '240일선 돌파·강도/지지 미정' };
  return { reason: '유효 기준봉/포인트 없음·시크릿/20일선 이탈 이력 또는 관찰 보조조건 미충족' };
}

export function select(rows) {
  const selected = [], seen = new Set();
  for (const row of [...rows].sort((a, b) => a.priority - b.priority || b.turnover - a.turnover)) {
    if (seen.has(row.code)) continue;
    seen.add(row.code);
    selected.push(row);
  }
  return selected;
}

export function eligibleThemes(rows) {
  return rows.filter(t => +t.totalCnt >= 5 && +t.changeRate >= rules.themeGain && +t.riseCnt / +t.totalCnt >= rules.breadth && !/신규상장|스팩/.test(t.name));
}

export function markdown(r) {
  return [`# ${r.asOf} ${r.phase === 'preview' ? '마감 전 잠정' : '일봉 재검사'} 관찰 목록`, '',
    `실행 ${r.generatedAt} · ${r.replay ? '지난 거래일 재현 — 오늘 신호 아님' : r.status} · ${r.rules?.version ?? rules.version}`, '',
    '매수/주문 신호가 아닙니다. 강의의 불명확한 정의는 확인 전 가설입니다. 토스 관심종목 동기화: 미연결.', '',
    '추세 판정은 가장 최근 확정 기준봉으로만 합니다. 새 기준봉이 생기면 갱신하며, 이탈한 최신 봉 대신 옛 기준봉을 찾아 통과시키지 않습니다. 당일 새 기준봉은 아직 후속 추세 유지가 검증된 봉이 아닙니다.', '',
    `검색: 강세 조건을 통과한 모든 테마의 종목 ${r.stockCount}개. 종목 수/테마별 개수 제한 없음. 전체 시장/신규상장 전수검색 아님.`, '',
    `아래 ${r.selected.length}개는 차트 조건 통과 전체이며 뉴스·산업 검토 전입니다. 최종 관찰 분류는 같은 실행의 _reviewed 파일을 확인하세요.`, '',
    '| 종목 | 관찰 상태 | 관측 가격 | 기준봉 가격 | 포인트 가격 | 시크릿 낮은선 / 높은선 | 20일선 |',
    '|---|---|---:|---:|---:|---:|---:|',
    ...r.selected.map(s => `| ${clean(s.name)} ${s.code} | ${s.setup} | ${price(s.observedPrice)} | ${price(s.reference?.level)} | ${price(s.point?.level)} | ${price(s.reference?.secretLower)} / ${price(s.reference?.secretUpper)} | ${price(s.ma20)} |`),
    ...(!r.selected.length ? ['| 조건 충족 없음 — 억지로 채우지 않음 | | | | | | |'] : []), '',
    ...r.selected.flatMap(s => [`## ${clean(s.name)} (${s.code})`, '',
      `- ${s.themes.map(clean).join(', ')} / 제공 거래대금 ${price(s.turnover / 1e8)}억원. 거래대금은 공급자 세션 집계로 정규장 전용 보장 없음.`,
      `- 기준봉 ${s.reference?.date ?? '해당 없음'} / 포인트봉 ${s.point?.date ?? '미형성'} / ${s.priceState}.`,
      ...(s.pendingReference ? [`- 장중 새 기준봉 후보 ${s.pendingReference.date}: 아직 미확정이므로 현재 기준봉을 교체하지 않음.`] : []),
      `- 다음 확인: ${s.point ? `포인트 ${price(s.point.level)}원 초과 마감` : s.reference ? `기준봉 ${price(s.reference.level)}원 초과 마감으로 포인트 형성 여부` : '이평선 회복/신규상장 별도 조건 수동 확인'}. 장중에는 확정할 수 없음.`,
      `- 제외/재검토: ${s.ma20 == null ? '20일선 자료 없음' : `20일선 ${price(s.ma20)}원 아래`}${s.reference?.secretUpper ? ` 또는 임시 시크릿 ${price(s.reference.secretUpper)}원 아래` : ''}. 실제 손절 주문이 아님.`,
      `- 참고: 240일선 ${price(s.ma240)}원 / 종가 교차 ${s.ma240Crossed ? '있음' : '없음/미확인'} / 엄격한 완전갭 3~5일 유지 ${s.gapHeld ? '있음' : '없음/미확인'}.`,
      ...s.warnings.map(w => `- 주의: ${w}`), '']),
    '## 최근 기준봉 이탈로 제외', '',
    '| 종목 | 최근 기준봉 | 최초 이탈일 | 종가 또는 잠정가격 | 이탈 기준선 |', '|---|---|---|---:|---:|',
    ...(r.excluded || []).filter(s => s.invalidation).map(s => `| ${clean(s.name || '')} ${s.code} | ${s.reference.date} | ${s.invalidation.date}${s.invalidation.provisional ? ' (장중 잠정)' : ''} | ${price(s.invalidation.close)} | ${s.invalidation.type === 'secret' ? '시크릿' : '20일선'} ${price(s.invalidation.level)} |`), '',
    '## 수집 한계', '', ...r.errors.map(e => `- ${clean(e)}`),
    '- 장전/장중/정규장/NXT/시간외 가격을 섞어 종가 체결 또는 수익률로 해석하지 않습니다.',
    '- 노트의 +3%에서 원물량 25%, +5%에서 원물량 25% 익절은 계획으로만 보존합니다. 실제 매입가·물량이 없어 가격/주문을 생성하지 않습니다.',
    '- 최신 공시·당일 재료 확인은 예약 에이전트의 별도 브리핑에 기록합니다. 이 표만으로 검증 완료가 아닙니다.', '',
    '## 원자료', '', ...r.sources.map(url => `- ${url}`), ''].join('\n');
}

export async function run({ phase = 'close', replay = false } = {}) {
  const out = path.join(root, 'outputs', 'closing');
  await fs.mkdir(path.join(out, 'runs'), { recursive: true });
  const lock = path.join(out, '.scan-lock');
  await fs.mkdir(lock); // Concurrent runs must not replace each other's current list.
  const now = new Date(), evidence = [], errors = [];
  async function atomic(name, body) {
    const target = path.join(out, name), temp = `${target}.${process.pid}.tmp`;
    await fs.writeFile(temp, body, { flag: 'wx' }); await fs.rename(temp, target);
  }
  async function get(url, json = true) {
    const response = await fetch(url, { signal: AbortSignal.timeout(15000), headers: { 'User-Agent': 'Mozilla/5.0', Referer: `${origin}/` } });
    assert(response.ok, `HTTP ${response.status}: ${url}`);
    const raw = await response.text(); evidence.push({ url, fetchedAt: new Date().toISOString(), raw });
    return json ? JSON.parse(raw) : raw;
  }
  async function daily(code, asOf) {
    assert(/^[0-9A-Z]{6}$/.test(code));
    const start = new Date(Date.parse(`${asOf}T00:00:00Z`) - 600 * 86400000).toISOString().slice(0, 10).replaceAll('-', '');
    return parseBars(await get(`https://api.finance.naver.com/siseJson.naver?symbol=${code}&requestType=1&startTime=${start}&endTime=${asOf.replaceAll('-', '')}&timeframe=day`, false));
  }
  try {
    const dates = session(await get(`${origin}/api/stockSecurity/exchanges/market-status?exchanges=krx`), phase, now, replay);
    if (dates.skipped) {
      const result = { ...dates, generatedAt: now.toISOString(), published: false };
      await atomic('status.json', JSON.stringify(result, null, 2)); return result;
    }
    const benchmark = await daily('005930', dates.asOf);
    assert(benchmark.at(-1).date === dates.asOf, '시장 일정과 대표 종목 일봉 불일치');
    const allThemes = await collectPages(p => get(`${origin}/api/domestic/market/theme/list?startIdx=${p}&pageSize=100&sortType=changeRate`), 'no', page => {
      assert(page.every((t, i) => finite(t.changeRate) && (!i || +page[i - 1].changeRate >= +t.changeRate)), '테마 정렬 불일치');
      return +page.at(-1).changeRate < rules.themeGain;
    });
    const themes = eligibleThemes(allThemes);
    assert(allThemes.length && allThemes.some(t => t.thistime?.slice(0, 8) === dates.asOf.replaceAll('-', '')), '테마 기준일 불일치');
    const stocks = new Map();
    for (const t of themes) {
      try {
        assert(t.thistime?.slice(0, 8) === dates.asOf.replaceAll('-', ''), `${t.name}: 테마 날짜 불일치`);
        if (phase === 'preview') {
          const stamp = t.thistime;
          const time = Date.parse(`${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}T${stamp.slice(8, 10)}:${stamp.slice(10, 12)}:${stamp.slice(12, 14)}+09:00`);
          assert(Math.abs(Date.now() - time) <= 120000, `${t.name}: 장중 테마 시세 지연`);
        }
        const members = await collectPages(p => get(`${origin}/api/domestic/market/theme/${t.no}/stocklist?marketType=ALL&orderType=priceTop&startIdx=${p}&pageSize=100`), 'itemcode');
        assert(members.length === +t.totalCnt, `${t.name}: 구성종목 누락`);
        for (const s of members) {
          if (stocks.has(s.itemcode)) stocks.get(s.itemcode).themes.push(t.name);
          else stocks.set(s.itemcode, { ...s, themes: [t.name] });
        }
      } catch (e) { errors.push(e.message); }
    }
    const candidates = [], excluded = [], list = [...stocks.values()];
    for (let j = 0; j < list.length; j += 4) await Promise.all(list.slice(j, j + 4).map(async stock => {
      try {
        if (!safeStock(stock)) {
          excluded.push({ code: stock.itemcode, reason: '안전/유동성 조건 제외' }); return;
        }
        assert(finite(stock.tradeAmount), '거래대금 누락');
        if (+stock.tradeAmount < rules.minTurnover) { excluded.push({ code: stock.itemcode, reason: '거래대금 100억원 미만' }); return; }
        const bars = await daily(stock.itemcode, dates.asOf);
        const expected = benchmark.filter(b => b.date >= bars[0].date).map(b => b.date);
        assert(expected.length && expected.join() === bars.map(b => b.date).join(), '시장 거래일과 일봉 누락/날짜 불일치');
        const result = analyzeLecture(stock, bars, dates);
        assert(!result.dataIssue, result.reason);
        if (result.reason) excluded.push({ code: stock.itemcode, name: stock.itemname, ...result });
        else candidates.push(result);
      } catch (e) { errors.push(`${stock.itemcode}: ${e.message}`); }
    }));
    // A delayed preview is not a near-close snapshot; don't publish it as one.
    session(await get(`${origin}/api/stockSecurity/exchanges/market-status?exchanges=krx`), phase, new Date(), replay);
    if (phase === 'preview') assert(Date.now() - now.getTime() <= 120000, '장중 스캔 2분 초과: 잠정 목록 발행 보류');
    let previous = null;
    try { previous = JSON.parse(await fs.readFile(path.join(out, `watchlist-${phase}.json`), 'utf8')); }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
    const sameRules = previous?.rules?.version === rules.version;
    const selected = select(candidates), before = new Set((sameRules ? previous.selected : []).map(s => s.code));
    const changes = { ruleChanged: !!previous && !sameRules, previousRuleVersion: previous?.rules?.version ?? null,
      added: selected.filter(s => !before.has(s.code)).map(s => s.code),
      retained: selected.filter(s => before.has(s.code)).map(s => s.code),
      removed: [...before].filter(code => !selected.some(s => s.code === code)) };
    const result = { ...dates, generatedAt: now.toISOString(), completedAt: new Date().toISOString(), rules,
      status: errors.length ? '부분 데이터·현재 목록 교체 보류' : '관찰용 계산 완료·강의 정의/공시 미확정',
      published: !replay && !errors.length, brokerSync: 'not_connected', themes, stockCount: stocks.size,
      researchStatus: 'pending', selectionStage: 'chart-only',
      candidates, selected, excluded, errors, changes, sources: [...new Set(evidence.map(e => e.url))] };
    const stem = path.join(out, 'runs', `${dates.asOf}_${phase}_${now.toISOString().replace(/[:.]/g, '-')}`);
    await fs.writeFile(`${stem}.json`, JSON.stringify({ ...result, evidence }, null, 2), { flag: 'wx' });
    await fs.writeFile(`${stem}.md`, markdown(result), { flag: 'wx' });
    if (result.published) {
      await atomic(`watchlist-${phase}.json`, JSON.stringify(result, null, 2));
      await atomic(`watchlist-${phase}.md`, markdown(result));
    }
    await atomic('status.json', JSON.stringify({ ...dates, ruleVersion: rules.version, generatedAt: result.generatedAt, status: result.status, published: result.published, report: `${stem}.md`, data: `${stem}.json` }, null, 2));
    return { ...result, report: `${stem}.md`, data: `${stem}.json` };
  } catch (e) {
    await atomic('status.json', JSON.stringify({ generatedAt: new Date().toISOString(), phase, status: 'failed', published: false, error: e.message }, null, 2));
    throw e;
  } finally { await fs.rmdir(lock); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2), phaseAt = args.indexOf('--phase');
  assert(args.every((a, i) => a === '--phase' || a === '--replay' || i === phaseAt + 1 && ['preview', 'close'].includes(a)), 'Unknown argument');
  run({ phase: phaseAt < 0 ? 'close' : args[phaseAt + 1], replay: args.includes('--replay') })
    .then(r => console.log(JSON.stringify({ report: r.report, data: r.data, asOf: r.asOf, skipped: r.skipped, status: r.status,
      published: r.published, selected: r.selected?.map(s => ({ code: s.code, name: s.name, setup: s.setup })), errors: r.errors }, null, 2)))
    .catch(e => { console.error(`CLOSING SCAN FAILED — 현재 목록 유지·최신 자료로 사용 금지: ${e.message}`); process.exitCode = 1; });
}
