// 종가베팅 스크리너 — 강의 노트 확정 규칙(2026-10-11) 기반
// 사용법:
//   node jongga_scan.mjs preview            # 장중 15:00~15:15 잠정 스캔 (15:20 동시호가 매수용)
//   node jongga_scan.mjs close              # 16:10 이후 확정 재검증 (내일 워치리스트 포함)
//   node jongga_scan.mjs close --replay     # 휴장일에 직전 거래일 기준으로 재현
//   node jongga_scan.mjs preview --replay   # 직전 거래일 데이터를 '장중'처럼 재현 (테스트용)
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));

export const RULES = Object.freeze({
  version: 'jongga-v1',
  bullishGain: 0.058,        // 양봉 기준봉: 전일 종가 대비 종가 +5.8% 이상 (6% 기준, 5.8%부터 포함)
  bearishHighGain: 0.12,     // 음봉 기준봉: 전일 종가 대비 고가 +12% 이상, 기준가격 = 시가
  referenceWindow: 60,       // 최근 60거래일 안의 기준봉만 사용 (운영값)
  liquidityWarn: 10e9,       // 거래대금 100억원 미만은 '유동성 주의' 표시 (제외하지 않음)
  nearPoint: 0.05,           // 내일 후보: 포인트 종가까지 5% 이내
  takeProfit: [0.03, 0.05],  // 최신 사용자 규칙: +3%에 원수량 35%, +5%에 35% (실행은 autotrader 모의 엔진)
});

// ---------- 순수 계산 함수 (테스트 대상) ----------

export function sma(bars, i, n) {
  if (i + 1 < n) return null;
  let s = 0;
  for (let k = i - n + 1; k <= i; k++) s += bars[k].close;
  return s / n;
}

/** 기준봉 판정. 양봉: 종가 +5.8%↑, 음봉: 고가 +12%↑ (기준가격=시가). */
export function referenceOf(bar, prev) {
  if (!prev || !(prev.close > 0)) return null;
  if (bar.close > bar.open && bar.close / prev.close - 1 >= RULES.bullishGain - 1e-9) {
    return { date: bar.date, type: '양봉', open: bar.open, close: bar.close, high: bar.high,
      level: bar.close, secret: bar.close - (bar.close - bar.open) / 3 }; // 몸통 위에서 1/3
  }
  if (bar.close < bar.open && bar.high / prev.close - 1 >= RULES.bearishHighGain - 1e-9) {
    return { date: bar.date, type: '음봉', open: bar.open, close: bar.close, high: bar.high,
      level: bar.open, secret: null }; // 음봉 시크릿은 노트에 정의 없음 → 20일선만 적용
  }
  return null;
}

/**
 * bars의 마지막 봉 = 오늘(장중이면 잠정). 그 전 봉까지로 '어제까지의 셋업'을 만들고, 오늘 봉으로 판정한다.
 * status:
 *   entry            오늘 종가가 포인트봉 종가를 돌파 (첫 돌파 또는 시크릿 유지 중 재돌파) → 종가베팅
 *   above_point      이미 포인트 위에서 유지 중 (신규 돌파 아님)
 *   waiting_breakout 포인트 형성됨, 돌파 대기
 *   point_today      오늘 포인트봉 형성 (내일부터 돌파 대기)
 *   waiting_point    기준봉 유지, 포인트 미형성
 *   exit_today       오늘 시크릿선 또는 20일선 아래로 마감 → 정리 판단
 *   invalidated      어제까지 이미 이탈한 셋업
 *   no_setup         최근 60거래일 기준봉 없음
 */
