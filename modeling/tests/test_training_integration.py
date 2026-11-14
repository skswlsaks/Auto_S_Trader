"""Synthetic fixtures verify plumbing only; never reported as market performance."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from modeling import VERSION
from modeling.config import atomic_json, load_config
from modeling.execution import outcome
from modeling.features import FEATURES, contract_hashes
from modeling.train import evaluate, load_bundle, optimize


def fixture(root):
    c = load_config()
    c.update(data_dir=str(root / "data"), runs_dir=str(root / "runs"))
    c["training"].update(trials=3, threads=1, min_candidates=100, min_validation_trades=5,
                         max_estimators=20, early_stopping_rounds=5)
    e = c["execution"]
    e["sell_tax_schedule"] = []
    e.update(max_holding_minutes=5, max_position_krw=10000, participation=1., breakeven_trigger_pct=.029)
    for key in ("buy_fee_bps", "sell_fee_bps", "sell_tax_bps", "slippage_bps", "half_spread_bps"):
        e[key] = 0.
    folder = Path(c["data_dir"])
    (folder / "candles").mkdir(parents=True)
    days = pd.bdate_range("2026-01-05", periods=120).strftime("%Y-%m-%d").tolist()
    symbols, rows = ["000660", "000990", "005930"], []
    for number, symbol in enumerate(symbols):
        raw = []
        for i, day in enumerate(days):
            direction = 1 if (i + number) % 2 else -1
            prices = np.r_[100., 100 + direction * np.linspace(.2, 1., 6)]
            opens = np.r_[100., prices[:-1]]
            bars = pd.DataFrame({"timestamp": pd.date_range(day + "T09:31:00+09:00", periods=7, freq="min").tz_convert("UTC"),
                                 "open": opens, "high": np.maximum(opens, prices) + .02,
                                 "low": np.minimum(opens, prices) - .02, "close": prices, "volume": 100000.})
            label = outcome(bars, bars.timestamp.iloc[0], 98., c)
            assert label["filled"] and label["label_status"] == "complete"
            row = {name: 0. for name in FEATURES}
            row.update(ret_1=direction * .01, timestamp=bars.timestamp.iloc[0], symbol=symbol, day=day,
                       pattern_id=f"synthetic-{symbol}-{day}", stop_price=98., regime="up" if direction > 0 else "down", **label)
            rows.append(row)
            raw.append(bars)
        pd.concat(raw).to_parquet(folder / "candles" / f"{symbol}-1m-raw.parquet", index=False)
    frame = pd.DataFrame(rows).sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    for name in FEATURES:
        frame[name] = frame[name].astype("float32")
    path = folder / "candidates.parquet"
    frame.to_parquet(path, index=False)
    manifest = {"version": VERSION, "contract_hashes": contract_hashes(), "source": "synthetic_test_only",
                "partial": False, "feature_names": FEATURES, "days": days,
                "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "pattern": c["pattern"], "execution": c["execution"], "coverage": [{"symbol": s} for s in symbols],
                "limitations": ["Synthetic fixture; no financial performance evidence."]}
    atomic_json(folder / "dataset.json", manifest)
    return c


class TrainingIntegrationTest(unittest.TestCase):
    def test_inactive_policy_remains_inactive_in_evaluation(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            c = fixture(Path(td))
            c["training"]["min_validation_trades"] = 1_000_000
            trained = optimize(c)
            self.assertFalse(trained["policy_active"])
            result = evaluate(trained["run_dir"])
            self.assertFalse(result["policy_active"])
            self.assertEqual(result["tree"]["trades"], 0)
            self.assertEqual(result["tree"]["net_pnl_krw"], 0.)
            self.assertIn("진입 비활성화", Path(trained["run_dir"]).joinpath("REPORT.md").read_text())

    def test_optimize_serialize_evaluate_stress_and_holdout_lock(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            c = fixture(Path(td))
            trained = optimize(c)
            run = Path(trained["run_dir"])
            self.assertFalse(trained["holdout_accessed"])
            self.assertTrue((run / "trials.csv").is_file())
            importance = pd.read_csv(run / trained["feature_importance_file"])
            self.assertEqual(set(importance.feature), set(FEATURES))
            self.assertTrue(np.isclose(importance.gain_fraction.sum(), 1.))
            bundle = load_bundle(run)
            self.assertEqual(bundle["features"], FEATURES)
            result = evaluate(run)
            self.assertEqual(result["source"], "synthetic_test_only")
            self.assertFalse(result["live_approved"])
            self.assertEqual(result["policy_active"], bundle["policy_active"])
            self.assertEqual(json.loads((Path(c["runs_dir"]) / "latest.json").read_text())["status"], "research_evaluated")
            self.assertIn("double_cost", result)
            self.assertIn("one_bar_delay", result)
            self.assertTrue((run / "REPORT.md").is_file())
            self.assertTrue((run / "holdout-predictions.parquet").is_file())
            self.assertEqual(evaluate(run), result)
            other = run.parent / "different-model"
            other.mkdir()
            bundle["threshold"] += 1.
            joblib.dump(bundle, other / "model.joblib")
            report = json.loads((run / "training.json").read_text())
            report["model_sha256"] = hashlib.sha256((other / "model.joblib").read_bytes()).hexdigest()
            atomic_json(other / "training.json", report)
            with self.assertRaisesRegex(ValueError, "already opened"):
                evaluate(other)


if __name__ == "__main__":
    unittest.main()
