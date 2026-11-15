// Combination search: every model value x up to two condition values, ranked on a selection period,
// corrected for the number of combinations tried (Benjamini-Hochberg over a day-clustered one-sided
// test of mean > 0), then checked on a later test period and optionally on another source.
// Exact twin of arena/search.py (parity fixture: tests/fixtures/search.json). Self-contained.

const TOL = 1e-9;
const SKIP_ALWAYS = new Set(["month"]);

export interface SearchColumn { key: string; role?: string; values: string[]; labels?: string[]; codes: number[] }
export interface SearchTable { n: number; days: string[]; day: number[]; ret: number[]; columns: SearchColumn[] }

export interface SearchStats {
  n: number; days?: number; mean?: number; sum_pp?: number; accuracy?: number; profit_rate_01?: number;
  profit_factor?: number | null; mean_ex_top5?: number | null; p?: number | null; q?: number;
}
export interface SearchRow {
  model: Record<string, string>; model_labels: Record<string, string>; conds: Record<string, string>; cond_labels: Record<string, string>;
  sel: SearchStats; test: SearchStats | null; holdout?: SearchStats | null; aliases: number; robust: boolean;
}
export type Split = { by: "date"; cut: string } | { by: "column"; key: string; select: string[]; test: string[] };
export type SortKey = "sel" | "test" | "worst" | "q";
export interface SearchOptions {
  modelKeys?: string[]; condKeys?: string[]; maxConds?: number; split?: Split; minN?: number; minDays?: number;
  holdout?: SearchTable | null; sort?: SortKey; top?: number; minTestN?: number;
}
export interface SearchResult {
  split: Split & { select_range: [string, string] | null; test_range: [string, string] | null };
  model_keys: string[]; cond_keys: string[]; skipped_columns: Record<string, string>; max_conds: number;
  min_n: number; min_days: number; sort: SortKey; tried: number; distinct: number; significant: number; robust: number; rows: SearchRow[];
}

export function erfc(x: number): number {
  const z = Math.abs(x), t = 1 / (1 + 0.5 * z);
  const r = t * Math.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (-0.18628806 + t * (
    0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))));
  return x >= 0 ? r : 2 - r;
}

function pMeanGt0(ret: number[], day: number[]): number | null {
  const n = ret.length;
  if (n < 2) return null;
  let s = 0;
  for (const x of ret) s += x;
  const m = s / n, e = new Map<number, number>();
  for (let i = 0; i < n; i++) e.set(day[i], (e.get(day[i]) ?? 0) + (ret[i] - m));
  const k = e.size;
  if (k < 2) return null;
  let ss = 0;
  // numpy bincount orders by day; the sum of squares is order-independent up to rounding
  for (const d of [...e.keys()].sort((a, b) => a - b)) ss += e.get(d)! ** 2;
  const v = ss / (n * n) * k / (k - 1);
  if (v <= 0) return m > 0 ? 0 : 1;
  return 0.5 * erfc(m / Math.sqrt(v) / Math.SQRT2);
}

export function bh(ps: number[]): number[] {
  const m = ps.length, order = ps.map((_, i) => i).sort((a, b) => ps[a] - ps[b]);
  const q = new Array(m).fill(1);
  let run = 1;
  for (let rank = m; rank >= 1; rank--) {
    const i = order[rank - 1];
    run = Math.min(run, ps[i] * m / rank);
    q[i] = Math.min(1, run);
  }
  return q;
}

function stats(ret: number[], day: number[], withP = false): SearchStats | null {
  const n = ret.length;
  if (!n) return null;
  let s = 0, w = 0, l = 0, acc = 0, p01 = 0;
  for (const x of ret) { s += x; if (x > TOL) w += x; if (x < -TOL) l += x; if (x >= -TOL) acc++; if (x > 0.001) p01++; }
  const hasLoss = ret.some(x => x < -TOL);
  let ex5: number | null = null;
  if (n > 10) { const srt = [...ret].sort((a, b) => a - b); let t = 0; for (let i = 0; i < n - 5; i++) t += srt[i]; ex5 = t / (n - 5); }
  const out: SearchStats = { n, days: new Set(day).size, mean: s / n, sum_pp: s * 100, accuracy: acc / n, profit_rate_01: p01 / n,
    profit_factor: hasLoss ? w / -l : null, mean_ex_top5: ex5 };
  if (withP) out.p = pMeanGt0(ret, day);
  return out;
}

