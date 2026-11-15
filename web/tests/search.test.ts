// Parity: web/src/lib/search.ts must reproduce arena/search.py on the shared fixture.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { search, type SearchOptions, type SearchTable } from "../src/lib/search.ts";

const fx = JSON.parse(readFileSync(new URL("./fixtures/search.json", import.meta.url), "utf8"));

function close(a: unknown, b: unknown, path: string) {
  if (typeof a === "number" && typeof b === "number") {
    if (a === b) return;
    assert.ok(Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(b)), `${path}: ${a} vs ${b}`);
  } else if (a && b && typeof a === "object") {
    assert.deepEqual(Object.keys(a).sort(), Object.keys(b as object).sort(), `${path}: keys`);
    for (const k of Object.keys(a)) close((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k], `${path}.${k}`);
  } else assert.equal(a, b, path);
}

fx.cases.forEach((c: { options: SearchOptions & { holdout?: boolean }; result: unknown }, i: number) => {
  test(`search parity case ${i}`, () => {
    const { holdout, ...opts } = c.options;
    const got = search(fx.source as SearchTable, { ...opts, holdout: holdout ? (fx.holdout as SearchTable) : null });
    close(JSON.parse(JSON.stringify(got)), c.result, `case${i}`);
  });
});