export function evaluate(bars) {
  const t = bars.length - 1;
  if (t < 1) return { status: 'no_setup', reason: '이력 부족' };
  const today = bars[t], prev = bars[t - 1];
  const ma5 = sma(bars, t, 5), ma20 = sma(bars, t, 20), ma240 = sma(bars, t, 240);
  const prevMa20 = sma(bars, t - 1, 20), prevMa240 = sma(bars, t - 1, 240);
  const flags = [];
  const todayRef = referenceOf(today, prev);
  if (todayRef) flags.push(`오늘 새 ${todayRef.type} 기준봉`);
  if (prevMa20 !== null && prev.close < prevMa20 && today.close > ma20) flags.push('20일선 돌파(의지)');
  if (prevMa240 !== null && prev.close < prevMa240 && today.close > ma240) flags.push('240일선 돌파(세력선)');
  const base = { today, prevClose: prev.close, changePct: today.close / prev.close - 1, ma5, ma20, ma240, todayRef, flags };

  // 1) 어제까지 확정된 가장 최근 기준봉 하나만 사용 (이탈 시 옛 기준봉으로 되돌아가지 않음)
  let r = -1, ref = null;
  for (let k = t - 1; k >= Math.max(1, t - RULES.referenceWindow); k--) {
    ref = referenceOf(bars[k], bars[k - 1]);
    if (ref) { r = k; break; }
  }
  if (r < 0) return { ...base, status: 'no_setup', ref: null };

  // 2) 기준봉 이후 ~ 어제까지 시크릿/20일선 종가 이탈 확인
  for (let k = r + 1; k <= t - 1; k++) {
    const m = sma(bars, k, 20), c = bars[k].close;
    if (ref.secret !== null && c < ref.secret) return { ...base, status: 'invalidated', ref, broken: { date: bars[k].date, close: c, line: '시크릿', level: ref.secret } };
    if (m !== null && c < m) return { ...base, status: 'invalidated', ref, broken: { date: bars[k].date, close: c, line: '20일선', level: m } };
  }

  // 3) 포인트봉: 기준봉 이후 처음으로 종가가 기준가격(양봉=종가, 음봉=시가)을 넘은 봉 (어제까지)
  let p = -1;
  for (let k = r + 1; k <= t - 1; k++) if (bars[k].close > ref.level) { p = k; break; }
  const point = p < 0 ? null : { date: bars[p].date, close: bars[p].close };

  // 갭 유지: 기준봉 이후 모든 봉의 저가가 기준봉 고가 위 (3~5일차 급등 가능 구간)
  const after = bars.slice(r + 1, t + 1);
  const gapDays = after.length && after.every(b => b.low > ref.high) ? after.length : 0;
  if (gapDays >= 3 && gapDays <= 5) flags.push(`기준봉 갭 유지 ${gapDays}일차`);
  if (ma20 === null) flags.push('20일선 이력 부족');
  if (ref.type === '음봉') flags.push('음봉 기준봉: 시크릿 미정의, 20일선만 적용');

  const out = { ...base, ref, point, refIndexAgo: t - r };
  // 4) 오늘 봉 판정
  if (ref.secret !== null && today.close < ref.secret) return { ...out, status: 'exit_today', broken: { date: today.date, close: today.close, line: '시크릿', level: ref.secret } };
  if (ma20 !== null && today.close < ma20) return { ...out, status: 'exit_today', broken: { date: today.date, close: today.close, line: '20일선', level: ma20 } };
  if (!point) return { ...out, status: today.close > ref.level ? 'point_today' : 'waiting_point' };
  if (prev.close <= point.close && today.close > point.close) {
    let rebreak = false;
    for (let k = p + 1; k <= t - 1; k++) if (bars[k].close > point.close) { rebreak = true; break; }
    if (rebreak) flags.push('재돌파');
    if (todayRef) flags.push('돌파봉이 새 기준봉 → 내일부터 이 봉 기준으로 갱신');
    return { ...out, status: 'entry', rebreak, breakoutPct: today.close / point.close - 1 };
  }
  if (today.close > point.close) return { ...out, status: 'above_point' };
  return { ...out, status: 'waiting_breakout', distancePct: point.close / today.close - 1 };
}

/** 신규상장: 첫날 양봉 마감 / 5일째 5일선 위 양봉 마감 */
export function evaluateIpo(bars, listedDate) {
  if (!/^\d{8}$/.test(listedDate || '') || !bars.length) return null;
  const first = `${listedDate.slice(0, 4)}-${listedDate.slice(4, 6)}-${listedDate.slice(6)}`;
  if (bars[0].date !== first || bars.length > 5) return null;
  const t = bars.length - 1, b = bars[t];
  if (!(b.close > b.open)) return null;
  if (bars.length === 1) return { status: 'ipo', day: 1, note: '신규상장 첫날 양봉 마감' };
  if (bars.length === 5 && b.close > sma(bars, t, 5)) return { status: 'ipo', day: 5, note: '신규상장 5일째 5일선 위 양봉' };
  return null;
}

