import copy
import unittest

import numpy as np
import pandas as pd

from modeling.config import load_config
from modeling.daytrade import bearish_structure, entry_time_allowed, make_entry, partial_trade, session_bars


class FixedOrdersTest(unittest.TestCase):
    def setUp(self):
        self.e = copy.deepcopy(load_config()["execution"])
        self.e["buy_fee_bps"]=self.e["sell_fee_bps"]=self.e["sell_tax_bps"]=0
        self.e["sell_tax_schedule"]=[]
        self.e["slippage_bps"]=self.e["half_spread_bps"]=0
        self.f = session_bars(pd.DataFrame(dict(timestamp=pd.date_range("2026-09-01 00:32",periods=8,freq="min",tz="UTC"),
                                               open=100.,high=100.1,low=99.9,close=100.,volume=100000.)))
        self.entry=dict(filled=True,quantity=100,capital=10000.,entry_price=100.,stop_price=99.,entry_bar_end=self.f.timestamp.iloc[0].isoformat())

    def test_fixed_targets_execute_without_any_model_signal(self):
        self.f.loc[1,["open","low","close"]]=102.;self.f.loc[1,"high"]=103.1
        self.f.loc[2:,["open","low","close"]]=104.;self.f.loc[2:,"high"]=104.1;self.f.loc[2,"high"]=105.1
        self.f.loc[3,"low"]=100.
        r=partial_trade(self.f,self.entry,self.e,np.zeros(8,bool),False)
        self.assertEqual(r,partial_trade(self.f,self.entry,self.e,np.ones(8,bool),False))
        self.assertEqual((r["tp3_quantity"],r["tp5_quantity"],r["remaining_quantity"]),(35,35,0))
        self.assertAlmostEqual(r["realized_net_return"],.028)
        self.assertTrue(r["be_armed"])

    def test_initial_stop_wins_ambiguous_target_bar(self):
        self.f.loc[1,"high"],self.f.loc[1,"low"]=106.,98.
        r=partial_trade(self.f,self.entry,self.e,np.zeros(8,bool))
        self.assertEqual(r["tp3_quantity"],0)
        self.assertTrue(r["ambiguous"])
        self.assertEqual(r["exit_reason"],"structural_stop")

    def test_new_be_touched_same_bar_does_not_award_tp5_or_earlier_open(self):
        self.f.loc[1,"open"],self.f.loc[1,"low"],self.f.loc[1,"high"]=100.,99.5,106.
        r=partial_trade(self.f,self.entry,self.e,np.zeros(8,bool))
        self.assertEqual((r["tp3_quantity"],r["tp5_quantity"]),(35,0))
        self.assertTrue(r["ambiguous"])
        self.assertEqual(r["fills"][-1]["price"],100.)

    def test_untouched_limit_does_not_become_later_market_sell(self):
        self.f.loc[1,"high"]=104.;self.f.loc[1,"volume"]=0
        r=partial_trade(self.f,self.entry,self.e,np.zeros(8,bool))
        self.assertEqual(r["tp3_quantity"],0)
        self.assertFalse(r["be_armed"])

    def test_bearish_close_exit_waits_for_next_bar(self):
        self.f.loc[2,"open"]=98.
        bear=np.zeros(8,bool);bear[1]=True
        r=partial_trade(self.f,self.entry,self.e,bear)
        self.assertEqual(r["exit_reason"],"bearish_structure")
        self.assertEqual(r["fills"][0]["bar_end"],self.f.timestamp.iloc[2].isoformat())
        self.assertAlmostEqual(r["realized_net_return"],-.02)

    def test_all_sells_share_volume_capacity_and_unfilled_remainder_survives(self):
        self.f.loc[1,"high"],self.f.loc[1,"low"],self.f.loc[1,"volume"]=106.,99.5,4000.
        self.f.loc[2:,"volume"]=0
        r=partial_trade(self.f,self.entry,self.e,np.zeros(8,bool))
        self.assertEqual(sum(v["quantity"] for v in r["fills"]),40)
        self.assertEqual(r["remaining_quantity"],60)
        self.assertFalse(r["complete"])
        self.assertIsNone(r["realized_net_return"])

    def test_time_windows_and_lunch_boundary(self):
        for time,allowed in [("08:00",True),("10:30",True),("10:31",False),("12:59",False),("13:00",True),("14:30",True),("14:31",False)]:
            self.assertEqual(entry_time_allowed(f"2026-09-01T{time}:00+09:00"),allowed)

    def test_session_gap_is_preserved_and_unknown_gap_rejected(self):
        f=self.f.copy();f.timestamp=pd.to_datetime(["2026-09-01T08:49:00+09:00","2026-09-01T08:50:00+09:00"]+
            [f"2026-09-01T09:0{i}:00+09:00" for i in range(1,7)],utc=True);f=session_bars(f)
        entry={**self.entry,"entry_bar_end":f.timestamp.iloc[0].isoformat()}
        partial_trade(f,entry,self.e,np.zeros(8,bool))
        with self.assertRaises(ValueError):partial_trade(f.drop(index=4),entry,self.e,np.zeros(7,bool))

    def test_end_of_day_exit_has_no_same_close_fill(self):
        f=self.f.iloc[:3].copy();f.timestamp=pd.date_range("2026-09-01 06:18",periods=3,freq="min",tz="UTC")
        f=session_bars(f)
        entry={**self.entry,"entry_bar_end":f.timestamp.iloc[0].isoformat()}
        r=partial_trade(f,entry,self.e,np.zeros(3,bool))
        self.assertEqual(r["exit_reason"],"session_end")
        self.assertEqual(r["fills"][0]["bar_end"],"2026-09-01T06:20:00+00:00")

    def test_three_share_minimum_and_gap_entry_rejection(self):
        f=self.f.copy();f["pullback_low"],f["recent_low5"],f["atr_pct"],f["recent_volume_mean5"]=99.8,99.8,.001,10000.
        f.loc[1,"volume"]=200.
        self.assertEqual(make_entry(f,0,self.e)["status"],"insufficient_partial_quantity")
        f.loc[1:,"timestamp"]+=pd.Timedelta(minutes=1)
        self.assertEqual(make_entry(f,0,self.e)["status"],"stale_signal_at_gap")

    def test_fee_inclusive_break_even_and_gap_through_stop(self):
        self.e=copy.deepcopy(load_config()["execution"])
        entry={**self.entry,"capital":10000*(1+self.e["buy_fee_bps"]/10000)}
        self.f.loc[1,["open","low","close"]]=102.;self.f.loc[1,"high"]=104.
        self.f.loc[2,"open"],self.f.loc[2,"low"]=98.,97.
        r=partial_trade(self.f,entry,self.e,np.zeros(8,bool))
        self.assertTrue(r["be_armed"])
        self.assertLess(r["realized_net_return"],0)


