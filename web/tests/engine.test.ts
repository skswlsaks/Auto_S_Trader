// Parity: the in-browser lab engine must reproduce the precomputed study cells (Python) exactly.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { groupBy, metrics, select, type Table } from "../src/lib/engine.ts";

const DATA = new URL("../public/data/", import.meta.url);
const has = existsSync(new URL("lab/index.json", DATA));

test("lab engine matches study cells (entry-gate)", { skip: !has && "built data not present" }, () => {
  const src = JSON.parse(readFileSync(new URL("lab/study__entry-gate-20261114-v1.json", DATA), "utf8")) as Table & { columns: { key: string; values: string[]; codes: number[] }[] };
  const study = JSON.parse(readFileSync(new URL("studies/entry-gate-20261114-v1/study.json", DATA), "utf8"));
  const dims: string[] = study.dimensions.map((d: { key: string }) => d.key);
  const base = study.cells.filter((c: { base: boolean; n: number }) => c.base && c.n >= 5).slice(0, 80);
  let checked = 0;
  for (const cell of base) {
    const filters = Object.fromEntries(dims.map(k => {
      const col = src.columns.find(c => c.key === k)!;
      return [k, [col.values.indexOf(cell.key[k])]];
    }));
    const m = metrics(src, select(src, filters), { bootstrap: false });
    assert.equal(m.n, cell.n);
    assert.equal(m.days, cell.days);
    // returns ship rounded to 1e-8, so %p aggregates over many trades carry ~1e-6 rounding
    for (const [f, tol] of [["mean", 1e-7], ["accuracy", 1e-12], ["profit_rate_01", 1e-12], ["cvar5", 1e-7], ["max_drawdown_pp", 1e-4], ["sum_pp", 1e-4]] as const)
      assert.ok(Math.abs((m[f] as number) - cell[f]) < tol, `${f} ${m[f]} vs ${cell[f]}`);
    if (cell.profit_factor != null) assert.ok(Math.abs((m.profit_factor as number) - cell.profit_factor) < 1e-5);
    if (cell.sharpe_daily_ann != null) assert.ok(Math.abs((m.sharpe_daily_ann as number) - cell.sharpe_daily_ann) < 1e-4, `sharpe ${m.sharpe_daily_ann} ${cell.sharpe_daily_ann}`);
    checked++;
  }
  assert.ok(checked > 50);
});

test("select + groupBy partition the filtered rows", () => {
  const t: Table = { n: 4, days: ["2026-01-05", "2026-01-06"], day: [0, 0, 1, 1], ret: [0.01, -0.02, 0, 0.03],
    columns: [{ key: "m", values: ["a", "b"], codes: [0, 1, 0, 1] }, { key: "c", values: ["x", "y"], codes: [0, 0, 1, 1] }] };
  const idx = select(t, { m: [0] });
  assert.deepEqual(idx, [0, 2]);
  const g = groupBy(t, select(t, {}), ["c"]);
  assert.deepEqual(g.map(x => x.idx), [[0, 1], [2, 3]]);
  const m = metrics(t, [0, 1, 2, 3], { bootstrap: false });
  assert.equal(m.accuracy, 0.75);
  assert.equal(m.max_consecutive_losses, 1);
  assert.ok(Math.abs((m.max_drawdown_pp as number) - -1) < 1e-9);
});
