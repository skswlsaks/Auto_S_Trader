import copy
import json
import unittest

import pandas as pd

from modeling.config import load_config
from modeling.escape_eval import escape_trade, opportunity, sell_factor, summary


class EscapeTest(unittest.TestCase):
    def setUp(self):
        self.execution = copy.deepcopy(load_config()["execution"])
        self.execution["max_holding_minutes"] = 3
        self.frame = pd.DataFrame(dict(timestamp=pd.date_range("2026-09-01 00:32", periods=8, freq="min", tz="UTC"),
                                       open=100., high=100.1, low=99.9, close=100., volume=100000.))
        price = 100*(1+(self.execution["slippage_bps"]+self.execution["half_spread_bps"])/10000)
        self.entry = dict(signal_at="2026-09-01T00:31:00+00:00", quantity=1000,
                          capital=1000*price*(1+self.execution["buy_fee_bps"]/10000),entry_price=price,stop_price=99.)
        self.cover = self.entry["capital"]/self.entry["quantity"]/sell_factor(self.execution,self.frame.timestamp.iloc[0])

    def test_cost_cover_requires_more_than_flat_price_and_high_is_not_a_sale(self):
        self.assertGreater(self.cover,100.3)
        self.frame.high = self.cover+1
        r = escape_trade(self.frame,self.entry,self.execution)
        self.assertEqual(r["trigger_reason"],"timeout")
        self.assertFalse(r["net_nonnegative"])
        self.assertTrue(opportunity(self.frame,self.entry,self.execution)["net_0permil_high_any30"])

    def test_exact_net_cover_and_no_same_bar_exit(self):
        self.frame.loc[0,"close"] = self.cover
        self.frame.loc[1:,"open"] = self.cover
        r = escape_trade(self.frame,self.entry,self.execution)
        self.assertTrue(r["net_nonnegative"])
        self.assertAlmostEqual(r["realized_net_return"],0,places=12)
        self.assertGreater(pd.Timestamp(r["first_sale_at"]),pd.Timestamp(r["trigger_at"]))

    def test_favorable_close_followed_by_gap_is_loss_even_after_arming(self):
        self.frame.loc[0,"close"] = self.cover*1.002
        self.frame.loc[1,"open"] = 99.
        r = escape_trade(self.frame,self.entry,self.execution,target=.001)
        self.assertTrue(r["target_triggered"])
        self.assertLess(r["realized_net_return"],0)

    def test_partials_and_zero_volume_do_not_turn_residual_into_success(self):
        self.frame.loc[0,"close"] = self.cover
        self.frame.loc[1:,"volume"] = 0
        self.frame.loc[2,"volume"] = 30000
        self.frame.loc[2,"open"] = self.cover*1.01
        r = escape_trade(self.frame,self.entry,self.execution)
        self.assertEqual(r["remaining_quantity"],700)
        self.assertIsNone(r["realized_net_return"])
        self.assertFalse(r["net_nonnegative"])

    def test_stop_priority_and_same_bar_high_ambiguity(self):
        self.frame.loc[0,["high","close"]] = self.cover+1
        self.frame.loc[0,"low"] = 98.
        r = escape_trade(self.frame,self.entry,self.execution)
        self.assertEqual(r["trigger_reason"],"structural_stop")
        o = opportunity(self.frame,self.entry,self.execution)
        self.assertFalse(o["net_0permil_high_before_stop_lower"])
        self.assertTrue(o["net_0permil_high_before_stop_upper"])
        self.assertTrue(o["net_0permil_same_bar_stop_ambiguous"])

    def test_timeout_fills_after_horizon_and_delay_stress_is_later(self):
        base = escape_trade(self.frame,self.entry,self.execution)
        delayed = escape_trade(self.frame,self.entry,self.execution,delay=2)
        self.assertEqual(base["holding_bars"],4)
        self.assertEqual(delayed["holding_bars"],5)

    def test_later_prices_cannot_change_earlier_exit_and_gaps_are_rejected(self):
        self.frame.loc[0,"close"] = self.cover*1.01
        expected = escape_trade(self.frame,self.entry,self.execution)
        self.frame.loc[2:,["open","high","low","close"]] = 1.
        self.assertEqual(expected,escape_trade(self.frame,self.entry,self.execution))
        with self.assertRaises(ValueError):
            escape_trade(self.frame.drop(index=3),self.entry,self.execution)

    def test_summary_denominator_includes_loss_and_residual(self):
        r = escape_trade(self.frame,self.entry,self.execution)
        rows = [dict(day="2026-09-01",capital=100.,**r)]
        s = summary(rows)
        self.assertEqual(s["entries"],1)
        self.assertEqual(s["nonnegative_fraction"],0)
        self.assertEqual(s["nonnegative_day_bootstrap_95"],[0.,0.])
        json.dumps(s,allow_nan=False)


if __name__ == "__main__":
    unittest.main()
