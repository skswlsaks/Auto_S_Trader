import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { demoConfig, demoEvents } from './demo.mjs';
import { initialState, applyEvent, validateConfig, summary } from './engine.mjs';
import { PaperStore } from './store.mjs';

function harness(overrides = {}) {
  const c = { ...demoConfig(), maxPositionKrw: 10000, buyFeeBps: 0, sellFeeBps: 0, sellTaxBps: 0,
    slippageBps: 0, fillParticipation: 1, ...overrides };
  c.plans = ['AAA001', 'AAA002', 'AAA003', 'AAA004'].map(symbol => ({ ...structuredClone(c.plans[0]), symbol, strategies: ['day', 'closing'] }));
  let state = initialState(c), n = 0;
  const next = fields => { const at = new Date(Date.parse('2026-10-29T09:00:00+09:00') + n++ * 1000).toISOString(); return { id: `test-${n}`, at, receivedAt: at, symbol: 'AAA001', ...fields }; };
  const process = fields => { const e = next(fields), r = applyEvent(state, c, e); state = r.state; return r.actions; };
  return { c, next, process, state: () => state,
    enter(symbol = 'AAA001', initialStop = 98) {
      process({ type: 'signal', symbol, strategy: 'closing', initialStop });
      return process({ type: 'quote', symbol, bid: 100, ask: 100, bidSize: 10000, askSize: 10000, session: 'regular' });
    },
    quote(bid, fields = {}) { return process({ type: 'quote', bid, ask: bid, bidSize: 10000, askSize: 10000, session: 'regular', ...fields }); }
  };
}

test('entry is queued, fills only on the next quote, and starts with a stop', () => {
  const h = harness(); h.process({ type: 'signal', strategy: 'closing', initialStop: 98 });
  assert.equal(Object.keys(h.state().positions).length, 0);
  const actions = h.quote(100);
  assert.equal(h.state().positions.AAA001.stop, 98);
  assert.equal(h.state().positions.AAA001.originalQuantity, 100);
  assert(actions.some(a => a.type === 'STOP_SET'));
});

test('missing initial stop never opens a position', () => {
  const h = harness(); assert.equal(h.process({ type: 'signal', strategy: 'closing' })[0].reason, 'initial_stop_required');
  h.quote(100); assert.deepEqual(h.state().positions, {});
});

test('gap on entry and too-wide initial stop reject entry', () => {
  const h = harness(); h.process({ type: 'signal', strategy: 'closing', initialStop: 98 });
  assert.equal(h.quote(105).at(-1).reason, 'invalid_initial_risk');
  assert.deepEqual(h.state().positions, {});
});

test('pending entries reserve the shared maximum of three symbols', () => {
  const h = harness();
  for (const symbol of ['AAA001', 'AAA002', 'AAA003']) h.process({ type: 'signal', symbol, strategy: 'closing', initialStop: 98 });
  assert.equal(h.process({ type: 'signal', symbol: 'AAA004', strategy: 'closing', initialStop: 98 })[0].reason, 'max_positions');
});

test('day and closing strategies cannot bypass an existing symbol reservation', () => {
  const h = harness(); h.enter();
  assert.equal(h.process({ type: 'signal', strategy: 'closing', initialStop: 98 })[0].reason, 'already_traded_or_reserved');
});

test('a previously closed symbol can reenter after the configured cooldown', () => {
  const h = harness(); h.enter(); h.quote(97);
  assert.equal(h.process({ type: 'signal', strategy: 'closing', initialStop: 98 })[0].reason, 'reentry_cooldown');
  h.process({ type: 'signal', strategy: 'closing', initialStop: 98, at: '2026-10-29T09:00:40+09:00' });
  const actions = h.quote(100, { at: '2026-10-29T09:00:41+09:00', receivedAt: '2026-10-29T09:00:41+09:00' });
  assert(actions.some(a => a.type === 'FILL' && a.side === 'BUY'));
});

test('breakeven is armed before +3% and never lowered on pullback', () => {
  const h = harness(); h.enter(); h.quote(100.6);
  assert.equal(h.state().positions.AAA001.stop, 100);
  h.quote(100.2); assert.equal(h.state().positions.AAA001.stop, 100);
  assert.equal(h.state().positions.AAA001.quantity, 100);
  assert.equal(h.quote(100).find(a => a.type === 'FILL').reason, 'stop_loss');
});

