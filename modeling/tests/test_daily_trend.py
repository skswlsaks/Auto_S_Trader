import unittest

import numpy as np
import pandas as pd

from modeling.daily_trend import context


def daily_bars():
    n=140;close=100+np.arange(n)*.2
    f=pd.DataFrame(dict(timestamp=pd.date_range('2025-01-01',periods=n,tz='UTC'),
                        open=close,high=close+.5,low=close-.5,close=close,volume=1e6))
    f.loc[90,'high']=130.;f.loc[100,'high']=128.
    f.loc[110:112,['open','high','low','close']]=[130.,130.5,129.5,130.]
    return f


class DailyTrendTest(unittest.TestCase):
    def test_confirmed_descending_high_line_breaks_at_completed_close(self):
        f=daily_bars();c=context(f)
        self.assertTrue(c.macro.iloc[110]);self.assertTrue(c.breakout_today.iloc[110])
        self.assertEqual(c.anchor2_day.iloc[110],f.timestamp.iloc[100].strftime('%Y-%m-%d'))
        self.assertEqual(c.breakout_age.iloc[110],0)
        self.assertFalse(c.breakout_today.iloc[109])

    def test_future_edits_and_every_relevant_prefix_leave_past_signals_unchanged(self):
        f=daily_bars();c=context(f)
        changed=f.copy();changed.loc[111:,['open','high','low','close']]*=10
        pd.testing.assert_frame_equal(c.iloc[:111],context(changed).iloc[:111])
        for cut in (45,100,110,111,120):
            pd.testing.assert_frame_equal(c.iloc[:cut].reset_index(drop=True),context(f.iloc[:cut]))

    def test_unconfirmed_high_is_not_used_to_draw_todays_line(self):
        f=daily_bars();f.loc[108,'high']=200.
        c=context(f)
        self.assertEqual(c.anchor2_day.iloc[110],f.timestamp.iloc[100].strftime('%Y-%m-%d'))

    def test_failed_break_is_not_revived_when_price_later_recovers(self):
        f=daily_bars();f.loc[115,['open','high','low','close']]=[110.,111.,109.,110.]
        f.loc[116,['open','high','low','close']]=[150.,151.,149.,150.]
        c=context(f)
        self.assertFalse(c.macro_break20.iloc[115]);self.assertFalse(c.macro_break20.iloc[116])
        self.assertTrue(pd.isna(c.breakout_day.iloc[116]))

    def test_straight_rally_is_not_automatically_a_line_break(self):
        f=daily_bars().iloc[:90];c=context(f)
        self.assertTrue(c.macro.iloc[-1]);self.assertFalse(c.breakout_today.any())


if __name__=='__main__':unittest.main()