/** 안전 필터: 거래정지·관리종목·투자경고/위험·스팩·리츠/펀드류 제외. 투자주의는 표시만. */
export function safety(s) {
  if (!/^[0-9A-Z]{6}$/.test(s.itemcode || '')) return { ok: false, reason: '코드 이상' };
  if (s.tradeStopYn !== 'N' || s.tradableStatus !== 'tradable') return { ok: false, reason: '거래정지' };
  if (s.manageStatusGb !== '0') return { ok: false, reason: '관리/정리 종목' };
  if (['02', '03'].includes(s.marketAlertType)) return { ok: false, reason: s.marketAlertType === '02' ? '투자경고' : '투자위험' };
  if (/스팩|SPAC/i.test(s.itemname || '')) return { ok: false, reason: '스팩' };
  if (!['ST', 'DR', 'FS'].includes(s.type)) return { ok: false, reason: `주식 아님(${s.type})` };
  return { ok: true, caution: s.marketAlertType === '01' };
}

export const GRADE_ORDER = { '강세': 0, '중립': 1, '약세': 2, '테마 없음': 3, '미확인': 4 };
const median = a => { const v = a.filter(Number.isFinite).sort((x, y) => x - y); return v.length ? (v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2) : null; };

/**
 * 테마 동향 등급 (운영 기준 — 강의 수치 아님):
 *  강세: (오늘 테마 +2%↑ & 상승비율 60%↑) 또는 (구성종목 5일 수익률 중간값 +3%↑ & 상승비율 50%↑ & 오늘 0%↑)
 *  약세: 5일 중간값 < 0 & 오늘 < 0
 *  구성종목 5개 미만 테마는 최대 '중립'
 */
export function sectorStats(theme, memberRows) {
  const today = +theme.changeRate, total = +theme.totalCnt, breadth = total ? +theme.riseCnt / total : 0;
  const med5 = median(memberRows.map(x => x.r5)), med20 = median(memberRows.map(x => x.r20));
  const refs5 = memberRows.filter(x => x.recentRef).length;
  let grade = '중립';
  if ((today >= 2 && breadth >= 0.6) || (med5 !== null && med5 >= 0.03 && breadth >= 0.5 && today >= 0)) grade = '강세';
  else if (med5 !== null && med5 < 0 && today < 0) grade = '약세';
  if (total < 5 && grade === '강세') grade = '중립';
  const leaders = String(theme.leadingItem || '').split('|').map(s => s.split(',')[2]).filter(Boolean);
  const signals = memberRows.filter(x => ['entry', 'waiting_breakout', 'point_today'].includes(x.status) || x.todayRef).map(x => `${x.name}${x.status === 'entry' ? '★' : ''}`);
  return { no: theme.no, name: theme.name, today, total, breadth, med5, med20, refs5, grade, leaders, signals,
    score: today + (med5 ?? 0) * 100, members: memberRows.map(x => x.code) };
}

export function parseSise(text) {
  const bars = [...text.matchAll(/\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)/g)].map(m => ({
    date: `${m[1].slice(0, 4)}-${m[1].slice(4, 6)}-${m[1].slice(6)}`,
    open: +m[2], high: +m[3], low: +m[4], close: +m[5], volume: +m[6] }));
  for (let i = 1; i < bars.length; i++) if (bars[i].date <= bars[i - 1].date) throw new Error('일봉 날짜 순서 오류');
  return bars;
}

// ---------- 보고서 ----------

const won = v => v == null || !Number.isFinite(v) ? '-' : Math.round(v).toLocaleString('ko-KR');
const pct = v => v == null || !Number.isFinite(v) ? '-' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
const eok = v => v == null ? '-' : `${Math.round(v / 1e8).toLocaleString('ko-KR')}억`;
const esc = s => String(s ?? '').replace(/[|\r\n]/g, ' ');

function notes(x) {
  const n = [...(x.flags || [])];
  if (x.caution) n.push('투자주의');
  if (x.turnover < RULES.liquidityWarn) n.push('거래대금 100억 미만');
  return n.join(', ') || '-';
}