test('+3% and +5% sell 35% of the ORIGINAL shares, leaving 30%', () => {
  const h = harness(); h.enter();
  assert.equal(h.quote(103).find(a => a.type === 'FILL').quantity, 35);
  assert.equal(h.quote(103.5).filter(a => a.type === 'FILL').length, 0);
  assert.equal(h.quote(105).find(a => a.type === 'FILL').quantity, 35);
  assert.equal(h.state().positions.AAA001.quantity, 30);
});

test('jump directly above +5% targets cumulative 70%, with integer rounding', () => {
  const h = harness({ maxPositionKrw: 300 }); h.enter();
  assert.equal(h.quote(106).find(a => a.type === 'FILL').quantity, 2);
  assert.equal(h.state().positions.AAA001.quantity, 1);
});

test('initial partial entry cancels the remainder and bases exits on filled shares', () => {
  const h = harness(); h.process({ type: 'signal', strategy: 'closing', initialStop: 98 });
  h.quote(100, { askSize: 10 });
  assert.equal(h.state().positions.AAA001.originalQuantity, 10);
  assert.deepEqual(h.state().pending, {});
  assert.equal(h.quote(103).find(a => a.type === 'FILL').quantity, 3);
});

test('partial profit fills do not repeat the full target on the next quote', () => {
  const h = harness(); h.enter(); h.quote(103, { bidSize: 10 });
  assert.equal(h.quote(103).find(a => a.type === 'FILL').quantity, 25);
  assert.equal(h.state().positions.AAA001.quantity, 65);
});

test('protective exit has priority over unfinished take-profit targets', () => {
  const h = harness(); h.enter(); h.quote(103, { bidSize: 10 });
  const actions = h.quote(99);
  assert.equal(actions.find(a => a.type === 'FILL').quantity, 90);
  assert.equal(actions.find(a => a.type === 'FILL').reason, 'stop_loss');
});

test('gap can exceed -2%; simulated stop execution uses bid, not stop reference', () => {
  const h = harness(); h.enter();
  const fill = h.quote(95).find(a => a.type === 'FILL');
  assert.equal(fill.price, 95); assert.equal(fill.stopReference, 98); assert.equal(fill.realizedNet, -500);
});

test('unfilled stop exit remains latched until liquidity arrives', () => {
  const h = harness(); h.enter(); h.quote(97, { bidSize: 0 });
  assert.equal(h.state().positions.AAA001.forceExit, 'stop_loss');
  assert.equal(h.quote(101).find(a => a.type === 'FILL').reason, 'stop_loss');
  assert.deepEqual(h.state().positions, {});
});

test('causally confirmed pullback failure exits before +3%, even above breakeven', () => {
  const h = harness(); h.enter(); h.quote(101.5);
  for (const price of [101.5, 101, 101.2, 100.4]) h.process({ type: 'trade', price });
  assert.equal(h.state().positions.AAA001.forceExit, 'trend_reversal');
  const fill = h.quote(100.3).find(a => a.type === 'FILL');
  assert.equal(fill.reason, 'trend_reversal'); assert.equal(fill.quantity, 100);
});

test('paused entries do not disable position protection', () => {
  const h = harness(); h.enter(); h.process({ type: 'control', action: 'pause' });
  assert(h.quote(97).some(a => a.type === 'POSITION_CLOSED'));
});

test('flatten cancels pending buys and exits only on a subsequent executable quote', () => {
  const h = harness(); h.enter();
  h.process({ type: 'signal', symbol: 'AAA002', strategy: 'closing', initialStop: 98 });
  h.process({ type: 'control', action: 'flatten' });
  assert.deepEqual(h.state().pending, {}); assert(h.state().positions.AAA001);
  assert.equal(h.quote(101).find(a => a.type === 'FILL').reason, 'manual_flatten');
});

test('closed session does not invent fills; protection resumes on next tradable quote', () => {
  const h = harness(); h.enter(); assert.equal(h.quote(97, { session: 'closed' }).filter(a => a.type === 'FILL').length, 0);
  assert(h.quote(96, { session: 'after' }).some(a => a.type === 'POSITION_CLOSED'));
});

test('daily loss halts new entries while stops remain active', () => {
  const h = harness({ dailyLossLimitKrw: 1 }); h.enter(); h.quote(99);
  assert.equal(h.state().dayHalted, true);
  assert.equal(h.process({ type: 'signal', symbol: 'AAA002', strategy: 'closing', initialStop: 98 })[0].reason, 'entries_halted');
  assert(h.quote(97).some(a => a.type === 'POSITION_CLOSED'));
});

