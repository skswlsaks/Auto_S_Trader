import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

export const VERSION = 'paper-v0.1';
export const TAKE_PROFITS = Object.freeze([{ gain: 0.03, cumulative: 0.35 }, { gain: 0.05, cumulative: 0.70 }]);
const number = (n, min, max, name) => assert(typeof n === 'number' && Number.isFinite(n) && n >= min && n <= max, `Invalid ${name}`);
export const time = s => {
  assert(typeof s === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(s) && Number.isFinite(Date.parse(s)), 'Timestamp must include timezone');
  return Date.parse(s);
};
export const kstDate = s => new Date(time(s) + 9 * 3600000).toISOString().slice(0, 10);
export const fingerprint = c => createHash('sha256').update(JSON.stringify(c)).digest('hex');

export function validateConfig(c) {
  assert(c.mode === 'paper' && c.schemaVersion === 1 && c.hypothesisOnly === true, 'Only explicitly unvalidated paper configurations are supported');
  number(c.initialCashKrw, 1, 1e12, 'initialCashKrw');
  number(c.maxPositions, 1, 3, 'maxPositions'); assert(Number.isInteger(c.maxPositions));
  number(c.maxPositionKrw, 1, c.initialCashKrw, 'maxPositionKrw');
  number(c.dailyLossLimitKrw, 1, c.initialCashKrw, 'dailyLossLimitKrw');
  number(c.breakevenTriggerPct, 0.00001, 0.1, 'breakevenTriggerPct');
  number(c.breakevenOffsetPct, 0, c.breakevenTriggerPct, 'breakevenOffsetPct');
  number(c.maxInitialRiskPct, 0.00001, 0.02, 'maxInitialRiskPct');
  for (const k of ['buyFeeBps', 'sellFeeBps', 'sellTaxBps', 'slippageBps']) number(c[k], 0, 1000, k);
  number(c.fillParticipation, 0.00001, 1, 'fillParticipation');
  number(c.maxSpreadBps, 0, 1000, 'maxSpreadBps');
  for (const k of ['maxQuoteAgeMs', 'pendingTtlMs']) number(c[k], 1, 60000, k);
  number(c.reentryCooldownMs, 0, 86400000, 'reentryCooldownMs');
  for (const k of ['breakoutBps', 'pullbackBandBps', 'invalidationBps', 'reboundBps', 'trendExitBps']) number(c.pattern[k], 0.01, 1000, k);
  number(c.pattern.setupTtlMs, 1, 3600000, 'setupTtlMs');
  assert(Array.isArray(c.plans) && c.plans.length <= 40, 'At most 40 watch candidates');
  const seen = new Set();
  for (const p of c.plans) {
    assert(/^[0-9A-Z]{6}$/.test(p.symbol) && !seen.has(p.symbol), 'Invalid/duplicate stock symbol'); seen.add(p.symbol);
    assert(Array.isArray(p.strategies) && p.strategies.length && p.strategies.every(s => ['day', 'closing'].includes(s)), 'Invalid strategies');
    assert(time(p.validFrom) < time(p.validUntil) && time(p.validUntil) <= time(p.exitAt), 'Invalid plan validity/exit deadline');
    assert(time(p.informationAsOf) <= time(p.validFrom), 'Plan uses future information');
    assert(typeof p.reason === 'string' && p.reason.length, 'Plan rationale required');
    if (p.strategies.includes('day')) {
      assert(p.trendline?.length === 2, 'Two previously known trendline anchors required');
      const [a, b] = p.trendline;
      assert(time(a.at) < time(b.at) && time(b.at) <= time(p.validFrom), 'Trendline anchors must precede plan validity');
      number(a.price, 1, 1e9, 'anchor price'); number(b.price, 1, 1e9, 'anchor price');
    }
  }
  return c;
}

export function initialState(c) {
  validateConfig(c);
  return { version: VERSION, configHash: fingerprint(c), cash: c.initialCashKrw, positions: {}, pending: {}, setups: {},
    quotes: {}, watermarks: {}, completed: {}, paused: false, feedReady: true, day: null, dayStartEquity: c.initialCashKrw,
    dayHalted: false, fillCount: 0, realizedNet: 0, fees: 0, taxes: 0 };
}

export function equity(s) {
  return s.cash + Object.values(s.positions).reduce((v, p) => v + p.quantity * (s.quotes[p.symbol]?.bid ?? p.entryPrice), 0);
}