export function markdown(r) {
  const preview = r.phase === 'preview';
  const L = [];
  const liquid = x => x.turnover >= RULES.liquidityWarn;
  const sec = x => x.sectors?.length ? x.sectors.slice(0, 2).map(s => `${esc(s.name)}(${s.grade} ${s.today >= 0 ? '+' : ''}${s.today.toFixed(1)}%)`).join(', ') : x.sectorGrade;
  const names = arr => arr.map(x => `${esc(x.name)}(${x.code})`).join(' · ');
  L.push(`# 종가베팅 스캔 — ${r.asOf} ${preview ? '장중 잠정' : '마감 확정'}`, '');
  L.push(`실행 ${r.runAtKst} KST · ${r.replay ? '**재현 실행(과거 데이터)** · ' : ''}규칙 ${RULES.version} · 전체 ${r.universe}종목 중 분석 ${r.analyzed}, 제외 ${r.excludedCount}, 오류 ${r.errors.length}`, '');
  if (preview) L.push('> 가격은 스캔 시점의 현재가입니다. 15:20 동시호가 직전에 토스에서 현재가가 여전히 포인트 종가 위인지 확인하세요. 최종 판정은 16:10 확정 스캔에서 합니다.', '');

  // 섹터 동향
  const strong = (r.sectors || []).filter(s => s.grade === '강세');
  L.push(`## 섹터 동향 — 강세 테마 ${strong.length}개`, '');
  L.push('강세 = 오늘 테마 +2%↑·상승비율 60%↑, 또는 구성종목 5일 수익률 중간값 +3%↑·상승비율 50%↑ (운영 기준). ★ = 오늘 진입 신호.', '');
  if (r.themeError) L.push(`테마 데이터 오류: ${esc(r.themeError)}`, '');
  L.push('| 테마 | 오늘 | 상승비율 | 5일 중간값 | 20일 중간값 | 최근5일 기준봉 | 대장주 | 셋업 종목 |', '|---|---:|---:|---:|---:|---:|---|---|');
  for (const s of strong.slice(0, 15)) L.push(`| ${esc(s.name)} (${s.total}) | ${s.today >= 0 ? '+' : ''}${s.today.toFixed(2)}% | ${Math.round(s.breadth * 100)}% | ${pct(s.med5)} | ${pct(s.med20)} | ${s.refs5} | ${esc(s.leaders.join(', '))} | ${esc(s.signals.slice(0, 8).join(', ')) || '-'} |`);
  if (!strong.length) L.push('| 강세 테마 없음 | | | | | | | |');
  if (strong.length > 15) L.push('', `그 외 강세 테마: ${strong.slice(15).map(s => esc(s.name)).join(' · ')}`);
  const weak = (r.sectors || []).filter(s => s.grade === '약세').sort((a, b) => a.score - b.score).slice(0, 8);
  if (weak.length) L.push('', `약세 테마(상위 8): ${weak.map(s => `${esc(s.name)} ${s.today.toFixed(1)}%/5일 ${pct(s.med5)}`).join(' · ')}`);
  L.push('');

  const entryRow = x => `| ${esc(x.name)} ${x.code} | ${x.market} | ${won(x.today.close)} | ${pct(x.changePct)} | ${won(x.point.close)} (${x.point.date.slice(5)}) | ${pct(x.breakoutPct)} | ${x.ref.secret ? won(x.ref.secret) : '미정의'} | ${won(x.ma20)} | ${won(x.today.close * 1.03)} / ${won(x.today.close * 1.05)} | ${eok(x.turnover)} | **${x.sectorGrade}** ${sec(x)} | 기준봉 ${x.ref.date.slice(5)}; ${notes(x)} |`;
  const entryHead = ['| 종목 | 시장 | 현재가 | 등락 | 포인트 종가(일자) | 돌파폭 | 시크릿선 | 20일선 | +3% / +5% | 거래대금 | 섹터 | 비고 |', '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|'];
  const entries = r.groups.entry.filter(liquid);
  L.push(`## ${preview ? '진입 후보' : '오늘 진입 신호(확정)'} — ${entries.length}개 (거래대금 100억 이상, 섹터 강세 순)`, '');
  L.push('포인트봉 종가를 오늘 종가가 돌파한 종목. 시크릿선 = 종가 기준 이탈 시 정리 판단선. 익절가는 현재가 기준 참고값.', '');
  L.push(...entryHead, ...entries.map(entryRow));
  if (!entries.length) L.push('| 해당 없음 | | | | | | | | | | | |');
  L.push('');
  const thinEntries = r.groups.entry.filter(x => !liquid(x));
  if (thinEntries.length) {
    L.push(`### 거래대금 100억 미만 진입 신호 — ${thinEntries.length}개 (체결·슬리피지 주의)`, '', ...entryHead, ...thinEntries.map(entryRow), '');
  }
  if (r.groups.ipo.length) {
    L.push(`## 신규상장 신호 — ${r.groups.ipo.length}개`, '', '| 종목 | 시장 | 현재가 | 등락 | 조건 | 거래대금 | 섹터 |', '|---|---|---:|---:|---|---:|---|');
    for (const x of r.groups.ipo) L.push(`| ${esc(x.name)} ${x.code} | ${x.market} | ${won(x.today.close)} | ${pct(x.changePct)} | ${x.ipo.note} | ${eok(x.turnover)} | ${x.sectorGrade} ${sec(x)} |`);
    L.push('');
  }
  if (r.watch?.length) {
    const label = { entry: '진입 신호', above_point: '포인트 위 유지', waiting_breakout: '포인트 돌파 대기', point_today: '오늘 포인트 형성', waiting_point: '포인트 미형성', exit_today: '오늘 이탈', invalidated: '이탈(셋업 종료)', no_setup: '셋업 없음' };
    L.push(`## 내 관심종목 — ${r.watch.length}개`, '', '| 종목 | 상태 | 종가 | 등락 | 기준봉 | 포인트 종가 | 시크릿선 | 20일선 | 섹터 | 메모 |', '|---|---|---:|---:|---|---:|---:|---:|---|---|');
    for (const x of r.watch) {
      if (!x.status) { L.push(`| ${esc(x.name)} ${x.code} | 데이터 없음(${esc(x.reason || '제외/미분석')}) | | | | | | | | |`); continue; }
      const memo = [x.broken ? `${x.broken.date.slice(5)} ${x.broken.line} ${won(x.broken.level)} 하회` : '', x.distancePct != null ? `돌파까지 ${pct(x.distancePct)}` : '', notes(x)].filter(s => s && s !== '-').join('; ');
      L.push(`| ${esc(x.name)} ${x.code} | **${label[x.status] || x.status}** | ${won(x.today.close)} | ${pct(x.changePct)} | ${x.ref ? `${x.ref.date.slice(5)} ${x.ref.type}` : '-'} | ${x.point ? won(x.point.close) : '-'} | ${x.ref?.secret ? won(x.ref.secret) : '-'} | ${won(x.ma20)} | ${x.sectorGrade} ${sec(x)} | ${memo || '-'} |`);
    }
    L.push('');
  }
  const nearAll = r.groups.waiting_breakout.filter(x => x.distancePct <= RULES.nearPoint).sort((a, b) => (GRADE_ORDER[a.sectorGrade] ?? 9) - (GRADE_ORDER[b.sectorGrade] ?? 9) || a.distancePct - b.distancePct);
  const near = nearAll.filter(liquid);
  L.push(`## 다음 거래일 후보 — 포인트 돌파 대기, 포인트까지 ${RULES.nearPoint * 100}% 이내 (거래대금 100억 이상 ${near.length}개 / 전체 대기 ${r.groups.waiting_breakout.length}개, 섹터 강세 순)`, '');
  L.push('| 종목 | 시장 | 종가 | 포인트 종가(일자) | 남은 거리 | 시크릿선 | 20일선 | 거래대금 | 섹터 | 비고 |', '|---|---|---:|---:|---:|---:|---:|---:|---|---|');
  for (const x of near) L.push(`| ${esc(x.name)} ${x.code} | ${x.market} | ${won(x.today.close)} | ${won(x.point.close)} (${x.point.date.slice(5)}) | ${pct(x.distancePct)} | ${x.ref.secret ? won(x.ref.secret) : '미정의'} | ${won(x.ma20)} | ${eok(x.turnover)} | **${x.sectorGrade}** ${sec(x)} | ${notes(x)} |`);
  if (!near.length) L.push('| 해당 없음 | | | | | | | | | |');
  L.push('');
  const thinNear = nearAll.filter(x => !liquid(x));
  if (thinNear.length) L.push(`거래대금 100억 미만 돌파 대기 ${thinNear.length}개 — 섹터 강세: ${names(thinNear.filter(x => x.sectorGrade === '강세')) || '없음'} / 그 외: ${names(thinNear.filter(x => x.sectorGrade !== '강세'))}`, '');
  const pt = r.groups.point_today.filter(liquid);
  L.push(`## 오늘 포인트봉 형성 — ${pt.length}개 (다음 거래일부터 이 봉 종가 돌파 대기, 거래대금 100억 이상)`, '');
  if (pt.length) L.push(pt.map(x => `${esc(x.name)}(${x.code}) ${won(x.today.close)} ${pct(x.changePct)} [${x.sectorGrade}]`).join(' · '), '');
  const nr = r.groups.new_reference.filter(liquid);
  L.push(`## 오늘 새 기준봉 — ${nr.length}개 ${preview ? '(장중 잠정, 마감 후 확정)' : '(다음 거래일부터 포인트 형성 대기)'}, 거래대금 100억 이상`, '');
  if (nr.length) L.push(nr.map(x => `${esc(x.name)}(${x.code}) ${pct(x.changePct)} [${x.sectorGrade}]`).join(' · '), '');
  const ex = r.groups.exit_today.filter(liquid);
  L.push(`## 오늘 시크릿/20일선 이탈 — ${ex.length}개 (보유 중이면 정리 판단, 거래대금 100억 이상 / 전체 ${r.groups.exit_today.length}개)`, '');
  if (ex.length) L.push(ex.map(x => `${esc(x.name)}(${x.code}) ${won(x.today.close)} < ${x.broken.line} ${won(x.broken.level)}`).join(' · '), '');
  L.push('## 집계', '', `100억 미만 포함 전체: 진입 ${r.groups.entry.length} · 포인트 형성 ${r.groups.point_today.length} · 새 기준봉 ${r.groups.new_reference.length} · 포인트 위 유지 ${r.groups.above_point.length} · 포인트 미형성 ${r.groups.waiting_point.length} · 이미 이탈 ${r.counts.invalidated} · 셋업 없음 ${r.counts.no_setup}`, '');
  L.push(`제외 사유: ${Object.entries(r.excludedReasons).map(([k, v]) => `${k} ${v}`).join(', ') || '없음'}`, '');
  if (r.errors.length) L.push(`데이터 오류 ${r.errors.length}건: ${r.errors.slice(0, 10).map(esc).join(' / ')}${r.errors.length > 10 ? ' …' : ''}`, '');
  L.push('---', '데이터: 네이버 증권(KRX 정규장 기준, 테마 분류 포함). 자동주문 없음 — 매매는 토스에서 직접. 뉴스·공시 확인은 별도.');
  return L.join('\n');
}

