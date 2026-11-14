from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from modeling.config import load_config
from modeling.__main__ import status
from modeling.data import fetch_series, normalized_candle, open_store, regular_cursor
from modeling.execution import outcome, portfolio, sell_tax_bps
from modeling.features import FEATURES, daily_features, regular_bars, structural_features, symbol_features
from modeling.toss import TossAPI, credentials
from modeling.train import fitting_rows, split_dates, interval_summary


def bars(count=180):
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(.005, .12, count))
    return pd.DataFrame({"timestamp": pd.date_range("2026-01-05T09:01:00+09:00", periods=count, freq="min").tz_convert("UTC"),
                         "open": np.r_[close[0], close[:-1]], "close": close,
                         "high": np.maximum(np.r_[close[0], close[:-1]], close) + .03,
                         "low": np.minimum(np.r_[close[0], close[:-1]], close) - .03,
                         "volume": rng.integers(100, 10000, count).astype(float)})


def daily():
    times = pd.date_range("2024-01-01", "2026-01-05", freq="B", tz="Asia/Seoul").tz_convert("UTC")
    close = np.linspace(80., 100., len(times))
    return pd.DataFrame({"timestamp": times, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10000.})


def raw(at, price="100"):
    return {"timestamp": at, "openPrice": price, "highPrice": price, "lowPrice": price, "closePrice": price, "volume": "10", "currency": "KRW"}


class CredentialsTest(unittest.TestCase):
    def test_file_is_data_not_shell_and_environment_can_override(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            marker = Path(td) / "executed"
            path.write_text(f"TOSS_CLIENT_ID='$(touch {marker})'\nTOSS_CLIENT_SECRET='a#b'\n")
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                c = credentials(path)
            self.assertEqual(c["TOSS_CLIENT_SECRET"], "a#b")
            self.assertFalse(marker.exists())
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                credentials(path)

    def test_read_only_allowlist_precedes_auth(self):
        with tempfile.TemporaryDirectory() as td:
            api = TossAPI(Path(td)/"missing")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                api.get("/api/v1/orders")


class StatusTest(unittest.TestCase):
    def test_saved_stage_is_distinguished_from_a_live_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = load_config()
            c.update(data_dir=str(root / "data"), runs_dir=str(root / "runs"), credentials_file=str(root / "missing.env"))
            (root / "pipeline-state.json").write_text(json.dumps({"stage": "waiting_for_collector", "pid": 12345}))
            with patch("modeling.__main__.os.kill", side_effect=ProcessLookupError):
                result = status(c)
            self.assertEqual(result["pipeline"]["stage"], "waiting_for_collector")
            self.assertFalse(result["pipeline"]["process_alive"])


class CollectorTest(unittest.TestCase):
    def test_inclusive_pages_deduplicate_and_resume_without_requests(self):
        class API:
            def __init__(self):
                self.calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"candles": [raw("2026-01-05T10:03:00+09:00"), raw("2026-01-05T10:01:00+09:00")], "nextBefore": "2026-01-05T10:01:00+09:00"}
                return {"candles": [raw("2026-01-05T10:01:00+09:00"), raw("2026-01-05T09:59:00+09:00")], "nextBefore": None}
        with tempfile.TemporaryDirectory() as td:
            api = API()
            with open_store(Path(td)/"raw.sqlite") as db:
                args = (api, db, "005930", "1m", False, "2026-01-05T10:00:00+09:00", "2026-01-05T10:03:00+09:00")
                fetch_series(*args, progress=lambda _: None)
                fetch_series(*args, progress=lambda _: None)
                self.assertEqual(api.calls, 2)
                self.assertEqual(db.execute("SELECT count(*) FROM candles").fetchone()[0], 3)

    def test_invalid_ohlc_is_rejected(self):
        value = raw("2026-01-05T10:03:00+09:00")
        value["lowPrice"] = "101"
        with self.assertRaises(ValueError):
            normalized_candle(value)

    def test_regular_clock_cursor_does_not_skip_regular_minutes(self):
        self.assertEqual(regular_cursor("2026-01-05T12:00:00+09:00"), "2026-01-05T03:00:00+00:00")
        self.assertEqual(regular_cursor("2026-01-05T08:40:00+09:00"), "2026-01-04T06:20:00+00:00")


