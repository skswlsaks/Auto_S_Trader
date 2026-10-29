import assert from 'node:assert/strict';
import { reference, analyzeLecture, select, eligibleThemes, session, markdown } from './closing_scan.mjs';

const ref = reference({ date: '2026-10-01', open: 100, close: 105.8, high: 110 }, { close: 100 });
assert.equal(ref.type, '양봉'); assert.equal(ref.level, 105.8);
assert(reference({ open: 110, close: 106, high: 112 }, { close: 100 })?.type === '음봉');
assert.equal(reference({ open: 100, close: 105.7, high: 111 }, { close: 100 }), null);
assert.equal(reference({ open: 100, close: 100, high: 120 }, { close: 100 }), null);
const stock = { itemcode: '0010S0', itemname: '테스트', sosok: '1', tradeStopYn: 'N', manageStatusGb: '0',
  tradableStatus: 'tradable', marketAlertType: '00', tradeAmount: '20000000000', themes: ['A'], listedDate: '20000101' };
const bars = Array.from({ length: 30 }, (_, i) => ({ date: `2026-09-${String(i + 1).padStart(2, '0')}`, open: 99, high: 101, low: 98, close: 100, volume: 1000 }));
Object.assign(bars.at(-4), { open: 100, close: 110, high: 112, low: 99 });
Object.assign(bars.at(-3), { open: 110, close: 112, high: 114, low: 109 });
Object.assign(bars.at(-2), { open: 111, close: 111, high: 113, low: 110 });
Object.assign(bars.at(-1), { open: 112, close: 114, high: 115, low: 111 });
const dates = { asOf: bars.at(-1).date, phase: 'close' };
const a = analyzeLecture(stock, bars, dates);
assert.equal(a.priority, 0); assert.equal(a.reference.level, 110); assert.equal(a.point.level, 112);
assert.equal(a.reference.date, bars.at(-4).date); assert(a.reference.secretUpper > a.reference.secretLower);
assert(!analyzeLecture(stock, bars.slice(0, -1), dates).code);
assert(!analyzeLecture({ ...stock, marketAlertType: null }, bars, dates).code);
assert(!analyzeLecture({ ...stock, tradeAmount: '' }, bars, dates).code);
assert(!analyzeLecture(stock, bars, { ...dates, phase: 'preview' }).code);
assert(analyzeLecture(stock, bars, { ...dates, phase: 'preview' }).dataIssue);
assert.equal(analyzeLecture({ ...stock, nowPrice: '114', marketStatus: 'OPEN', tradingSessionType: 'REGULAR_MARKET' }, bars, { ...dates, phase: 'preview' }).priority, 0);
const equal = structuredClone(bars); equal.at(-1).close = 112;
assert.notEqual(analyzeLecture(stock, equal, dates).priority, 0);
const held = structuredClone(bars); Object.assign(held.at(-2), { close: 113, high: 114 });
assert(analyzeLecture(stock, held, dates).setup.includes('신규 돌파 아님'));
const failed = structuredClone(bars); Object.assign(failed.at(-2), { close: 90, low: 89 });
assert.notEqual(analyzeLecture(stock, failed, dates).priority, 0);
const bad = structuredClone(bars); bad.at(-1).volume = 0;
assert(!analyzeLecture(stock, bad, dates).code);
assert(analyzeLecture(stock, bad, dates).dataIssue);
const below = structuredClone(bars); Object.assign(below.at(-1), { close: 80, low: 79 });
assert(analyzeLecture(stock, below, dates).reason.includes('20일선 아래'));
const secretBreak = structuredClone(bars); Object.assign(secretBreak.at(-2), { close: 105, low: 104 });
assert.notEqual(analyzeLecture(stock, secretBreak, dates).priority, 0);