export function summary(s, c) {
  return { mode: 'paper', version: VERSION, hypothesisOnly: true, model: 'rule-baseline / no trained model',
    cashKrw: s.cash, equityKrw: equity(s), netPnlKrw: equity(s) - c.initialCashKrw, realizedNetKrw: s.realizedNet,
    feesKrw: s.fees, taxesKrw: s.taxes, fills: s.fillCount, paused: s.paused, feedReady: s.feedReady,
    dayHalted: s.dayHalted, positions: Object.values(s.positions), pendingEntries: Object.values(s.pending),
    costAssumptions: { buyFeeBps: c.buyFeeBps, sellFeeBps: c.sellFeeBps, sellTaxBps: c.sellTaxBps, slippageBps: c.slippageBps },
    warning: 'Simulated fills at next book update; not an execution guarantee or validated strategy performance.' };
}

function lineAt(p, at) {
  const [a, b] = p.trendline;
  return b.price + (b.price - a.price) * (time(at) - time(b.at)) / (time(b.at) - time(a.at));
}

function pattern(s, c, p, e, emit) {
  if (!p?.strategies.includes('day') || time(e.at) < time(p.validFrom) || time(e.at) >= time(p.validUntil)) return;
  const line = lineAt(p, e.at);
  if (!(line > 0)) return;
  let setup = s.setups[e.symbol];
  if (setup && time(e.at) - time(setup.startedAt) > c.pattern.setupTtlMs) { delete s.setups[e.symbol]; setup = null; }
  const f = c.pattern;
  if (e.price < line * (1 - f.invalidationBps / 10000)) { delete s.setups[e.symbol]; return; }
  if (!setup) {
    if (e.price > line * (1 + f.breakoutBps / 10000)) {
      s.setups[e.symbol] = { stage: 'breakout', startedAt: e.at, last: e.price, low: e.price };
      emit('PATTERN', { symbol: e.symbol, stage: 'breakout', line });
    }
    return;
  }
  if (setup.stage === 'breakout' && e.price < setup.last && e.price <= line * (1 + f.pullbackBandBps / 10000)) {
    setup.stage = 'pullback'; setup.low = e.price;
    emit('PATTERN', { symbol: e.symbol, stage: 'pullback', line });
  } else if (setup.stage === 'pullback') {
    setup.low = Math.min(setup.low, e.price);
    if (e.price > setup.last && e.price >= setup.low * (1 + f.reboundBps / 10000) && e.price > line) {
      // shortcut: Explicit anchor-based baseline, replace only after labeled causal pattern evaluation.
      requestEntry(s, c, p, { ...e, strategy: 'day', initialStop: setup.low * (1 - f.invalidationBps / 10000) }, emit);
      delete s.setups[e.symbol];
      return;
    }
  }
  setup.last = e.price;
}

function requestEntry(s, c, p, e, emit) {
  const reject = reason => emit('ENTRY_REJECTED', { symbol: e.symbol, reason });
  if (!p || !p.strategies.includes(e.strategy)) return reject('not_in_plan');
  if (time(e.at) < time(p.validFrom) || time(e.at) >= time(p.validUntil)) return reject('plan_expired');
  if (s.paused || s.dayHalted || !s.feedReady) return reject('entries_halted');
  if (s.positions[e.symbol] || s.pending[e.symbol]) return reject('already_traded_or_reserved');
  if (s.completed[e.symbol] && time(e.at) - time(s.completed[e.symbol]) < c.reentryCooldownMs) return reject('reentry_cooldown');
  if (Object.keys(s.positions).length + Object.keys(s.pending).length >= c.maxPositions) return reject('max_positions');
  if (!(typeof e.initialStop === 'number' && Number.isFinite(e.initialStop) && e.initialStop > 0)) return reject('initial_stop_required');
  s.pending[e.symbol] = { symbol: e.symbol, strategy: e.strategy, initialStop: e.initialStop, signalAt: e.at,
    expiresAt: Math.min(time(e.at) + c.pendingTtlMs, time(p.validUntil)), exitAt: p.exitAt };
  emit('ENTRY_QUEUED', { ...s.pending[e.symbol] });
}

