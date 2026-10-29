import assert from 'node:assert/strict';
import { evaluate, evaluateIpo, referenceOf, safety, sectorStats } from './jongga_scan.mjs';

// 날짜 자동 생성 도우미: [open, high, low, close]
function mk(rows, flat = 25, base = 10000) {
  const out = [];
  let d = Date.parse('2026-01-01T00:00:00Z');
  const push = (o, h, l, c) => { out.push({ date: new Date(d).toISOString().slice(0, 10), open: o, high: h, low: l, close: c, volume: 1000 }); d += 86400e3; };
  for (let i = 0; i < flat; i++) push(base, base + 50, base - 50, base);
  for (const [o, h, l, c] of rows) push(o, h, l, c);
  return out;
}
const T = (name, fn) => { fn(); console.log('ok -', name); };

T('시크릿선은 몸통 위에서 1/3', () => {
  const r = referenceOf({ date: 'x', open: 10000, high: 11600, low: 9990, close: 11500 }, { close: 10000 });
  assert.equal(r.type, '양봉'); assert.equal(r.secret, 11000); assert.equal(r.level, 11500);
});

T('+5.8%부터 기준봉, 5.7%는 아님', () => {
  assert.ok(referenceOf({ open: 10000, high: 10600, low: 9990, close: 10580 }, { close: 10000 }));
  assert.equal(referenceOf({ open: 10000, high: 10600, low: 9990, close: 10570 }, { close: 10000 }), null);
});

T('음봉 기준봉: 고가 +12%, 기준가격=시가', () => {
  const r = referenceOf({ open: 11000, high: 11300, low: 10200, close: 10300 }, { close: 10000 });
  assert.equal(r.type, '음봉'); assert.equal(r.level, 11000); assert.equal(r.secret, null);
});

T('포인트 종가 첫 돌파 → entry', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800], [10800, 11000, 10750, 10900]]);
  const e = evaluate(bars);
  assert.equal(e.status, 'entry'); assert.equal(e.point.close, 10800); assert.equal(e.rebreak, false);
});

T('포인트봉은 종가 기준 — 꼬리만 넘은 봉은 포인트 아님', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10650, 10900, 10600, 10650], [10650, 10700, 10600, 10680]]);
  assert.equal(evaluate(bars).status, 'waiting_point');
});

T('오늘 포인트 형성은 진입이 아님', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800]]);
  assert.equal(evaluate(bars).status, 'point_today');
});

T('시크릿 유지 중 재돌파 → entry(재돌파)', () => {
  // 기준봉 10000→10700 (시크릿 10466.7), 포인트 10800, 돌파 10900, 눌림 10600, 재돌파 10850
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800], [10800, 10950, 10750, 10900],
    [10850, 10870, 10550, 10600], [10600, 10900, 10580, 10850]]);
  const e = evaluate(bars);
  assert.equal(e.status, 'entry'); assert.equal(e.rebreak, true);
});

T('돌파 후 유지 중이면 신규 신호 아님', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800], [10800, 10950, 10750, 10900], [10900, 11000, 10850, 10950]]);
  assert.equal(evaluate(bars).status, 'above_point');
});

T('오늘 시크릿 아래 마감 → exit_today', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800], [10700, 10750, 10300, 10400]]);
  const e = evaluate(bars);
  assert.equal(e.status, 'exit_today'); assert.equal(e.broken.line, '시크릿');
});

T('최신 기준봉 이탈 시 옛 기준봉으로 되돌아가지 않음 (이수페타시스 사례)', () => {
  // 옛 기준봉(낮은 시크릿) → 새 기준봉 127,500→132,200 → 다음날 121,300 → 그 다음날 125,000
  const bars = mk([[100000, 107500, 99900, 107000], [107000, 113000, 106000, 112000], [118000, 120000, 117000, 119000],
    [127500, 133000, 127000, 132200], [128000, 128500, 121000, 121300], [121500, 125500, 121000, 125000]], 25, 100000);
  const ref = referenceOf(bars.at(-3), bars.at(-4));
  assert.equal(Math.round(ref.secret), 130633);
  const e = evaluate(bars);
  assert.equal(e.status, 'invalidated'); assert.equal(e.ref.date, bars.at(-3).date); assert.equal(e.broken.close, 121300);
});

T('20일선 아래 마감 → 이탈', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10600, 10650, 9000, 9100], [9100, 9200, 9000, 9150]]);
  assert.equal(evaluate(bars).status, 'invalidated');
});

T('돌파봉이 동시에 새 기준봉이면 표시', () => {
  const bars = mk([[10000, 10750, 9990, 10700], [10700, 10850, 10650, 10800], [10800, 11500, 10750, 11450]]);
  const e = evaluate(bars);
  assert.equal(e.status, 'entry'); assert.ok(e.flags.some(f => f.includes('새 기준봉')));
});

T('신규상장 첫날 양봉 / 5일째 5일선 위 양봉', () => {
  const day1 = [{ date: '2026-10-01', open: 10000, high: 13000, low: 9500, close: 12000 }];
  assert.equal(evaluateIpo(day1, '20261001').day, 1);
  const five = ['01', '02', '05', '06', '07'].map((d, i) => ({ date: `2026-10-${d}`, open: 10000, high: 12000, low: 9000, close: [11000, 10500, 10200, 10000, 11500][i] }));
  five[4].open = 10100;
  assert.equal(evaluateIpo(five, '20261001').day, 5);
  assert.equal(evaluateIpo(five.slice(0, 3), '20261001'), null);
});

T('안전 필터', () => {
  const ok = { itemcode: '005930', itemname: '삼성전자', tradeStopYn: 'N', tradableStatus: 'tradable', manageStatusGb: '0', marketAlertType: '00', type: 'ST' };
  assert.equal(safety(ok).ok, true);
  assert.equal(safety({ ...ok, marketAlertType: '01' }).caution, true);
  assert.equal(safety({ ...ok, marketAlertType: '02' }).ok, false);
  assert.equal(safety({ ...ok, manageStatusGb: '1' }).ok, false);
  assert.equal(safety({ ...ok, itemname: '엔에이치스팩34호' }).ok, false);
  assert.equal(safety({ ...ok, type: 'RT' }).ok, false);
});
T('섹터 등급', () => {
  const m = r5 => ({ code: 'x', name: 'x', r5, r20: 0, recentRef: false, status: 'no_setup' });
  assert.equal(sectorStats({ changeRate: '2.5', totalCnt: '10', riseCnt: '7' }, [m(0), m(0.01)]).grade, '강세');
  assert.equal(sectorStats({ changeRate: '0.5', totalCnt: '10', riseCnt: '6' }, [m(0.04), m(0.05)]).grade, '강세');
  assert.equal(sectorStats({ changeRate: '-0.5', totalCnt: '10', riseCnt: '3' }, [m(-0.02), m(-0.01)]).grade, '약세');
  assert.equal(sectorStats({ changeRate: '5', totalCnt: '3', riseCnt: '3' }, [m(0.1)]).grade, '중립');
});
console.log('all passed');