// A newer reference supersedes an older surviving reference, even when it fails later.
const latestFailed = structuredClone(bars);
Object.assign(latestFailed.at(-3), { open: 112, high: 125, low: 111, close: 121 });
Object.assign(latestFailed.at(-2), { open: 120, high: 121, low: 109, close: 111 });
const rejectedLatest = analyzeLecture(stock, latestFailed, dates);
assert(rejectedLatest.reason, 'must reject the latest invalidated reference, not revive the older one');
assert.equal(rejectedLatest.reference.date, latestFailed.at(-3).date);
assert.equal(rejectedLatest.invalidation.date, latestFailed.at(-2).date);
assert.equal(rejectedLatest.invalidation.type, 'secret');

// A close back above the failed secret line without a new reference is not re-entry.
const recovered = structuredClone(latestFailed);
Object.assign(recovered.at(-2), { open: 120, high: 121, low: 116, close: 117 });
Object.assign(recovered.at(-1), { open: 118, high: 122, low: 117, close: 120 });
assert(analyzeLecture(stock, recovered, dates).reason);

const newestToday = structuredClone(bars);
Object.assign(newestToday.at(-1), { open: 113, high: 122, low: 112, close: 120 });
const closedToday = analyzeLecture(stock, newestToday, dates);
assert.equal(closedToday.reference.date, dates.asOf);
assert.equal(closedToday.point, null);
assert(closedToday.setup.includes('당일 새 기준봉'));
const provisionalToday = analyzeLecture({ ...stock, nowPrice: '120', marketStatus: 'OPEN', tradingSessionType: 'REGULAR_MARKET' }, newestToday, { ...dates, phase: 'preview' });
assert.equal(provisionalToday.reference.date, bars.at(-4).date);
assert.equal(provisionalToday.pendingReference.date, dates.asOf);

// An actual NEW reference can restart observation; the obsolete one stays obsolete.
const newAfterFailure = structuredClone(latestFailed);
Object.assign(newAfterFailure.at(-1), { open: 114, high: 130, low: 113, close: 125 });
assert.equal(analyzeLecture(stock, newAfterFailure, dates).reference.date, dates.asOf);

// The latest bearish reference has no defined secret line: don't borrow an old bullish one.
const bearishLatest = structuredClone(bars);
Object.assign(bearishLatest.at(-3), { open: 125, high: 130, low: 112, close: 113 });
const bearishResult = analyzeLecture(stock, bearishLatest, dates);
assert.equal(bearishResult.reference.date, bearishLatest.at(-3).date);
assert.equal(bearishResult.reference.type, '음봉');
assert.equal(bearishResult.reference.secretUpper, null);

// Latest-reference invalidation also blocks MA recovery/240-day auxiliary fallbacks.
const maFailed = structuredClone(latestFailed);
Object.assign(maFailed.at(-2), { open: 120, high: 121, low: 89, close: 90 });
Object.assign(maFailed.at(-1), { open: 91, high: 114, low: 90, close: 113 });
// Exclude the unconfirmed last candle from reference creation in preview mode.
const maRejected = analyzeLecture({ ...stock, nowPrice: '113', marketStatus: 'OPEN', tradingSessionType: 'REGULAR_MARKET' }, maFailed, { ...dates, phase: 'preview' });
assert(maRejected.reason);
assert.equal(maRejected.reference.date, latestFailed.at(-3).date);

// Real failure case: Isu Petasys 2026-10-06 reference / 10-07 invalidation.
const isu = Array.from({ length: 24 }, (_, i) => ({ date: `2026-09-${String(i + 1).padStart(2, '0')}`, open: 114000, high: 116000, low: 113000, close: 115000, volume: 1000 }));
isu.push(
  { date: '2026-09-30', open: 118200, high: 118900, low: 114100, close: 115400, volume: 580461 },
  { date: '2026-10-01', open: 115300, high: 123500, low: 113400, close: 122800, volume: 905772 },
  { date: '2026-10-02', open: 122800, high: 123700, low: 119000, close: 122000, volume: 472175 },
  { date: '2026-10-06', open: 127500, high: 134800, low: 126900, close: 132200, volume: 1448264 },
  { date: '2026-10-07', open: 131100, high: 131200, low: 121000, close: 121300, volume: 1381280 },
  { date: '2026-10-08', open: 122600, high: 129300, low: 119800, close: 125000, volume: 1637833 });