function fillEntry(s, c, e, emit) {
  const order = s.pending[e.symbol];
  if (!order) return;
  const reject = reason => { delete s.pending[e.symbol]; emit('ENTRY_REJECTED', { symbol: e.symbol, reason }); };
  if (time(e.at) <= time(order.signalAt)) return;
  if (time(e.at) >= order.expiresAt) return reject('entry_expired');
  if (s.paused || s.dayHalted || !s.feedReady) return reject('entries_halted');
  if (!['regular', 'after'].includes(e.session)) return;
  if ((e.ask / e.bid - 1) * 10000 > c.maxSpreadBps) return reject('spread_too_wide');
  const price = e.ask * (1 + c.slippageBps / 10000), stop = order.initialStop;
  if (stop >= e.bid || (price - stop) / price > c.maxInitialRiskPct + 1e-12) return reject('invalid_initial_risk');
  const quantity = Math.min(Math.floor(c.maxPositionKrw / price), Math.floor(s.cash / (price * (1 + c.buyFeeBps / 10000))), Math.floor(e.askSize * c.fillParticipation));
  if (quantity < 1) return;
  const fee = quantity * price * c.buyFeeBps / 10000;
  s.cash -= quantity * price + fee; s.fees += fee; s.fillCount++;
  s.positions[e.symbol] = { symbol: e.symbol, strategy: order.strategy, quantity, originalQuantity: quantity, soldQuantity: 0,
    entryPrice: price, entryAt: e.at, entryFeePerShare: fee / quantity, stop, initialStop: stop,
    breakevenArmed: false, high: e.bid, lastTrade: price, pullbackLow: null, trendLow: stop,
    exitAt: order.exitAt, forceExit: null, tpStage: 0 };
  delete s.pending[e.symbol];
  emit('FILL', { symbol: e.symbol, side: 'BUY', quantity, price, fee, tax: 0, reason: order.strategy });
  emit('STOP_SET', { symbol: e.symbol, price: stop, reason: 'initial', quantity });
  // The partial fill is IOC-like: the unfilled entry remainder is not carried forward.
}

function sell(s, c, p, e, wanted, reason, liquidity, emit) {
  const quantity = Math.min(wanted, p.quantity, liquidity);
  if (quantity <= 0) return 0;
  const price = e.bid * (1 - c.slippageBps / 10000);
  const fee = price * quantity * c.sellFeeBps / 10000, tax = price * quantity * c.sellTaxBps / 10000;
  s.cash += price * quantity - fee - tax; s.fees += fee; s.taxes += tax; s.fillCount++;
  const realizedNet = quantity * (price - p.entryPrice - p.entryFeePerShare) - fee - tax;
  s.realizedNet += realizedNet; p.quantity -= quantity; p.soldQuantity += quantity;
  emit('FILL', { symbol: p.symbol, side: 'SELL', quantity, price, fee, tax, realizedNet, reason,
    stopReference: p.stop, remainingQuantity: p.quantity });
  if (!p.quantity) {
    s.completed[p.symbol] = e.at; delete s.positions[p.symbol]; delete s.setups[p.symbol];
    emit('POSITION_CLOSED', { symbol: p.symbol, reason });
  }
  return quantity;
}

function protect(s, c, e, emit) {
  const p = s.positions[e.symbol];
  if (!p) return;
  if (time(e.at) >= time(p.exitAt)) p.forceExit ||= 'time_exit';
  if (e.bid <= p.stop) p.forceExit ||= 'stop_loss';
  const liquidity = Math.floor(e.bidSize * c.fillParticipation);
  if (p.forceExit) { sell(s, c, p, e, p.quantity, p.forceExit, liquidity, emit); return; }
  p.high = Math.max(p.high, e.bid);
  const gain = e.bid / p.entryPrice - 1;
  if (!p.breakevenArmed && gain >= c.breakevenTriggerPct - 1e-12) {
    p.breakevenArmed = true; p.stop = Math.max(p.stop, p.entryPrice * (1 + c.breakevenOffsetPct));
    emit('STOP_RAISED', { symbol: p.symbol, price: p.stop, reason: 'breakeven' });
  }
  // Latched thresholds survive partial fills and pullbacks; quantities are cumulative fractions of the original entry.
  for (let i = 0; i < TAKE_PROFITS.length; i++) if (gain >= TAKE_PROFITS[i].gain - 1e-12) p.tpStage = Math.max(p.tpStage, i + 1);
  if (p.tpStage) {
    const target = Math.floor(p.originalQuantity * TAKE_PROFITS[p.tpStage - 1].cumulative + 1e-9);
    sell(s, c, p, e, Math.max(0, target - p.soldQuantity), `take_profit_${p.tpStage}`, liquidity, emit);
  }
}