// ---------- 실행 ----------

const HEADERS = { 'User-Agent': 'Mozilla/5.0', Referer: 'https://stock.naver.com/' };
async function get(url, retries = 2) {
  for (let a = 0; ; a++) {
    try {
      const res = await fetch(url, { headers: HEADERS, signal: AbortSignal.timeout(15000) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    } catch (e) {
      if (a >= retries) throw new Error(`${e.message}: ${url}`);
      await new Promise(r => setTimeout(r, 500 * (a + 1)));
    }
  }
}
const kst = (d = new Date()) => new Date(d.getTime() + 9 * 3600e3).toISOString().replace('T', ' ').slice(0, 16);
const ymd = s => s.replaceAll('-', '');

async function marketList() {
  const all = [];
  for (const market of ['KOSPI', 'KOSDAQ']) {
    const seen = new Set();
    for (let page = 0; page < 40; page++) {
      const rows = JSON.parse(await get(`https://stock.naver.com/api/domestic/market/stock/default?tradeType=KRX&marketType=${market}&orderType=marketSum&startIdx=${page}&pageSize=100`));
      for (const s of rows) if (!seen.has(s.itemcode)) { seen.add(s.itemcode); all.push({ ...s, market }); }
      if (rows.length < 100) break;
    }
  }
  return all;
}

async function themeData() {
  const themes = [];
  for (let p = 0; p < 20; p++) {
    const a = JSON.parse(await get(`https://stock.naver.com/api/domestic/market/theme/list?startIdx=${p}&pageSize=100&sortType=changeRate`));
    themes.push(...a); if (a.length < 100) break;
  }
  const use = themes.filter(t => !/신규상장|스팩/.test(t.name));
  await pool(use, 8, async t => {
    t.members = [];
    for (let p = 0; p < 10; p++) {
      const a = JSON.parse(await get(`https://stock.naver.com/api/domestic/market/theme/${t.no}/stocklist?marketType=ALL&orderType=priceTop&startIdx=${p}&pageSize=100`));
      t.members.push(...a.map(s => s.itemcode)); if (a.length < 100) break;
    }
  });
  return use;
}

async function pool(items, n, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: n }, async () => { while (i < items.length) { const k = i++; await fn(items[k], k); } }));
}