const isuResult = analyzeLecture({ ...stock, itemcode: '007660', itemname: '이수페타시스' }, isu, { asOf: '2026-10-08', phase: 'close' });
assert(isuResult.reason);
assert.equal(isuResult.reference.date, '2026-10-06');
assert.equal(isuResult.invalidation.date, '2026-10-07');
assert.equal(isuResult.invalidation.close, 121300);
assert.equal(isuResult.invalidation.level, 132200 - (132200 - 127500) / 3);
const invalidationReport = markdown({ asOf: '2026-10-08', phase: 'close', generatedAt: 'test', selected: [], errors: [], sources: [], stockCount: 1,
  excluded: [{ code: '007660', name: '이수페타시스', ...isuResult }] });
assert(invalidationReport.includes('이수페타시스 007660') && invalidationReport.includes('2026-10-06') && invalidationReport.includes('2026-10-07'));

// Duksan Hi-Metal: 09-29 supersedes 09-21, even after a subsequent recovery.
const duksan = Array.from({ length: 24 }, (_, i) => ({ date: `2026-08-${String(i + 1).padStart(2, '0')}`, open: 11400, high: 11600, low: 11300, close: 11500, volume: 1000 }));
duksan.push(
  { date: '2026-09-18', open: 11450, high: 12240, low: 11290, close: 11550, volume: 1171491 },
  { date: '2026-09-21', open: 11610, high: 12590, low: 11470, close: 12370, volume: 1247750 },
  { date: '2026-09-22', open: 12600, high: 12930, low: 12330, close: 12930, volume: 1039077 },
  { date: '2026-09-23', open: 13040, high: 13460, low: 12800, close: 13200, volume: 713342 },
  { date: '2026-09-28', open: 13200, high: 13600, low: 12680, close: 12700, volume: 589232 },
  { date: '2026-09-29', open: 12690, high: 14260, low: 12680, close: 14240, volume: 1398045 },
  { date: '2026-09-30', open: 14240, high: 14240, low: 12800, close: 13470, volume: 1739517 },
  { date: '2026-10-01', open: 13350, high: 14150, low: 13200, close: 14090, volume: 1068156 },
  { date: '2026-10-02', open: 14090, high: 14580, low: 13820, close: 14110, volume: 795611 },
  { date: '2026-10-06', open: 14440, high: 14940, low: 14190, close: 14500, volume: 890472 },
  { date: '2026-10-07', open: 14490, high: 14490, low: 13540, close: 13720, volume: 809978 },
  { date: '2026-10-08', open: 13770, high: 14550, low: 13300, close: 14110, volume: 787443 });
const duksanResult = analyzeLecture({ ...stock, itemcode: '077360', itemname: '덕산하이메탈' }, duksan, { asOf: '2026-10-08', phase: 'close' });
assert.equal(duksanResult.reference.date, '2026-09-29');
assert.equal(duksanResult.reference.level, 14240);
assert.equal(duksanResult.invalidation.date, '2026-09-30');
assert.equal(duksanResult.invalidation.close, 13470);
assert.equal(duksanResult.invalidation.level, 14240 - (14240 - 12690) / 3);
assert(13470 > duksanResult.reference.secretLower, 'the lower-third interpretation differs; do not claim both lines failed');

