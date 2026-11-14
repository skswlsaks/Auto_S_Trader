import unittest
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from modeling.features import regular_bars
from modeling.shape_v2 import detect, structure_broken
from modeling.shape_experiment import event_metrics, load_labels, supervised_alarms
from modeling.pattern_eval import sha


def structure():
    close = np.r_[np.linspace(108, 104, 20), 109, 107, 105, 103, 102, 103, 104, 103, 104, 104,
                  105, 103, 102, 101, 100.5, 101, 101.5, 102, 103.5, 104.5, 104.8, 105.2, 105.8, 106]
    opening = np.r_[close[0], close[:-1]]
    high, low = np.maximum(opening, close)+.2, np.minimum(opening, close)-.2
    high[20], high[30], low[34], low[41], high[41] = 110, 106, 100, 104, 105.4
    return pd.DataFrame(dict(timestamp=pd.date_range("2026-01-05 00:01", periods=len(close), freq="min", tz="UTC"),
                             open=opening, high=high, low=low, close=close, volume=100.))


class ShapeV2Test(unittest.TestCase):
    def test_high_pullback_wick_with_rising_close_is_detected(self):
        bars = structure()
        result = detect(regular_bars(bars), "TEST")
        self.assertGreater(bars.close.iloc[41], bars.close.iloc[40])
        self.assertEqual(result.rule_stage.iloc[41], "pullback")
        self.assertTrue(result.candidate.iloc[42])
        self.assertEqual(result.loc[42, ["anchor_high_1", "anchor_high_2"]].tolist(), [20, 30])
        self.assertGreater(result.pullback_low.iloc[42], 103.5)
        self.assertEqual(result.candidate.sum(), 1)

    def test_future_mutation_and_every_prefix_cannot_change_past(self):
        bars = structure()
        full = detect(regular_bars(bars), "TEST")
        for cut in range(20, len(bars)):
            prefix = detect(regular_bars(bars.iloc[:cut]), "TEST")
            pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True), prefix)
        changed = bars.copy()
        changed.loc[42:, ["open", "high", "low", "close"]] *= 10
        replay = detect(regular_bars(changed), "TEST")
        pd.testing.assert_frame_equal(full.iloc[:42], replay.iloc[:42])

    def test_price_scale_does_not_change_decisions(self):
        bars = structure()
        expected = detect(regular_bars(bars), "TEST")
        bars[["open", "high", "low", "close"]] *= 100
        actual = detect(regular_bars(bars), "TEST")
        self.assertEqual(expected.candidate.tolist(), actual.candidate.tolist())
        self.assertEqual(expected.rule_stage.tolist(), actual.rule_stage.tolist())

    def test_segments_cannot_share_a_setup(self):
        bars = structure()
        bars.loc[41:, "timestamp"] += pd.Timedelta(minutes=1)
        self.assertFalse(detect(regular_bars(bars), "TEST").candidate.any())

    def test_structural_low_controls_invalidation(self):
        self.assertFalse(structure_broken(102, 100, 1))
        self.assertTrue(structure_broken(99, 100, 1))

    def test_flat_no_volume_and_invalid_bars(self):
        bars = structure()
        bars[["open", "high", "low", "close"]] = 100.
        bars.volume = 0.
        self.assertFalse(detect(regular_bars(bars), "TEST").candidate.any())
        bars.loc[25, "low"] = 101
        with self.assertRaises(ValueError):
            detect(regular_bars(bars), "TEST")


class ResearchEvaluationTest(unittest.TestCase):
    def test_blind_labels_reject_pending_future_and_noninteger_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"bars").mkdir()
            path = root/"bars"/"B001.parquet"
            pd.DataFrame({"close": [100.]*180}).to_parquet(path)
            sample = dict(id="B001", score_start=120, score_end=179, bars_sha256=sha(path))
            (root/"manifest.json").write_text(json.dumps(dict(protocol={"version": "test"}, samples=[sample])))
            event = dict(type="reversal", confirmation=140, prefix_verified=True, highs=[100, 120], lows=[125, 135])
            def save(status="clear", **changes):
                (root/"annotations.json").write_text(json.dumps(dict(protocol="test", manifest_sha256=sha(root/"manifest.json"),
                    samples=[dict(id="B001", status=status, events=[{**event, **changes}])])) )
            for changes in ({"confirmation": 140.5}, {"highs": [100, 150]}, {"prefix_verified": False}):
                save(**changes)
                with self.assertRaises(ValueError):
                    load_labels(root)
            save(status="pending")
            with self.assertRaises(ValueError):
                load_labels(root)
            save()
            self.assertEqual(load_labels(root)[1]["B001"]["events"][0]["confirmation"], 140)

    def test_duplicate_and_late_alarms_keep_original_penalties(self):
        metrics = event_metrics([140, 170], [140, 141, 176])
        self.assertEqual((metrics["tp_reference"], metrics["fn"], metrics["fp"]), (1, 1, 1))
        self.assertEqual(metrics["delays"], [0])

    def test_supervised_alarm_rearms_at_segment_boundary(self):
        class Model:
            def predict_proba(self, frame):
                return np.array([[.1, .9]]*len(frame))
        from modeling.shape_v2 import SHAPE_FEATURES
        frame = pd.DataFrame(1., index=range(4), columns=SHAPE_FEATURES)
        frame["segment"] = [1, 1, 2, 2]
        alarms, _ = supervised_alarms(frame, Model())
        self.assertEqual(alarms.tolist(), [True, False, True, False])


if __name__ == "__main__":
    unittest.main()