class BearishShapeTest(unittest.TestCase):
    def test_known_bearish_retest_and_rebreak_is_detected(self):
        from modeling.daytrade import mirror_bars
        from modeling.tests.test_shape_v2 import structure
        f=session_bars(mirror_bars(structure()))
        result=bearish_structure(f,"TEST")
        self.assertTrue(result.candidate.iloc[42])
        self.assertEqual(result.candidate.sum(),1)

    def test_every_prefix_matches_and_future_mutation_cannot_change_past(self):
        # A generic OHLC path suffices to exercise prefix stability in both directions.
        f=session_bars(pd.DataFrame(dict(timestamp=pd.date_range("2026-09-01 00:01",periods=80,freq="min",tz="UTC"),
            open=100+np.sin(np.arange(80)/4),high=101+np.sin(np.arange(80)/4),
            low=99+np.sin(np.arange(80)/4),close=100+np.sin(np.arange(80)/4),volume=100000.)))
        full=bearish_structure(f,"TEST")
        for cut in (30,50,70):
            pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True),bearish_structure(f.iloc[:cut],"TEST"))
        changed=f.copy();changed.loc[50:,["open","high","low","close"]]*=2
        pd.testing.assert_frame_equal(full.iloc[:50],bearish_structure(changed,"TEST").iloc[:50])


if __name__=="__main__":unittest.main()
