// Combination-lab engine: filter a columnar trade table by any model/condition values, group by up
// to two columns, and compute the platform metrics in the browser. Definitions mirror
// arena/studies.py (_cell_metrics) and are parity-tested against the precomputed study cells.
// Self-contained (no imports) so it runs under `node --test` as well as in the app.

export const TOL = 1e-9;

export interface Table {
  n: number;
  days: string[];
  day: number[];
  ret: number[];
  columns: { key: string; values: string[]; codes: number[] }[];
}

export type Filters = Record<string, number[] | undefined>; // column key -> allowed codes (undefined = all)

export interface LabMetrics {
  n: number; days: number; mean: number | null; median: number | null; accuracy: number | null;
  profit_rate: number | null; profit_rate_01: number | null; profit_factor: number | null; payoff_ratio: number | null;
  sum_pp: number; cvar5: number | null; max_drawdown_pp: number | null; worst_day_pp: number | null;
  sharpe_daily_ann: number | null; sortino_daily_ann: number | null; max_consecutive_losses: number;
  mean_ex_top5: number | null; positive_month_ratio: number | null;
  mean_ci95: [number | null, number | null]; p_value_mean_gt0: number | null;
  curve: [string, number][];
}

export function select(t: Table, filters: Filters): number[] {
  const active = t.columns.filter(c => filters[c.key] && filters[c.key]!.length < c.values.length);
  const allowed = active.map(c => { const s = new Uint8Array(c.values.length); for (const v of filters[c.key]!) s[v] = 1; return s; });
  const out: number[] = [];
  outer: for (let i = 0; i < t.n; i++) {
    for (let k = 0; k < active.length; k++) if (!allowed[k][active[k].codes[i]]) continue outer;
    out.push(i);
  }
  return out;
}

