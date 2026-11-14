from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from modeling.feed import BAR_COLUMNS, TossInference, window


def candle(at, price="100"):
    return {"timestamp": at, "openPrice": price, "highPrice": price, "lowPrice": price,
            "closePrice": price, "volume": "10", "currency": "KRW"}


class FeedTest(unittest.TestCase):
    def test_fixed_window_deduplicates_inclusive_pages(self):
        class API:
            def __init__(self):
                self.calls = []

            def get(self, path, **query):
                self.calls.append(query)
                if len(self.calls) == 1:
                    return {"candles": [candle("2026-01-05T10:03:00+09:00"), candle("2026-01-05T10:02:00+09:00")],
                            "nextBefore": "2026-01-05T10:02:00+09:00"}
                return {"candles": [candle("2026-01-05T10:02:00+09:00"), candle("2026-01-05T10:01:00+09:00")],
                        "nextBefore": None}

        api = API()
        result = window(api, "005930", "1m", False, "2026-01-05T10:01:00+09:00", "2026-01-05T10:03:00+09:00")
        self.assertEqual(len(result), 3)
        self.assertFalse(result.timestamp.duplicated().any())
        self.assertEqual(api.calls[1]["before"], "2026-01-05T10:02:00+09:00")

    def test_provider_future_and_inconsistent_overlap_are_rejected(self):
        class API:
            def __init__(self, future):
                self.calls, self.future = 0, future

            def get(self, *args, **kwargs):
                self.calls += 1
                if self.future:
                    return {"candles": [candle("2026-01-05T10:04:00+09:00")]}
                return {"candles": [candle("2026-01-05T10:02:00+09:00", "100" if self.calls == 1 else "101")],
                        "nextBefore": "2026-01-05T10:02:00+09:00"}

        for future, message in [(True, "fixed inference"), (False, "revised")]:
            with self.subTest(future=future), self.assertRaisesRegex(ValueError, message):
                window(API(future), "005930", "1m", False, "2026-01-05T10:01:00+09:00", "2026-01-05T10:03:00+09:00")

    def feed(self, root):
        (root / "dataset.json").write_text(json.dumps({"coverage": [{"symbol": str(i)} for i in range(5)]}))
        predictor = SimpleNamespace(config={"data_dir": str(root), "api_workers": 2},
                                    bundle={"holdout_days": ["2026-01-06"]}, run=root)
        predictor.predict = lambda payload: {"symbol": payload["symbol"], "sector": payload["sector"], "decision": "hold"}
        with patch("modeling.feed.Predictor", return_value=predictor):
            return TossInference(root, api=object())

    def test_locked_period_and_closed_clock_never_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            feed = self.feed(Path(td))
            with self.assertRaisesRegex(ValueError, "locked final"):
                feed.snapshot("2026-01-06T10:03:00+09:00")
            result = feed.snapshot("2026-01-05T20:03:00+09:00")
            self.assertEqual(result["status"], "outside_regular_clock")
            self.assertFalse(result["orders_enabled"])

    def test_sector_uses_only_fresh_same_time_symbols(self):
        at = pd.Timestamp("2026-01-05T10:03:00+09:00").tz_convert("UTC")
        fresh = pd.DataFrame([[at, 100., 101., 99., 100., 1000.]], columns=BAR_COLUMNS)
        stale = fresh.assign(timestamp=at-pd.Timedelta(minutes=1))
        with tempfile.TemporaryDirectory() as td:
            feed = self.feed(Path(td))
            with patch("modeling.feed.window", side_effect=lambda api, symbol, *args: stale if symbol == "4" else fresh), \
                 patch.object(feed, "_daily", return_value=fresh), \
                 patch("modeling.feed.symbol_features", side_effect=lambda b, a, r, symbol, c: pd.DataFrame([{"ret_15": float(symbol), "daily_momentum20": .2}])):
                result = feed.snapshot(at.isoformat())
            self.assertEqual(result["status"], "scored")
            self.assertEqual(result["fresh_symbols"], 4)
            self.assertEqual(result["sector"]["sector_ret15"], 1.5)
            self.assertEqual(result["sector"]["as_of"], at.isoformat())
            self.assertEqual(result["predictions"][-1]["status"], "missing_or_stale_data")


if __name__ == "__main__":
    unittest.main()
