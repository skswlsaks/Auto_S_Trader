import copy
import unittest

import numpy as np
import pandas as pd

from modeling.config import load_config
from modeling.features import regular_bars
from modeling.liquid_trend import alarms, daily_context, exit_summary, intraday_context, stop_probe


def probe_bars():
    timestamps=pd.date_range("2026-01-05 00:31",periods=12,freq="min",tz="UTC")
    f=regular_bars(pd.DataFrame(dict(timestamp=timestamps,open=100.,high=100.1,low=99.9,close=100.,volume=100000.)))
    f["atr_pct"],f["pullback_low"],f["recent_low5"],f["recent_volume_mean5"] = .001,99.8,99.8,100000.
    f.loc[1,"low"]=99.7
    f.loc[2,["open","low","close"]]=99.5
    return f


class LiquidityContextTest(unittest.TestCase):
    def test_daily_future_changes_cannot_change_selection(self):
        f=pd.DataFrame(dict(timestamp=pd.date_range("2025-01-01",periods=100,freq="D",tz="UTC"),
                            open=np.arange(100)+100.,high=np.arange(100)+101.,low=np.arange(100)+99.,
                            close=np.arange(100)+100.,volume=1e9))
        full=daily_context(f,f)
        changed=f.copy();changed.loc[80:,["open","high","low","close","volume"]]*=100
        pd.testing.assert_frame_equal(full.iloc[:80],daily_context(changed,changed).iloc[:80])
        self.assertTrue(full.daily_focus.iloc[79])
        with self.assertRaises(ValueError):daily_context(f,f.iloc[:-1])

    def test_intraday_context_restarts_and_is_prefix_invariant(self):
        f=probe_bars();f=pd.concat([f]*4,ignore_index=True);f.timestamp=pd.date_range("2026-01-05 00:01",periods=len(f),freq="min",tz="UTC")
        f=regular_bars(f);daily=dict(log_turnover20=25.,daily_close_ma20=.1,daily_ma20_ma60=.1,daily_slope5=.01)
        e=load_config()["execution"]
        expected=intraday_context(f,daily,e)
        pd.testing.assert_frame_equal(expected.iloc[:30],intraday_context(f.iloc[:30],daily,e))
        f.loc[25:,"timestamp"]+=pd.Timedelta(minutes=1);f=regular_bars(f)
        split=intraday_context(f,daily,e)
        self.assertTrue(pd.isna(split.turnover5.iloc[25]))
        self.assertFalse(split.minute_eligible.iloc[25])

    def test_gate_can_activate_when_score_was_already_high(self):
        f=pd.DataFrame(dict(atr_pct=[.01]*3,segment=[1]*3,minute_eligible=[False,True,True]))
        self.assertEqual(alarms([.8]*3,f,True).tolist(),[False,True,False])


class StopExitTest(unittest.TestCase):
    def setUp(self):
        self.config=copy.deepcopy(load_config());self.config["execution"]["max_holding_minutes"]=8

    def test_no_same_bar_fill_credit_and_gap_loss_is_kept(self):
        r=stop_probe(probe_bars(),0,self.config)
        self.assertTrue(r["stop_complete"])
        self.assertEqual(r["completion_delay_bars"],1)
        self.assertGreater(r["adverse_stop_slippage_bps"],20)
        self.assertGreater(pd.Timestamp(r["first_exit_bar_end"]),pd.Timestamp(r["stop_observed_at"]))
        self.assertFalse(r["full_budget_next_bar_capacity_pass"])

    def test_zero_volume_cannot_sell_and_partials_are_carried(self):
        f=probe_bars();f.loc[2,"volume"]=0;f.loc[3,"volume"]=30000;f.loc[4,"volume"]=70000
        r=stop_probe(f,0,self.config)
        self.assertTrue(r["stop_complete"])
        self.assertEqual(r["completion_delay_bars"],3)
        self.assertFalse(r["next_bar_exit_capacity_pass"])

    def test_unfilled_residual_is_not_erased(self):
        f=probe_bars();f.loc[2:,"volume"]=0;r=stop_probe(f,0,self.config)
        self.assertEqual(r["status"],"stop_residual")
        self.assertEqual(r["remaining_quantity"],r["quantity"])
        self.assertIsNone(r["realized_net_return"])
        self.assertEqual(exit_summary([r])["residual_stops"],1)

    def test_no_stop_is_not_success_and_delay_stress_is_later(self):
        f=probe_bars();f.low=99.95;f.open=100.;f.close=100.;r=stop_probe(f,0,self.config)
        self.assertFalse(r["stop_triggered"])
        self.assertIsNone(exit_summary([r])["next_bar_complete_fraction_of_triggered"])
        self.assertEqual(stop_probe(probe_bars(),0,self.config,reaction_bars=2)["completion_delay_bars"],2)

    def test_stop_at_observation_boundary_is_reported_as_censored_residual(self):
        f=probe_bars();f.open=100.;f.close=100.;f.low=99.95;f.loc[8,"low"]=99.7
        r=stop_probe(f,0,self.config)
        self.assertTrue(r["stop_triggered"])
        self.assertFalse(r["stop_complete"])
        self.assertIsNone(r["first_exit_bar_end"])
        self.assertEqual(r["remaining_quantity"],r["quantity"])

    def test_wide_stop_and_future_gaps_reject_entry(self):
        f=probe_bars();f.pullback_low=95.
        self.assertEqual(stop_probe(f,0,self.config)["status"],"stop_risk_rejected")
        f=probe_bars();f.loc[3:,"timestamp"]+=pd.Timedelta(minutes=1)
        self.assertEqual(stop_probe(f,0,self.config)["status"],"future_gap")


if __name__=="__main__":unittest.main()