class CausalityTest(unittest.TestCase):
    def test_future_changes_cannot_change_past_features_or_candidates(self):
        c = load_config()
        frame = bars()
        first = symbol_features(frame, daily(), daily(), "005930", c)
        altered = frame.copy()
        altered.loc[110:, ["open", "high", "low", "close"]] *= 1.5
        second = symbol_features(altered, daily(), daily(), "005930", c)
        cols = [k for k in FEATURES if k in first] + ["candidate", "pattern_id", "stop_price"]
        pd.testing.assert_frame_equal(first.loc[:109, cols], second.loc[:109, cols])
        self.assertGreater(int(first.candidate.sum()), 0)

    def test_daily_context_available_next_day_and_live_prefix_matches(self):
        c, frame, d = load_config(), bars(), daily()
        historical = symbol_features(frame, d, d, "005930", c)
        past_daily = d[d.timestamp < pd.Timestamp("2026-01-05", tz="Asia/Seoul").tz_convert("UTC")]
        live = symbol_features(frame.iloc[:100], past_daily, past_daily, "005930", c)
        cols = [k for k in FEATURES if k in live] + ["candidate", "pattern_id", "stop_price"]
        pd.testing.assert_frame_equal(historical.loc[:99, cols], live.loc[:, cols])
        self.assertTrue(live.daily_ma240_distance.notna().all())

    def test_missing_minutes_reset_pattern_and_rolling_context(self):
        c, f = load_config(), bars()
        f = f.drop(index=60)
        result = structural_features(regular_bars(f), "005930", c)
        after = result[result.timestamp > pd.Timestamp("2026-01-05T10:01:00+09:00")].iloc[0]
        self.assertTrue(np.isnan(after.ret_15))
        self.assertFalse(after.candidate)

    def test_all_symbols_share_dates_and_cross_boundary_labels_are_purged(self):
        c = load_config()
        days = pd.bdate_range("2025-01-01", periods=160).strftime("%Y-%m-%d").tolist()
        dev, holdout, folds = split_dates(days, c)
        self.assertLess(dev[-1], holdout[0])
        for train, valid in folds:
            self.assertLess(train[-1], valid[0])
        f = pd.DataFrame({"day": [days[0], days[0]], "symbol": ["005930", "000660"], "filled": [True, True],
                          "net_R": [1., 1.], "exit_at": pd.to_datetime([days[0]+"T03:00:00Z", days[2]+"T03:00:00Z"], utc=True)})
        self.assertEqual(fitting_rows(f, [days[0]], days[1]).symbol.tolist(), ["005930"])


