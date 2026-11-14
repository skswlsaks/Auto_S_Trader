import unittest

import numpy as np
import pandas as pd

from modeling.trade_accuracy import measure


class TradeAccuracyTest(unittest.TestCase):
    def frame(self):
        return pd.DataFrame(dict(day=['2026-09-01']*4,complete=[True,True,True,False],
            remaining_quantity=[0,0,0,10],realized_net_return=[.03,0.,-.01,np.nan],
            marked_net_return=[.03,0.,-.01,.05]))

    def test_zero_is_success_and_profitable_residual_is_not(self):
        r=measure(self.frame())
        self.assertEqual((r['entries'],r['successes'],r['profitable'],r['flat'],r['losses'],r['unclosed']),(4,2,1,1,1,1))
        self.assertEqual(r['accuracy'],.5)
        self.assertAlmostEqual(r['accuracy_among_closed'],2/3)
        self.assertEqual(r['day_bootstrap_95'],[.5,.5])

    def test_small_real_loss_is_not_rounded_to_break_even(self):
        f=self.frame().iloc[:1].copy();f.realized_net_return=-.000001
        self.assertEqual(measure(f)['successes'],0)

    def test_closed_trade_needs_realized_return_and_zero_shares(self):
        f=self.frame();f.loc[0,'realized_net_return']=np.nan
        with self.assertRaises(ValueError):measure(f)
        f=self.frame();f.loc[0,'remaining_quantity']=1
        with self.assertRaises(ValueError):measure(f)

    def test_empty_and_all_unclosed_have_explicit_denominators(self):
        self.assertIsNone(measure(self.frame().iloc[:0])['accuracy'])
        r=measure(self.frame().iloc[3:])
        self.assertEqual(r['accuracy'],0.)
        self.assertIsNone(r['accuracy_among_closed'])


if __name__=='__main__':unittest.main()
