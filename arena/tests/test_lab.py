"""Stock-level lab conditions: no look-ahead, fixed buckets, missing data handling."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from arena import lab


def _write_candles(folder: Path, symbol: str, closes: list[float], volumes: list[float], start="2026-01-01"):
    days = pd.bdate_range(start, periods=len(closes))
    # stored like the collector does: KST midnight expressed in UTC (15:00 the previous day)
    ts = (days.tz_localize("Asia/Seoul")).tz_convert("UTC")
    c = np.asarray(closes, float)
    pd.DataFrame({"timestamp": ts, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                  "volume": np.asarray(volumes, float)}).to_parquet(folder / f"{symbol}-1d-raw.parquet")
    return days


class StockConditionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        n = 70
        closes = [100.0] * n
        volumes = [1000.0] * n
        closes[65] = 112.0       # +12% on day 65 (S)
        volumes[65] = 5000.0     # 5x volume on S
        closes[66] = 50.0        # D-day crash: must never leak into features for D
        volumes[66] = 1.0
        self.days = _write_candles(self.dir, "000001", closes, volumes)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_bars_are_keyed_by_kst_day(self):
        f = lab.daily_features("000001", self.dir)
        self.assertEqual(f.index[0], self.days[0])
        self.assertAlmostEqual(f.loc[self.days[65], "day_ret"], 0.12)

    def test_features_use_last_bar_strictly_before_trade_day(self):
        D = self.days[66].strftime("%Y-%m-%d")
        df = pd.DataFrame({"symbol": ["000001"], "_day": [D]})
        f = lab.features_before(df, "_day", "symbol", self.dir)
        self.assertAlmostEqual(f["day_ret"].iloc[0], 0.12)       # S bar, not the D crash
        self.assertAlmostEqual(f["vol_ratio"].iloc[0], 5.0)

    def test_stale_or_unknown_symbol_is_missing(self):
        late = (self.days[-1] + pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        df = pd.DataFrame({"symbol": ["000001", "999999"], "_day": [late, self.days[66].strftime("%Y-%m-%d")]})
        f = lab.features_before(df, "_day", "symbol", self.dir)
        self.assertTrue(f.isna().all().all())

    def test_attach_stock_buckets_and_skips(self):
        D = self.days[66].strftime("%Y-%m-%d")
        df = pd.DataFrame({"symbol": ["000001", "000001"], "_day": [D, D], "chg1": [np.nan, 0.05],
                           "ref_age": [0.0, 3.0], "_risk": [0.019, 0.04], "pick_status": ["invalidated", None]})
        cols = lab._attach_stock(df, "_day", "symbol", folder=self.dir)
        keys = {c["key"] for c in cols}
        self.assertIn("vol_ratio", keys)
        self.assertNotIn("stage", keys)  # absent in the table
        # chg1 wins when present, else the stock's own S-day change; edges are left-closed
        self.assertEqual(list(df["s_chg"]), ["+10~20%", "+5~10%"])
        self.assertEqual(list(df["ref_age"]), ["당일(0일)", "1~3일"])
        self.assertEqual(list(df["stop_dist"]), ["2% 미만", "4% 이상"])
        self.assertEqual(list(df["pick_status"]), ["invalidated", lab.MISSING])
        self.assertEqual(df["vol_ratio"].iloc[0], "3배 이상")
        self.assertEqual(df["ret5"].iloc[0], "+10~20%")

    def test_bars_merge_across_folders_with_priority(self):
        with tempfile.TemporaryDirectory() as other:
            other = Path(other)
            # a later copy extends the history by 5 days and disagrees on an overlapping day
            later = _write_candles(other, "000001", [100.0] * 75, [1000.0] * 75)
            f = lab.daily_features("000001", [self.dir, other])
            self.assertEqual(f.index[-1], later[-1])
            self.assertAlmostEqual(f.loc[self.days[65], "day_ret"], 0.12)  # first folder wins

    def test_columns_without_data_are_not_added(self):
        df = pd.DataFrame({"symbol": ["999999"], "_day": ["2026-03-02"]})
        cols = lab._attach_stock(df, "_day", "symbol", skip={"market"}, folder=self.dir)
        self.assertEqual(cols, [])


if __name__ == "__main__":
    unittest.main()
