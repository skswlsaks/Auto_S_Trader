export function demoConfig() {
  return {
    schemaVersion: 1, mode: 'paper', hypothesisOnly: true,
    note: 'SYNTHETIC DEMO ONLY. Numeric assumptions are not user-approved strategy parameters. Sell tax is deliberately zero, not a statement of current tax rates.',
    initialCashKrw: 10000000, maxPositions: 3, maxPositionKrw: 1000000, dailyLossLimitKrw: 100000,
    breakevenTriggerPct: 0.005, breakevenOffsetPct: 0, maxInitialRiskPct: 0.02,
    buyFeeBps: 1.5, sellFeeBps: 1.5, sellTaxBps: 0, slippageBps: 5, fillParticipation: 0.1,
    maxSpreadBps: 30, maxQuoteAgeMs: 3000, pendingTtlMs: 5000, reentryCooldownMs: 30000,
    pattern: { breakoutBps: 5, pullbackBandBps: 30, invalidationBps: 50, reboundBps: 10, trendExitBps: 50, setupTtlMs: 300000 },
    plans: ['AAA001', 'AAA002'].map((symbol, i) => ({ symbol, strategies: ['day'], reason: 'Synthetic demonstration; no security recommendation',
      informationAsOf: '2026-10-29T08:59:00+09:00', validFrom: '2026-10-29T09:00:00+09:00',
      validUntil: '2026-10-29T19:50:00+09:00', exitAt: '2026-10-29T19:55:00+09:00',
      trendline: [{ at: '2026-10-29T08:58:00+09:00', price: 10000 * (i + 1) }, { at: '2026-10-29T08:59:00+09:00', price: 10000 * (i + 1) }] }))
  };
}

export function demoEvents() {
  const rows = [], start = Date.parse('2026-10-29T09:00:00+09:00');
  const add = (second, symbol, type, fields) => {
    const at = new Date(start + second * 1000).toISOString();
    rows.push({ id: `demo-${rows.length + 1}`, type, symbol, at, receivedAt: at, ...fields });
  };
  const quote = (t, symbol, bid, ask, size = 10000) => add(t, symbol, 'quote', { bid, ask, bidSize: size, askSize: size, session: 'regular' });
  const trade = (t, symbol, price) => add(t, symbol, 'trade', { price });
  quote(0, 'AAA001', 9990, 10000);
  trade(1, 'AAA001', 10020); trade(2, 'AAA001', 10005); trade(3, 'AAA001', 10025);
  quote(4, 'AAA001', 10020, 10025); quote(5, 'AAA001', 10100, 10105);
  quote(6, 'AAA001', 10340, 10345);
  trade(7, 'AAA001', 10350); trade(8, 'AAA001', 10300); trade(9, 'AAA001', 10330);
  quote(10, 'AAA001', 10550, 10555); trade(11, 'AAA001', 10240); quote(12, 'AAA001', 10235, 10240);
  quote(60, 'AAA002', 19990, 20000);
  trade(61, 'AAA002', 20030); trade(62, 'AAA002', 20010); trade(63, 'AAA002', 20040);
  quote(64, 'AAA002', 20040, 20050);
  quote(65, 'AAA002', 18900, 18910); // A gap through the stop demonstrates that the -2% target is not guaranteed.
  return rows;
}