export function applyEvent(state, c, e) {
  assert(state.version === VERSION && state.configHash === fingerprint(c), 'State/config mismatch');
  assert(e && typeof e.id === 'string' && e.id.length > 0 && e.id.length <= 256, 'Event ID required');
  const at = time(e.at), actions = [], s = structuredClone(state);
  const emit = (type, data = {}) => actions.push({ type, at: e.at, eventId: e.id, ...data });
  assert(['quote', 'trade', 'signal', 'control', 'feed'].includes(e.type), 'Unknown event type');
  if (e.type === 'control') {
    assert(['pause', 'resume', 'flatten'].includes(e.action), 'Unknown control');
    if (e.action === 'resume') s.paused = false;
    else { s.paused = true; s.pending = {}; }
    if (e.action === 'flatten') for (const p of Object.values(s.positions)) p.forceExit ||= 'manual_flatten';
    emit('CONTROL', { action: e.action }); return { state: s, actions };
  }
  if (e.type === 'feed') {
    assert(typeof e.ready === 'boolean'); s.feedReady = e.ready;
    if (!e.ready) { s.pending = {}; s.setups = {}; }
    emit('FEED_STATUS', { ready: e.ready }); return { state: s, actions };
  }
  assert(/^[0-9A-Z]{6}$/.test(e.symbol), 'Invalid symbol');
  if (s.day && kstDate(e.at) < s.day) { emit('DATA_REJECTED', { symbol: e.symbol, reason: 'previous_day' }); return { state: s, actions }; }
  const key = e.symbol;
  if (at < (s.watermarks[key] ?? 0)) { emit('DATA_REJECTED', { symbol: e.symbol, reason: 'out_of_order' }); return { state: s, actions }; }
  if (e.type === 'quote' || e.type === 'trade') {
    const received = time(e.receivedAt);
    if (at > received + 1000 || received - at > c.maxQuoteAgeMs) {
      delete s.pending[e.symbol]; emit('DATA_REJECTED', { symbol: e.symbol, reason: 'stale_or_future' }); return { state: s, actions };
    }
  }
  s.watermarks[key] = at;
  const day = kstDate(e.at);
  if (s.day !== day) { s.day = day; s.dayStartEquity = equity(s); s.dayHalted = false; }
  for (const [code, order] of Object.entries(s.pending)) if (at >= order.expiresAt) {
    delete s.pending[code]; emit('ENTRY_REJECTED', { symbol: code, reason: 'entry_expired' });
  }
  const p = c.plans.find(p => p.symbol === e.symbol);
  if (e.type === 'signal') {
    assert(e.strategy === 'closing', 'External entry signals are only supported for the provisional closing strategy');
    requestEntry(s, c, p, e, emit);
  } else if (e.type === 'trade') {
    number(e.price, 0.000001, 1e9, 'trade price');
    const held = s.positions[e.symbol];
    if (held) {
      // shortcut: A causally confirmed pullback low is the reversal baseline until a labeled model is validated.
      if (e.price < held.trendLow * (1 - c.pattern.trendExitBps / 10000)) held.forceExit ||= 'trend_reversal';
      if (e.price < held.lastTrade) held.pullbackLow = Math.min(held.pullbackLow ?? e.price, e.price);
      else if (held.pullbackLow !== null && e.price >= held.pullbackLow * (1 + c.pattern.reboundBps / 10000)) {
        held.trendLow = Math.max(held.trendLow, held.pullbackLow); held.pullbackLow = null;
      }
      held.lastTrade = e.price;
    } else if (!s.paused && s.feedReady && !s.dayHalted) pattern(s, c, p, e, emit);
  } else {
    for (const k of ['bid', 'ask']) number(e[k], 0.000001, 1e9, k);
    for (const k of ['bidSize', 'askSize']) number(e[k], 0, 1e12, k);
    assert(['regular', 'after', 'closed'].includes(e.session), 'Unknown market session');
    if (e.bid > e.ask) { delete s.pending[e.symbol]; emit('DATA_REJECTED', { symbol: e.symbol, reason: 'crossed_book' }); return { state: s, actions }; }
    s.quotes[e.symbol] = { bid: e.bid, ask: e.ask, at: e.at };
    if (equity(s) <= s.dayStartEquity - c.dailyLossLimitKrw) {
      if (!s.dayHalted) emit('DAILY_ENTRY_HALT');
      s.dayHalted = true; s.pending = {};
    }
    if (['regular', 'after'].includes(e.session)) { protect(s, c, e, emit); fillEntry(s, c, e, emit); }
  }
  return { state: s, actions };
}