export async function run({ phase = 'close', replay = false } = {}) {
  if (!['preview', 'close'].includes(phase)) throw new Error('phase는 preview 또는 close');
  const started = new Date();
  const ms = JSON.parse(await get('https://stock.naver.com/api/stockSecurity/exchanges/market-status?exchanges=krx'));
  const st = ms.exchanges.find(e => e.exchange === 'krx').statuses.filter(s => s.stockType === 'stock' && ['KOSPI', 'KOSDAQ'].includes(s.marketType));
  const today = st[0].today, latest = st[0].latest;
  const outDir = path.join(root, 'outputs', 'jongga');
  await fs.mkdir(path.join(outDir, 'runs'), { recursive: true });

  let asOf;
  if (replay) asOf = latest.tradeBaseAt;
  else {
    if (!today.isTradingDay) {
      const s = { status: 'skipped', reason: '휴장일', date: today.date, runAtKst: kst() };
      await fs.writeFile(path.join(outDir, 'status.json'), JSON.stringify(s, null, 2));
      console.log('휴장일 — 스캔하지 않음'); return s;
    }
    asOf = today.date;
    if (latest.tradeBaseAt !== asOf) throw new Error('오늘 세션 데이터 미확인');
    const sess = latest.session;
    if (phase === 'preview' && !(sess.marketSessionType === 'regularMarket' && sess.marketStatusDetailType === 'open'))
      throw new Error(`정규장 진행 중이 아님 (${sess.displayLabel}) — 장중 잠정 스캔은 정규장 시간에만`);
    if (phase === 'close' && sess.marketSessionType === 'regularMarket' && sess.marketStatusDetailType !== 'close')
      throw new Error('정규장이 아직 끝나지 않음 — 확정 스캔은 15:30 이후');
  }

  const list = await marketList();
  const excludedReasons = {}, errors = [], rows = [];
  const targets = [];
  for (const s of list) {
    const sf = safety(s);
    if (!sf.ok) { excludedReasons[sf.reason] = (excludedReasons[sf.reason] || 0) + 1; continue; }
    targets.push({ s, caution: sf.caution });
  }
  const start = ymd(new Date(Date.parse(`${asOf}T00:00:00Z`) - 520 * 86400e3).toISOString().slice(0, 10));
  let skippedNoTrade = 0;
  await pool(targets, 8, async ({ s, caution }) => {
    try {
      let bars = parseSise(await get(`https://api.finance.naver.com/siseJson.naver?symbol=${s.itemcode}&requestType=1&startTime=${start}&endTime=${ymd(asOf)}&timeframe=day`));
      bars = bars.filter(b => b.open > 0 && b.close > 0); // 거래 없던 날(정지 등) 제외
      if (phase === 'preview') {
        // 오늘 봉은 시세 목록의 현재가로 구성 (장중 잠정)
        bars = bars.filter(b => b.date < asOf);
        const o = +s.openPrice, h = +s.highPrice, l = +s.lowPrice, c = +s.nowPrice;
        if (!(o > 0 && c > 0)) { skippedNoTrade++; return; }
        bars.push({ date: asOf, open: o, high: h, low: l, close: c, volume: +s.tradeVolume });
      } else if (bars.at(-1)?.date !== asOf) { skippedNoTrade++; return; }
      const base = { code: s.itemcode, name: s.itemname, market: s.market, turnover: +s.tradeAmount, caution, listedDate: s.listedDate };
      const ipo = evaluateIpo(bars, s.listedDate);
      const ev = evaluate(bars);
      const t = bars.length - 1;
      const r5 = t >= 5 ? bars[t].close / bars[t - 5].close - 1 : null;
      const r20 = t >= 20 ? bars[t].close / bars[t - 20].close - 1 : null;
      let recentRef = false;
      for (let k = Math.max(1, t - 4); k <= t; k++) if (referenceOf(bars[k], bars[k - 1])) { recentRef = true; break; }
      rows.push({ ...base, ...ev, ipo, r5, r20, recentRef });
    } catch (e) { errors.push(`${s.itemcode} ${s.itemname}: ${e.message}`); }
  });

  // 섹터(테마) 동향: 오늘 등락·상승비율 + 구성종목 5일/20일 수익률 중간값 + 최근 5일 기준봉 수
  let sectors = [], themeError = null;
  try {
    const themes = await themeData();
    const byCode = new Map(rows.map(x => [x.code, x]));
    sectors = themes.map(th => sectorStats(th, th.members.map(c => byCode.get(c)).filter(Boolean)));
    const memberOf = new Map();
    for (const sc of sectors) for (const c of sc.members) (memberOf.get(c) ?? memberOf.set(c, []).get(c)).push(sc);
    for (const x of rows) {
      const list = (memberOf.get(x.code) || []).sort((a, b) => GRADE_ORDER[a.grade] - GRADE_ORDER[b.grade] || b.med5 - a.med5);
      x.sectors = list.slice(0, 3).map(s => ({ name: s.name, grade: s.grade, today: s.today, med5: s.med5 }));
      x.sectorGrade = list[0]?.grade ?? '테마 없음';
    }
  } catch (e) { themeError = e.message; for (const x of rows) { x.sectors = []; x.sectorGrade = '미확인'; } }

  rows.sort((a, b) => (GRADE_ORDER[a.sectorGrade] ?? 9) - (GRADE_ORDER[b.sectorGrade] ?? 9) || b.turnover - a.turnover);
  const groups = { entry: [], above_point: [], waiting_breakout: [], point_today: [], waiting_point: [], exit_today: [], ipo: [], new_reference: [] };
  const counts = { invalidated: 0, no_setup: 0 };
  for (const x of rows) {
    if (groups[x.status]) groups[x.status].push(x); else counts[x.status]++;
    if (x.ipo) groups.ipo.push(x);
    if (x.todayRef && x.status !== 'entry') groups.new_reference.push(x);
  }
  let watch = [];
  try {
    const wl = JSON.parse(await fs.readFile(path.join(root, 'watchlist.json'), 'utf8'));
    const byCode = new Map(rows.map(x => [x.code, x]));
    const reasonOf = new Map(list.map(s => [s.itemcode, safety(s)]));
    watch = (wl.items || []).map(w => byCode.get(w.code) ?? { code: w.code, name: w.name, reason: reasonOf.get(w.code)?.reason || '목록에 없음/당일 거래 없음' });
  } catch (e) { if (e.code !== 'ENOENT') errors.push(`watchlist.json: ${e.message}`); }
  sectors.sort((a, b) => GRADE_ORDER[a.grade] - GRADE_ORDER[b.grade] || b.score - a.score);
  const result = { ruleVersion: RULES.version, rules: RULES, phase, replay, asOf, runAtKst: kst(started), sectors: sectors.map(({ members, ...s }) => s), themeError, watch,
    finishedAtKst: kst(), universe: list.length, analyzed: rows.length, excludedCount: list.length - targets.length + skippedNoTrade,
    excludedReasons: { ...excludedReasons, ...(skippedNoTrade ? { '당일 거래 없음/일봉 미반영': skippedNoTrade } : {}) },
    errors, counts, groups: Object.fromEntries(Object.entries(groups).map(([k, v]) => [k, v])) };
  const stem = `${asOf}_${phase}${replay ? '_replay' : ''}_${kst(started).replace(/[ :]/g, '')}`;
  const md = markdown(result);
  // 원자료 크기를 줄이기 위해 JSON엔 셋업 있는 종목만 저장
  const slim = { ...result, groups: Object.fromEntries(Object.entries(groups).map(([k, v]) => [k, v.map(({ today, ...x }) => ({ ...x, close: today.close, open: today.open, high: today.high, low: today.low }))])) };
  await fs.writeFile(path.join(outDir, 'runs', `${stem}.json`), JSON.stringify(slim, null, 2));
  await fs.writeFile(path.join(outDir, 'runs', `${stem}.md`), md);
  if (!replay) await fs.writeFile(path.join(outDir, `latest_${phase}.md`), md);
  const status = { status: errors.length > rows.length * 0.05 ? 'partial' : 'ok', phase, replay, asOf, runAtKst: result.runAtKst, finishedAtKst: result.finishedAtKst,
    entries: groups.entry.length, report: path.join(outDir, 'runs', `${stem}.md`), errors: errors.length };
  await fs.writeFile(path.join(outDir, 'status.json'), JSON.stringify(status, null, 2));
  console.log(JSON.stringify(status, null, 2));
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const phase = process.argv[2] || 'close', replay = process.argv.includes('--replay');
  run({ phase, replay }).catch(e => { console.error(`스캔 실패: ${e.message}`); process.exitCode = 1; });
}