function timePartition(codes: number[], day: number[]): boolean {
  const span = new Map<number, [number, number]>();
  codes.forEach((c, i) => { const s = span.get(c); if (!s) span.set(c, [day[i], day[i]]); else { s[0] = Math.min(s[0], day[i]); s[1] = Math.max(s[1], day[i]); } });
  const spans = [...span.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return spans.length > 1 && spans.every((s, i) => i === 0 || spans[i - 1][1] < s[0]);
}

export function defaultCut(t: SearchTable): string {
  const used = [...new Set(t.day)].sort((a, b) => a - b);
  return t.days[used[Math.min(used.length - 1, Math.floor((2 * used.length) / 3))]];
}

function combinations<T>(xs: T[], r: number): T[][] {
  if (r === 0) return [[]];
  const out: T[][] = [];
  xs.forEach((x, i) => { for (const rest of combinations(xs.slice(i + 1), r - 1)) out.push([x, ...rest]); });
  return out;
}

const round7 = (x: number) => Math.round(x * 1e7) / 1e7;

export function search(t: SearchTable, o: SearchOptions = {}): SearchResult {
  const cols = new Map(t.columns.map(c => [c.key, c]));
  const maxConds = o.maxConds ?? 2, minN = o.minN ?? 25, minDays = o.minDays ?? 10, sort = o.sort ?? "sel", top = o.top ?? 30, minTestN = o.minTestN ?? 10;
  const split: Split = o.split ?? { by: "date", cut: defaultCut(t) };
  const part = new Uint8Array(t.n);
  if (split.by === "date") {
    let cut = t.days.findIndex(d => d >= split.cut);
    if (cut < 0) cut = t.days.length;
    for (let i = 0; i < t.n; i++) part[i] = t.day[i] < cut ? 1 : 2;
  } else {
    const c = cols.get(split.key)!;
    const sv = new Set(split.select.map(v => c.values.indexOf(v))), tv = new Set(split.test.map(v => c.values.indexOf(v)));
    for (let i = 0; i < t.n; i++) part[i] = sv.has(c.codes[i]) ? 1 : tv.has(c.codes[i]) ? 2 : 0;
  }
  const modelKeys = o.modelKeys ?? t.columns.filter(c => c.role === "model").map(c => c.key);
  const skipped: Record<string, string> = {};
  let condKeys = o.condKeys;
  if (!condKeys) {
    condKeys = [];
    for (const c of t.columns) {
      if (c.role !== "condition" || modelKeys.includes(c.key)) continue;
      if (SKIP_ALWAYS.has(c.key) || (split.by === "column" && c.key === split.key)) skipped[c.key] = "calendar or split column";
      else if (new Set(c.codes).size < 2) skipped[c.key] = "one value only";
      else if (timePartition(c.codes, t.day)) skipped[c.key] = "splits by date";
      else condKeys.push(c.key);
    }
  }
  for (const k of [...modelKeys, ...condKeys]) if (!cols.has(k)) throw new Error(`unknown column '${k}'`);
  const colsets: string[][] = [];
  for (let r = 0; r <= maxConds; r++) colsets.push(...combinations(condKeys, r));

  const groups = new Map<string, { key: number[]; idx: number[] }>();
  const mcols = modelKeys.map(k => cols.get(k)!);
  for (let i = 0; i < t.n; i++) {
    const key = mcols.map(c => c.codes[i]), id = key.join(",");
    let g = groups.get(id);
    if (!g) { g = { key, idx: [] }; groups.set(id, g); }
    g.idx.push(i);
  }
  const ordered = [...groups.values()].sort((a, b) => { for (let j = 0; j < a.key.length; j++) if (a.key[j] !== b.key[j]) return a.key[j] - b.key[j]; return 0; });
  const labelOf = (c: SearchColumn, v: number) => (c.labels ?? c.values)[v];

  let rows: SearchRow[] = [];
  for (const g of ordered) {
    for (const cs of colsets) {
      const ccols = cs.map(k => cols.get(k)!), radix = ccols.map(c => c.values.length);
      const buckets = new Map<number, number[]>(), selCount = new Map<number, number>();
      for (const i of g.idx) {
        let b = 0;
        for (let j = 0; j < ccols.length; j++) b = b * radix[j] + ccols[j].codes[i];
        let arr = buckets.get(b);
        if (!arr) { arr = []; buckets.set(b, arr); }
        arr.push(i);
        if (part[i] === 1) selCount.set(b, (selCount.get(b) ?? 0) + 1);
      }
      for (const b of [...buckets.keys()].sort((x, y) => x - y)) {
        if ((selCount.get(b) ?? 0) < minN) continue;
        const J = buckets.get(b)!, js = J.filter(i => part[i] === 1), jt = J.filter(i => part[i] === 2);
        const s = stats(js.map(i => t.ret[i]), js.map(i => t.day[i]), true)!;
        if ((s.days ?? 0) < minDays) continue;
        const vals: number[] = [];
        let rest = b;
        for (let j = radix.length - 1; j >= 0; j--) { vals.unshift(rest % radix[j]); rest = Math.floor(rest / radix[j]); }
        rows.push({
          model: Object.fromEntries(mcols.map((c, j) => [c.key, c.values[g.key[j]]])),
          model_labels: Object.fromEntries(mcols.map((c, j) => [c.key, labelOf(c, g.key[j])])),
          conds: Object.fromEntries(ccols.map((c, j) => [c.key, c.values[vals[j]]])),
          cond_labels: Object.fromEntries(ccols.map((c, j) => [c.key, labelOf(c, vals[j])])),
          sel: s, test: stats(jt.map(i => t.ret[i]), jt.map(i => t.day[i])), aliases: 0, robust: false,
        });
      }
    }
  }
  const tried = rows.length;
  rows = rows.map((r, i) => ({ r, i })).sort((a, b) => Object.keys(a.r.conds).length - Object.keys(b.r.conds).length || a.i - b.i).map(x => x.r);
  const seen = new Map<string, SearchRow>(), distinct: SearchRow[] = [];
  for (const r of rows) {
    const te = r.test ?? { n: 0, sum_pp: 0 };
    const key = [r.sel.n, round7(r.sel.sum_pp!), te.n, round7(te.sum_pp ?? 0), r.sel.days].join("|");
    const prev = seen.get(key);
    if (prev) { prev.aliases++; continue; }
    seen.set(key, r);
    distinct.push(r);
  }
  const q = bh(distinct.map(r => r.sel.p ?? 1));
  distinct.forEach((r, i) => { r.sel.q = q[i]; r.robust = q[i] < 0.05 && !!r.test && r.test.n >= minTestN && r.test.mean! > 0; });
  const testMean = (r: SearchRow) => (r.test && r.test.n >= minTestN ? r.test.mean! : -Infinity);
  const score = (r: SearchRow) => sort === "sel" ? -r.sel.mean! : sort === "test" ? -testMean(r) : sort === "worst" ? -Math.min(r.sel.mean!, testMean(r)) : r.sel.q!;
  const ranked = distinct.map((r, i) => ({ r, i, s: score(r) }))
    .sort((a, b) => a.s - b.s || b.r.sel.n - a.r.sel.n || a.i - b.i).map(x => x.r);
  const outRows = ranked.slice(0, top);
  if (o.holdout) for (const r of outRows) r.holdout = match(o.holdout, { ...r.model, ...r.conds });
  const range = (p: number): [string, string] | null => {
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < t.n; i++) if (part[i] === p) { lo = Math.min(lo, t.day[i]); hi = Math.max(hi, t.day[i]); }
    return lo === Infinity ? null : [t.days[lo], t.days[hi]];
  };
  return {
    split: { ...split, select_range: range(1), test_range: range(2) }, model_keys: modelKeys, cond_keys: condKeys, skipped_columns: skipped,
    max_conds: maxConds, min_n: minN, min_days: minDays, sort, tried, distinct: distinct.length,
    significant: distinct.filter(r => r.sel.q! < 0.05).length, robust: distinct.filter(r => r.robust).length, rows: outRows,
  };
}

/** Same model/condition values on another source; null when a column is missing there. */
export function match(t: SearchTable, values: Record<string, string>): SearchStats | null {
  const cols = new Map(t.columns.map(c => [c.key, c]));
  const want: [number[], number][] = [];
  for (const [k, v] of Object.entries(values)) {
    const c = cols.get(k);
    if (!c) return null;
    const code = c.values.indexOf(v);
    if (code < 0) return { n: 0 };
    want.push([c.codes, code]);
  }
  const ret: number[] = [], day: number[] = [];
  for (let i = 0; i < t.n; i++) if (want.every(([codes, v]) => codes[i] === v)) { ret.push(t.ret[i]); day.push(t.day[i]); }
  return stats(ret, day) ?? { n: 0 };
}
