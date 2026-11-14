"""Study cells must reproduce the research session's own summary tables (when the data is present)."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from arena.store import OUTPUTS
from arena.studies import ALL, build_study

EG = OUTPUTS / "model-research" / "entry-gate" / "20261114-v1"
PR = OUTPUTS / "model-research" / "picks-runner" / "20261114-v1"


def _cells(summary):
    return {tuple(sorted(c["key"].items())): c for c in summary["cells"]}


@unittest.skipUnless((EG / "trades.parquet").exists(), "research data not present")
class EntryGateParity(unittest.TestCase):
    def test_every_summary_row_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = build_study(EG, Path(tmp))
        cells = _cells(s)
        sm = pd.read_csv(EG / "summary.csv")
        gate = {"all": ALL, "on": "True", "off": "False"}
        for r in sm.itertuples():
            c = cells[tuple(sorted({"pop": r.pop, "period": r.period, "variant": r.variant, "scope": r.scope, "gate_ok": gate[r.gate]}.items()))]
            self.assertEqual((c["n"], c["days"]), (r.n, r.days))
            self.assertAlmostEqual(c["mean"], r.mean, places=12)
            self.assertAlmostEqual(c["accuracy"], r.accuracy, places=12)
            self.assertAlmostEqual(c["profit_rate_01"], r.profit_share, places=12)  # their "profit_share" = net > +0.1%
            if not pd.isna(r.pf) and c["profit_factor"] is not None:
                self.assertAlmostEqual(c["profit_factor"], r.pf, places=9)
        self.assertEqual(s["trials"], sum(1 for c in s["cells"] if c["base"]))


@unittest.skipUnless((PR / "trades.parquet").exists(), "research data not present")
class PicksRunnerParity(unittest.TestCase):
    def test_summary_rows_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = build_study(PR, Path(tmp))
            cell_files = list((Path(tmp) / s["study_id"] / "cells").glob("*.json"))
        cells = _cells(s)
        for r in pd.read_csv(PR / "summary.csv").itertuples():
            c = cells[tuple(sorted({"period": ALL if r.period == "all" else r.period, "entry_variant": r.entry_variant, "policy": r.policy, "risk_flag": ALL}.items()))]
            self.assertEqual(c["n"], r.n)
            self.assertAlmostEqual(c["mean"], r.mean, places=12)
            self.assertAlmostEqual(c["accuracy"], r.accuracy, places=12)
            self.assertAlmostEqual(c["profit_rate_01"], r.win_rate, places=12)  # their "win_rate" = net > +0.1%
        self.assertEqual(len(cell_files), len(s["cells"]))
        q = [c["q_value"] for c in s["cells"] if c["base"]]
        self.assertTrue(all(0 <= v <= 1 for v in q if v is not None))


if __name__ == "__main__":
    unittest.main()