const maOnlyFailed = bars.map(b => ({ ...b, open: 199, high: 201, low: 198, close: 200 }));
Object.assign(maOnlyFailed.at(-3), { open: 240, high: 250, low: 210, close: 218 });
Object.assign(maOnlyFailed.at(-2), { open: 205, high: 206, low: 189, close: 190 });
Object.assign(maOnlyFailed.at(-1), { open: 198, high: 202, low: 197, close: 201 });
const maOnlyResult = analyzeLecture(stock, maOnlyFailed, dates);
assert.equal(maOnlyResult.reference.date, maOnlyFailed.at(-3).date);
assert.equal(maOnlyResult.invalidation.type, 'ma20');
assert.equal(maOnlyResult.invalidation.date, maOnlyFailed.at(-2).date);
const ipoBars = bars.slice(0, 5);
assert(!analyzeLecture({ ...stock, listedDate: '20000101' }, ipoBars, { ...dates, asOf: ipoBars.at(-1).date }).code);
assert(analyzeLecture({ ...stock, listedDate: ipoBars[0].date.replaceAll('-', '') }, [ipoBars[0]], { ...dates, asOf: ipoBars[0].date }).setup.includes('1거래일'));
const ipo5 = structuredClone(ipoBars); Object.assign(ipo5.at(-1), { close: 101, high: 102 });
assert(analyzeLecture({ ...stock, listedDate: ipo5[0].date.replaceAll('-', '') }, ipo5, { ...dates, asOf: ipo5.at(-1).date }).setup.includes('5거래일'));
assert.equal(select([a, a, { ...a, code: '000002' }, { ...a, code: '000003' }]).length, 3);
const many = Array.from({ length: 20 }, (_, i) => ({ ...a, code: String(i).padStart(6, '0'), themes: ['same theme'], turnover: i + 1 }));
assert.equal(select(many).length, 20);
assert.equal(select(many)[0].code, '000019');
const themes = Array.from({ length: 15 }, (_, i) => ({ no: String(i), name: `theme${i}`, totalCnt: '10', riseCnt: '7', changeRate: '3' }));
assert.equal(eligibleThemes(themes).length, 15);
assert.equal(eligibleThemes([...themes, { ...themes[0], name: '스팩', changeRate: '20' }, { ...themes[0], changeRate: '1' }]).length, 15);
const calendar = (date, trading, detail = 'open', type = 'regularMarket') => ({ serverTime: `${date}T15:05:00+09:00`, exchanges: [{ exchange: 'krx', statuses: ['KOSPI', 'KOSDAQ'].map(marketType => ({ marketType, stockType: 'stock', today: { date, isTradingDay: trading }, latest: { tradeBaseAt: trading ? date : '2026-10-08', session: { marketStatusDetailType: detail, marketSessionType: type } } })) }] });
const live = calendar('2026-10-12', true);
assert.equal(session(live, 'preview', new Date(live.serverTime)).asOf, '2026-10-12');
assert.throws(() => session(live, 'close', new Date(live.serverTime)));
assert.throws(() => session(live, 'preview', new Date('2026-10-12T15:08:00+09:00')));
const closed = calendar('2026-10-12', true, 'open', 'afterMarket'); closed.serverTime = '2026-10-12T16:10:00+09:00';
assert.equal(session(closed, 'close', new Date(closed.serverTime)).phase, 'close');
const holiday = calendar('2026-10-11', false, 'close', 'afterMarket');
assert.equal(session(holiday, 'preview', new Date(holiday.serverTime)).skipped, '휴장일');
assert.equal(session(holiday, 'close', new Date(holiday.serverTime), true).asOf, '2026-10-08');
assert.throws(() => session(holiday, 'preview', new Date(holiday.serverTime), true));
const special = structuredClone(closed); special.exchanges[0].statuses[0].latest.session.marketSessionType = 'regularMarket';
assert.throws(() => session(special, 'close', new Date(special.serverTime)));
const inconsistent = structuredClone(live); inconsistent.exchanges[0].statuses[0].latest.tradeBaseAt = '2026-10-08';
assert.throws(() => session(inconsistent, 'preview', new Date(inconsistent.serverTime)));
assert(markdown({ ...dates, generatedAt: 'test', selected: [a], errors: [], sources: [], stockCount: 1, replay: true }).includes('오늘 신호 아님'));
console.log('closing scanner checks passed');