class ExecutionTest(unittest.TestCase):
    def config(self):
        c = load_config()
        e = c["execution"]
        e["sell_tax_schedule"] = []
        for k in ("buy_fee_bps", "sell_fee_bps", "sell_tax_bps", "slippage_bps", "half_spread_bps"):
            e[k] = 0.
        e.update(max_holding_minutes=5, max_position_krw=10000, participation=1., breakeven_trigger_pct=.02)
        return c

    def sample(self):
        f = bars(6)
        f[["open", "close"]] = 100.
        f["high"], f["low"], f["volume"] = 100.5, 99.8, 10000.
        return f

    def test_tax_change_uses_execution_date_and_feature_date(self):
        c, f = load_config(), bars()
        self.assertEqual(sell_tax_bps(c["execution"], "2025-12-26T15:20:00+09:00"), 15.)
        self.assertEqual(sell_tax_bps(c["execution"], "2025-12-29T09:01:00+09:00"), 20.)
        f["timestamp"] = pd.date_range("2025-12-26T09:01:00+09:00", periods=len(f), freq="min").tz_convert("UTC")
        result = symbol_features(f, daily(), daily(), "005930", c)
        selected = result.loc[result.candidate]
        self.assertGreater(len(selected), 0)
        e = c["execution"]
        expected = (e["buy_fee_bps"] + e["sell_fee_bps"] + 15. + 2*(e["slippage_bps"]+e["half_spread_bps"]))/10000
        np.testing.assert_allclose(selected.estimated_cost_to_risk * selected.signal_risk_pct, expected)

    def test_residual_exit_reprices_tax_when_execution_crosses_change_date(self):
        f, c = self.sample(), self.config()
        c["execution"]["sell_tax_schedule"] = load_config()["execution"]["sell_tax_schedule"]
        f["timestamp"] = pd.date_range("2025-12-26T15:15:00+09:00", periods=len(f), freq="min").tz_convert("UTC")
        f.loc[5, "volume"] = 0.
        next_bar = f.iloc[[-1]].copy()
        next_bar["timestamp"], next_bar["volume"] = pd.Timestamp("2025-12-29T09:01:00+09:00").tz_convert("UTC"), 10000.
        f = pd.concat([f, next_bar], ignore_index=True)
        result = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertAlmostEqual(result["path_pnl"][-2], -15.)
        self.assertAlmostEqual(result["net_pnl"], -20.)

    def test_same_bar_stop_and_target_uses_adverse_sequence(self):
        f, c = self.sample(), self.config()
        f.loc[1, ["high", "low"]] = [106., 98.]
        result = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertAlmostEqual(result["net_R"], -1.)
        self.assertTrue(result["ambiguous"])

    def test_35_35_targets_and_residual_exit(self):
        f, c = self.sample(), self.config()
        f.loc[2:, ["open", "high", "low", "close"]] = [103., 105.5, 102.5, 105.]
        f.loc[5, ["open", "high", "low", "close"]] = [105., 106., 105., 106.]
        result = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertEqual(result["quantity"], 100)
        self.assertAlmostEqual(result["net_pnl"], 460.)

    def test_gap_can_exceed_planned_stop_loss(self):
        f, c = self.sample(), self.config()
        f.loc[2, ["open", "high", "low", "close"]] = [80., 81., 79., 80.]
        result = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertAlmostEqual(result["net_R"], -20.)

    def test_unresolved_exit_cannot_disappear_from_portfolio(self):
        f, c = self.sample(), self.config()
        f.loc[5, "volume"] = 0.
        label = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertEqual(label["label_status"], "open_residual")
        record = {"symbol": "005930", "timestamp": f.timestamp.iloc[0], "day": "2026-01-05", "path_times": [], "path_pnl": [], **label}
        result = portfolio(pd.DataFrame([record]), [1.], 0., c)
        self.assertEqual(result["closed_trades"], 0)
        self.assertEqual(result["open_positions"][0]["remaining_quantity"], 100)
        self.assertAlmostEqual(result["available_cash_krw"], c["execution"]["initial_cash_krw"] - 10000.)
        self.assertAlmostEqual(result["equity_krw"], c["execution"]["initial_cash_krw"])
        self.assertIsNone(result["trade_log"][0]["net_R"])

    def test_unfilled_exit_continues_next_day_and_validation_cannot_see_it_early(self):
        f, c = self.sample(), self.config()
        f.loc[5, "volume"] = 0.
        next_day = f.iloc[[1]].copy()
        next_day["timestamp"] = pd.to_datetime(["2026-01-06T09:01:00+09:00"], utc=True)
        next_day[["open", "high", "low", "close"]] = [80., 81., 79., 80.]
        combined = pd.concat([f, next_day], ignore_index=True)
        label = outcome(combined, f.timestamp.iloc[0], 99., c)
        self.assertEqual(label["label_status"], "complete")
        self.assertAlmostEqual(label["net_R"], -20.)
        record = {"symbol": "005930", "timestamp": f.timestamp.iloc[0], "day": "2026-01-05", **label}
        short = portfolio(pd.DataFrame([record]), [1.], 0., c, all_days=["2026-01-05"])
        long = portfolio(pd.DataFrame([record]), [1.], 0., c, all_days=["2026-01-05", "2026-01-06"])
        self.assertAlmostEqual(short["net_pnl_krw"], 0.)
        self.assertEqual(len(short["open_positions"]), 1)
        self.assertIsNone(short["trade_log"][0]["net_R"])
        self.assertAlmostEqual(long["net_pnl_krw"], -2000.)
        self.assertEqual(long["closed_trades"], 1)
        self.assertAlmostEqual(sum(long["daily_pnl"].values()), long["net_pnl_krw"])
        record["regime"] = "down"
        scored = interval_summary(pd.DataFrame([record]), [1.], 0., c, ["2026-01-05"])
        self.assertIsNone(scored["mae_net_R"])

    def test_latched_profit_remainder_fills_at_later_available_open(self):
        f, c = self.sample(), self.config()
        f.loc[2, ["open", "high", "low", "close", "volume"]] = [103., 104., 102., 103., 10.]
        f.loc[3, ["open", "high", "low", "close", "volume"]] = [102., 102.5, 101., 102., 25.]
        f.loc[4, ["open", "high", "low", "close", "volume"]] = [104., 105.5, 103., 105., 35.]
        f.loc[5, ["open", "high", "low", "close"]] = [105., 106., 105., 106.]
        label = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertAlmostEqual(label["net_pnl"], 435.)
        self.assertEqual(label["remaining_quantity"], 0)

    def test_new_breakeven_stop_does_not_execute_at_pre_trigger_open(self):
        f, c = self.sample(), self.config()
        c["execution"].update(breakeven_trigger_pct=.005, buy_fee_bps=1., sell_fee_bps=1.)
        f.loc[1, ["high", "low", "close"]] = [100.6, 99.8, 100.4]
        label = outcome(f, f.timestamp.iloc[0], 99., c)
        self.assertTrue(label["ambiguous"])
        self.assertAlmostEqual(label["net_pnl"], 0., places=7)

    def test_portfolio_cash_slots_and_no_duplicate_symbol(self):
        f, c = self.sample(), self.config()
        label = outcome(f, f.timestamp.iloc[0], 99., c)
        rows = [{"symbol": s, "timestamp": f.timestamp.iloc[0], "day": "2026-01-05", **label} for s in ["005930", "005930", "000660", "000990", "002380"]]
        result = portfolio(pd.DataFrame(rows), [1.] * len(rows), 0., c)
        self.assertEqual(result["trades"], 3)
        self.assertAlmostEqual(result["net_pnl_krw"], 0.)


if __name__ == "__main__":
    unittest.main()
