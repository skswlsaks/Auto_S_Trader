import copy
import unittest

import numpy as np
import pandas as pd

from modeling.config import load_config
from modeling.daytrade import session_bars
from modeling.protected_orders import partial_trade,categorize,category_counts


class EarlyBreakevenTest(unittest.TestCase):
    def setUp(self):
        self.e=copy.deepcopy(load_config()['execution'])
        self.e.update(buy_fee_bps=0,sell_fee_bps=0,sell_tax_bps=0,slippage_bps=0,half_spread_bps=0,sell_tax_schedule=[])
        self.f=session_bars(pd.DataFrame(dict(timestamp=pd.date_range('2026-09-01 00:32',periods=5,freq='min',tz='UTC'),
            open=100.,high=100.1,low=99.9,close=100.,volume=100000.)))
        self.entry=dict(filled=True,quantity=100,capital=10000.,entry_price=100.,stop_price=99.,entry_bar_end=self.f.timestamp.iloc[0].isoformat())

    def run_trade(self):return partial_trade(self.f,self.entry,self.e,np.zeros(len(self.f),bool),False)

    def test_half_percent_then_breakeven_is_success_without_tp3(self):
        self.f.loc[1,'high']=100.6
        r=self.run_trade();self.assertTrue(r['be_armed']);self.assertEqual(r['tp3_quantity'],0)
        self.assertAlmostEqual(r['realized_net_return'],0.)
        s=category_counts(pd.DataFrame([dict(r,day='2026-09-01')]))
        self.assertEqual((s['rise_then_breakeven_success'],s['successes'],s['exact_flat']),(1,1,1))

    def test_open_already_above_activation_arms_before_the_later_low(self):
        self.f.loc[1,['open','high','low','close']]=[101.,101.1,98.,100.]
        r=self.run_trade();self.assertTrue(r['be_armed_from_open']);self.assertEqual(r['realized_net_return'],0.)

    def test_initial_stop_wins_unknown_intrabar_order(self):
        self.f.loc[1,['high','low']]=[101.,98.]
        r=self.run_trade();self.assertFalse(r['be_armed']);self.assertLess(r['realized_net_return'],0)
        self.assertTrue(r['ambiguous'])

    def test_gap_through_armed_stop_is_a_loss_not_success(self):
        self.f.loc[1,['open','high','low','close']]=[100.6,100.8,100.2,100.6]
        self.f.loc[2,['open','high','low','close']]=[98.,99.,97.,98.]
        r=self.run_trade();s=category_counts(pd.DataFrame([dict(r,day='2026-09-01')]))
        self.assertAlmostEqual(r['realized_net_return'],-.02)
        self.assertEqual((s['successes'],s['loss'],s['be_protection_failed']),(0,1,1))

    def test_cost_inclusive_breakeven_recovers_fees_tax_and_impact(self):
        self.e=copy.deepcopy(load_config()['execution'])
        self.entry['capital']=10000*(1+self.e['buy_fee_bps']/10000)
        self.f.loc[1,['open','high','low','close']]=[100.6,100.8,100.5,100.6]
        self.f.loc[2,['open','high','low','close']]=[100.4,100.5,100.1,100.2]
        r=self.run_trade();self.assertAlmostEqual(r['realized_net_return'],0.,places=12)
        self.assertTrue(r['net_nonnegative'])

    def test_fixed_partials_are_unchanged_and_be_profit_is_not_double_counted(self):
        self.f.loc[1,['open','high','low','close']]=[102.,103.1,102.,103.]
        self.f.loc[2,['open','high','low','close']]=[104.,105.1,104.,105.]
        r=self.run_trade();self.assertEqual((r['tp3_quantity'],r['tp5_quantity']),(35,35))
        self.assertAlmostEqual(r['realized_net_return'],.028)
        s=category_counts(pd.DataFrame([dict(r,day='2026-09-01')]))
        self.assertEqual((s['profit_success'],s['rise_then_breakeven_success'],s['successes']),(0,1,1))

    def test_observed_be_with_no_executable_volume_is_unclosed_not_success(self):
        self.f.loc[1,'high']=100.6;self.f.loc[1:,'volume']=0
        r=self.run_trade();self.assertFalse(r['complete']);self.assertIsNone(r['realized_net_return'])
        s=category_counts(pd.DataFrame([dict(r,day='2026-09-01')]))
        self.assertEqual((s['successes'],s['unclosed']),(0,1))

    def test_one_bar_activation_delay_does_not_credit_same_bar_flat_exit(self):
        self.f.loc[1,'high']=100.6
        self.f.loc[2,['open','high','low','close']]=[98.,99.,97.,98.]
        r=partial_trade(self.f,self.entry,self.e,np.zeros(len(self.f),bool),False,activation_delay=True)
        self.assertLess(r['realized_net_return'],0)
        self.assertEqual(r['be_activation_delay_bars'],1)


if __name__=='__main__':unittest.main()
