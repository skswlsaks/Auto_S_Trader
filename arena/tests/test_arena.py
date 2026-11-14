import json
import tempfile
import unittest
from pathlib import Path

from arena import SCHEMA_VERSION
from arena.metrics import score, wilson
from arena.schema import validate_run

TRACK = {"track_id": "t", "windows": {"dev": {"start": "2026-01-05", "end": "2026-01-09"}},
         "gates": {"min_trades": 3, "min_active_days": 2}, "success_tolerance": 1e-9}


def trade(day, r, complete=True, hour="01"):
    return {"symbol": "000001", "entry_at": f"{day}T{hour}:00:00+00:00",
            "exit_at": f"{day}T05:00:00+00:00" if complete else None, "net_return": r, "complete": complete}


def run(**kw):
    return {"schema_version": SCHEMA_VERSION, "run_id": "x", "track_id": "t", "split": "dev",
            "model": {"name": "m", "family": "rl", "version": "1"}, "author": "a",
            "created_at": "2026-11-14T00:00:00+00:00", "artifact_hash": "h", **kw}


class MetricsTest(unittest.TestCase):
    def test_accuracy_counts_unclosed_as_failure_and_tolerates_float_noise(self):
        trades = [trade("2026-01-05", 0.02), trade("2026-01-05", -1e-16), trade("2026-01-06", -0.01),
                  trade("2026-01-07", 0.01, complete=False)]
        m = score(trades, TRACK, "dev")
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["unclosed"], 1)
        self.assertAlmostEqual(m["accuracy"], 2 / 4)   # breakeven noise counts, unclosed fails
        self.assertAlmostEqual(m["win_rate"], 1 / 3)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertTrue(m["passes_gates"])

    def test_curve_and_drawdown_on_business_days(self):
        trades = [trade("2026-01-05", 0.03), trade("2026-01-06", -0.05), trade("2026-01-08", 0.01)]
        m = score(trades, TRACK, "dev")
        self.assertEqual([d for d, _ in m["curve"]], ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
        self.assertEqual([v for _, v in m["curve"]], [3.0, -2.0, -2.0, -1.0, -1.0])
        self.assertAlmostEqual(m["max_drawdown_pp"], -5.0)
        self.assertAlmostEqual(m["total_return_pp"], -1.0)

    def test_kst_premarket_entry_maps_to_trading_day(self):
        t = {"symbol": "1", "entry_at": "2026-01-05T23:30:00+00:00", "exit_at": "2026-01-06T01:00:00+00:00",
             "net_return": 0.01, "complete": True}
        self.assertEqual(validate_run(run(), [t], {"t": TRACK}), [])
        self.assertEqual(score([t], TRACK, "dev")["curve"][1], ["2026-01-06", 1.0])

    def test_wilson(self):
        lo, hi = wilson(48, 243)
        self.assertAlmostEqual(lo, 0.1523, places=3)
        self.assertAlmostEqual(hi, 0.2521, places=3)


class SchemaTest(unittest.TestCase):
    def test_rejects_bad_submissions(self):
        tracks = {"t": TRACK}
        self.assertTrue(any("family" in e for e in validate_run(run(model={"name": "m", "family": "magic", "version": "1"}), [trade("2026-01-05", 0.0)], tracks)))
        self.assertTrue(any("outside" in e for e in validate_run(run(), [trade("2026-02-02", 0.0)], tracks)))
        self.assertTrue(any("[-1, 1]" in e for e in validate_run(run(), [trade("2026-01-05", 3.0)], tracks)))
        self.assertTrue(any("unknown track" in e for e in validate_run(run(track_id="nope"), [trade("2026-01-05", 0.0)], tracks)))
        self.assertEqual(validate_run(run(), [trade("2026-01-05", 0.0)], tracks), [])


if __name__ == "__main__":
    unittest.main()