function mulberry32(seed: number) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function quantile(sorted: number[], q: number) {
  const pos = (sorted.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

export function dayBootstrap(dayIdx: number[], rets: number[], nBoot = 1000, seed = 7): { ci: [number | null, number | null]; p: number | null } {
  const sums = new Map<number, number>(), cnts = new Map<number, number>();
  rets.forEach((r, i) => { sums.set(dayIdx[i], (sums.get(dayIdx[i]) ?? 0) + r); cnts.set(dayIdx[i], (cnts.get(dayIdx[i]) ?? 0) + 1); });
  const keys = [...sums.keys()];
  if (keys.length < 2) return { ci: [null, null], p: null };
  const S = keys.map(k => sums.get(k)!), C = keys.map(k => cnts.get(k)!);
  const rnd = mulberry32(seed), means: number[] = [];
  let le0 = 0;
  for (let b = 0; b < nBoot; b++) {
    let s = 0, c = 0;
    for (let j = 0; j < keys.length; j++) { const k = Math.floor(rnd() * keys.length); s += S[k]; c += C[k]; }
    const m = s / c; means.push(m); if (m <= 0) le0++;
  }
  means.sort((a, b) => a - b);
  return { ci: [quantile(means, 0.025), quantile(means, 0.975)], p: le0 / nBoot };
}

export function metrics(t: Table, idx: number[], opts: { bootstrap?: boolean } = {}): LabMetrics {
  const r = idx.map(i => t.ret[i]), d = idx.map(i => t.day[i]);
  const n = r.length;
  const empty: LabMetrics = { n: 0, days: 0, mean: null, median: null, accuracy: null, profit_rate: null, profit_rate_01: null,
    profit_factor: null, payoff_ratio: null, sum_pp: 0, cvar5: null, max_drawdown_pp: null, worst_day_pp: null,
    sharpe_daily_ann: null, sortino_daily_ann: null, max_consecutive_losses: 0, mean_ex_top5: null, positive_month_ratio: null,
    mean_ci95: [null, null], p_value_mean_gt0: null, curve: [] };
  if (!n) return empty;
  let sum = 0, acc = 0, pr = 0, pr01 = 0, wSum = 0, wN = 0, lSum = 0, lN = 0, streak = 0, maxStreak = 0;
  for (const x of r) {
    sum += x;
    if (x >= -TOL) acc++;
    if (x > TOL) { pr++; wSum += x; wN++; }
    if (x > 0.001) pr01++;
    if (x < -TOL) { lSum += x; lN++; streak++; maxStreak = Math.max(maxStreak, streak); } else streak = 0;
  }
  const srt = [...r].sort((a, b) => a - b);
  const median = n % 2 ? srt[(n - 1) / 2] : (srt[n / 2 - 1] + srt[n / 2]) / 2;
  const k = Math.max(1, Math.ceil(0.05 * n));
  // daily unit-stake series on the source calendar between the group's first and last day
  let lo = Infinity, hi = -Infinity;
  const byDay = new Map<number, number>();
  d.forEach((di, i) => { lo = Math.min(lo, di); hi = Math.max(hi, di); byDay.set(di, (byDay.get(di) ?? 0) + r[i] * 100); });
  const daily: number[] = [], curve: [string, number][] = [];
  let cum = 0, peak = 0, mdd = 0, worst = Infinity;
  const months = new Map<string, number>();
  for (let di = lo; di <= hi; di++) {
    const v = byDay.get(di) ?? 0;
    daily.push(v); cum += v; peak = Math.max(peak, cum); mdd = Math.min(mdd, cum - peak); worst = Math.min(worst, v);
    curve.push([t.days[di], Math.round(cum * 1e4) / 1e4]);
    const m = t.days[di].slice(0, 7); months.set(m, (months.get(m) ?? 0) + v);
  }
  const mu = daily.reduce((a, b) => a + b, 0) / daily.length;
  const sd = daily.length > 1 ? Math.sqrt(daily.reduce((a, b) => a + (b - mu) ** 2, 0) / (daily.length - 1)) : 0;
  const downSq = daily.reduce((a, b) => a + (b < 0 ? b * b : 0), 0);
  const dsd = Math.sqrt(downSq / daily.length);
  const traded = [...months.values()].filter(v => Math.abs(v) > 1e-12);
  const boot = opts.bootstrap === false ? { ci: [null, null] as [null, null], p: null } : dayBootstrap(d, r);
  return {
    n, days: byDay.size, mean: sum / n, median, accuracy: acc / n, profit_rate: pr / n, profit_rate_01: pr01 / n,
    profit_factor: lN ? wSum / -lSum : null, payoff_ratio: wN && lN ? (wSum / wN) / (-lSum / lN) : null,
    sum_pp: sum * 100, cvar5: srt.slice(0, k).reduce((a, b) => a + b, 0) / k, max_drawdown_pp: mdd, worst_day_pp: worst,
    sharpe_daily_ann: sd > 0 ? (mu / sd) * Math.sqrt(252) : null, sortino_daily_ann: dsd > 0 ? (mu / dsd) * Math.sqrt(252) : null,
    max_consecutive_losses: maxStreak, mean_ex_top5: n > 10 ? srt.slice(0, -5).reduce((a, b) => a + b, 0) / (n - 5) : null,
    positive_month_ratio: traded.length ? traded.filter(v => v > 0).length / traded.length : null,
    mean_ci95: boot.ci, p_value_mean_gt0: boot.p, curve,
  };
}

/** Share of random same-size draws from `pool` whose mean is below `value` (selection skill check). */
export function randomPercentile(t: Table, pool: number[], n: number, value: number | null, draws = 1000, seed = 11): number | null {
  if (value == null || !n || !pool.length || n >= pool.length) return null;
  const rnd = mulberry32(seed), rets = pool.map(i => t.ret[i]);
  let below = 0;
  const idx = rets.map((_, i) => i);
  for (let b = 0; b < draws; b++) {
    let s = 0;
    for (let j = 0; j < n; j++) { // partial Fisher–Yates
      const k = j + Math.floor(rnd() * (idx.length - j));
      [idx[j], idx[k]] = [idx[k], idx[j]];
      s += rets[idx[j]];
    }
    if (s / n < value) below++;
  }
  return below / draws;
}

export interface Group { key: Record<string, number>; idx: number[] }

export function groupBy(t: Table, idx: number[], keys: string[]): Group[] {
  if (!keys.length) return [{ key: {}, idx }];
  const cols = keys.map(k => t.columns.find(c => c.key === k)!);
  const map = new Map<string, Group>();
  for (const i of idx) {
    const codes = cols.map(c => c.codes[i]);
    const id = codes.join("|");
    let g = map.get(id);
    if (!g) { g = { key: Object.fromEntries(keys.map((k, j) => [k, codes[j]])), idx: [] }; map.set(id, g); }
    g.idx.push(i);
  }
  return [...map.values()];
}
