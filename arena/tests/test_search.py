"""Combination search: planted edge is found, period/calendar columns are not used as conditions,
aliases collapse, holdout matching, and the TS twin's parity fixture is current.

Regenerate the fixture after changing arena/search.py:  uv run python -m arena.tests.test_search --write
"""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

from arena import search as S

FIXTURE = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures" / "search.json"


def synthetic(seed=3, n_days=60, per_day=12):
    rng = np.random.default_rng(seed)
    days = [f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}" for d in range(n_days)]
    rows = []
    for d in range(n_days):
        regime = int(rng.random() < 0.5)
        for _ in range(per_day):
            m, flag, wd = int(rng.integers(3)), int(rng.random() < 0.4), d % 5
            r = rng.normal(-0.002, 0.012)
            if m == 1 and flag == 0 and regime == 1:  # planted: model b, no flag, regime on
                r += 0.012
            rows.append((d, m, flag, regime, wd, int(d >= 40), round(float(r), 8)))
    d, m, flag, regime, wd, period, ret = map(list, zip(*rows))
    col = lambda key, role, values, codes, labels=None: {"key": key, "label": key, "role": role, "values": values,
                                                         "labels": labels or values, "codes": codes}
    return {"n": len(ret), "days": days, "day": d, "ret": ret, "columns": [
        col("model", "model", ["a", "b", "c"], m),
        col("flag", "condition", ["False", "True"], flag, ["no flag", "flag"]),
        col("regime", "condition", ["off", "on"], regime),
        col("weekday", "condition", ["mon", "tue", "wed", "thu", "fri"], wd),
        col("period", "condition", ["pre", "post"], period),
        col("const", "condition", ["x"], [0] * len(ret)),
        col("month", "condition", ["01", "02", "03"], [int(x[5:7]) - 1 for x in (days[i] for i in d)]),
    ]}


CASES = [
    {"maxConds": 2, "minN": 20, "minDays": 8, "top": 12},
    {"maxConds": 1, "minN": 15, "minDays": 5, "sort": "worst", "top": 8, "split": {"by": "column", "key": "period", "select": ["pre"], "test": ["post"]}},
    {"maxConds": 2, "minN": 10, "minDays": 5, "sort": "q", "top": 10, "split": {"by": "date", "cut": "2026-02-05"}, "holdout": True},
]


def run_case(src, case, holdout):
    return S.search(src, max_conds=case["maxConds"], min_n=case["minN"], min_days=case["minDays"], sort=case.get("sort", "sel"),
                    top=case["top"], split=case.get("split"), holdout=holdout if case.get("holdout") else None)


def fixture():
    src, hold = synthetic(), synthetic(seed=9, n_days=10)
    return {"source": src, "holdout": hold, "cases": [{"options": c, "result": run_case(src, c, hold)} for c in CASES]}


class SearchTest(unittest.TestCase):
    def test_planted_edge_and_columns(self):
        r = S.search(synthetic(), min_n=20, min_days=8)
        self.assertEqual(r["skipped_columns"], {"period": "splits by date", "const": "one value only", "month": "calendar or split column"})
        best = r["rows"][0]
        self.assertEqual(best["model"], {"model": "b"})
        self.assertEqual(best["conds"], {"flag": "False", "regime": "on"})
        self.assertTrue(best["robust"], best)
        self.assertLess(best["sel"]["q"], 0.05)
        self.assertGreater(r["tried"], r["distinct"] - 1)

    def test_aliases_collapse(self):
        src = synthetic()
        c = next(c for c in src["columns"] if c["key"] == "model")
        flag = next(c for c in src["columns"] if c["key"] == "flag")["codes"]
        # add model "b_F" == model b restricted to flag False
        extra = [i for i, (m, f) in enumerate(zip(c["codes"], flag)) if m == 1 and f == 0]
        for k in ("day", "ret"):
            src[k] = src[k] + [src[k][i] for i in extra]
        for col in src["columns"]:
            col["codes"] = col["codes"] + ([3] * len(extra) if col["key"] == "model" else [col["codes"][i] for i in extra])
        c["values"].append("b_F"); c["labels"].append("b_F")
        src["n"] = len(src["ret"])
        r = S.search(src, min_n=20, min_days=8, top=500)
        keys = [(tuple(x["model"].values()), tuple(x["conds"].items())) for x in r["rows"]]
        self.assertIn((("b_F",), (("regime", "on"),)), keys)
        self.assertNotIn((("b",), (("flag", "False"), ("regime", "on"))), keys)  # same trades, more conditions

    def test_holdout_match(self):
        src, hold = synthetic(), synthetic(seed=9, n_days=10)
        h = S.match(hold, {"model": "b", "flag": "False"})
        mc = np.asarray(hold["columns"][0]["codes"]); fc = np.asarray(hold["columns"][1]["codes"])
        self.assertEqual(h["n"], int(((mc == 1) & (fc == 0)).sum()))
        self.assertIsNone(S.match(hold, {"nope": "x"}))
        self.assertEqual(S.match(hold, {"model": "zzz"}), {"n": 0})

    def test_p_value_and_bh(self):
        self.assertAlmostEqual(S.erfc(0.0), 1.0, places=6)
        self.assertAlmostEqual(S.erfc(1.0), 0.157299207, places=6)
        self.assertEqual(S.bh([0.01, 0.04, 0.03]), [0.03, 0.04, 0.04])

    def test_fixture_current(self):
        self.assertTrue(FIXTURE.exists(), "run: uv run python -m arena.tests.test_search --write")
        want = json.loads(json.dumps(fixture()))
        self.assertEqual(json.loads(FIXTURE.read_text()), want, "fixture stale: uv run python -m arena.tests.test_search --write")


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(fixture(), ensure_ascii=False))
        print(f"wrote {FIXTURE}")
    else:
        unittest.main()