test('stale, future, crossed and out-of-order market data cannot open trades', () => {
  const h = harness();
  assert.equal(h.quote(100, { receivedAt: '2026-10-29T09:01:00+09:00' })[0].reason, 'stale_or_future');
  assert.equal(h.quote(101, { ask: 100 })[0].reason, 'crossed_book');
  h.quote(100);
  assert.equal(h.process({ type: 'trade', price: 100, at: '2026-10-29T09:00:00+09:00', receivedAt: '2026-10-29T09:00:00+09:00' })[0].reason, 'out_of_order');
});

test('disconnect cancels queued entries and clears incomplete pattern setups', () => {
  const h = harness(); h.process({ type: 'signal', strategy: 'closing', initialStop: 98 });
  h.process({ type: 'feed', ready: false }); assert.deepEqual(h.state().pending, {});
  assert.equal(h.process({ type: 'signal', strategy: 'closing', initialStop: 98 })[0].reason, 'entries_halted');
});

test('cash accounting includes fees, taxes and adverse slippage', () => {
  const h = harness({ buyFeeBps: 10, sellFeeBps: 10, sellTaxBps: 20, slippageBps: 5 }); h.enter('AAA001', 98.1); h.quote(95);
  const r = summary(h.state(), h.c);
  assert(r.feesKrw > 0 && r.taxesKrw > 0);
  assert(Math.abs(r.realizedNetKrw - r.netPnlKrw) < 1e-6);
  assert(r.netPnlKrw < -500);
});

test('future trend anchors, live mode and absent numeric decisions fail closed', () => {
  const c = demoConfig(); c.plans[0].trendline[1].at = '2026-10-29T09:01:00+09:00';
  assert.throws(() => validateConfig(c));
  assert.throws(() => validateConfig({ ...demoConfig(), mode: 'live' }));
  assert.throws(() => validateConfig({ ...demoConfig(), breakevenTriggerPct: null }));
});

test('synthetic scenario covers entry, 35/35 exits, trend exit and gap loss', () => {
  const c = demoConfig(); let s = initialState(c); const actions = [];
  for (const e of demoEvents()) { const r = applyEvent(s, c, e); s = r.state; actions.push(...r.actions); }
  assert(actions.some(a => a.type === 'STOP_RAISED'));
  for (const reason of ['take_profit_1', 'take_profit_2', 'trend_reversal', 'stop_loss']) assert(actions.some(a => a.reason === reason), reason);
  assert.deepEqual(s.positions, {});
});

test('SQLite transactions deduplicate replays, restore stops and roll back invalid events', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 's-trader-test-')), file = path.join(dir, 'paper.sqlite');
  const c = demoConfig(), events = demoEvents(); let db;
  try {
    db = new PaperStore(file, c);
    for (const e of events.slice(0, 7)) db.process(e);
    const before = db.summary(); db.close(); db = new PaperStore(file, c);
    assert.deepEqual(db.summary(), before);
    assert.deepEqual(db.process(events[6]), []);
    assert.throws(() => db.process({ ...events[6], bid: 1 }), /reused/);
    assert.throws(() => db.process({ ...events[7], type: 'unknown' }));
    assert.deepEqual(db.summary(), before);
    for (const e of events.slice(7)) db.process(e);
    const result = db.summary(), count = db.actions().length;
    for (const e of events) db.process(e);
    assert.deepEqual(db.summary(), result); assert.equal(db.actions().length, count);
  } finally { db?.close(); rmSync(dir, { recursive: true, force: true }); }
});

test('SQLite multiple handles share reservations and reject changed configuration', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 's-trader-handles-')), file = path.join(dir, 'paper.sqlite');
  const h = harness(), a = new PaperStore(file, h.c), b = new PaperStore(file, h.c);
  try {
    for (const [i, symbol] of ['AAA001', 'AAA002', 'AAA003', 'AAA004'].entries()) {
      const actions = (i % 2 ? b : a).process(h.next({ type: 'signal', symbol, strategy: 'closing', initialStop: 98 }));
      if (i === 3) assert.equal(actions[0].reason, 'max_positions');
    }
    assert.deepEqual(a.state(), b.state());
    assert.throws(() => new PaperStore(file, { ...h.c, maxPositionKrw: 9000 }), /different configuration/);
  } finally { a.close(); b.close(); rmSync(dir, { recursive: true, force: true }); }
});
