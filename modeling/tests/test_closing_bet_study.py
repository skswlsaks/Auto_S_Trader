import copy
import json
import subprocess
import unittest

import numpy as np
import pandas as pd

from modeling.closing_bet_study import overnight_exit, ROOT
from modeling.config import load_config
from modeling.daytrade import session_bars


class ClosingBetTest(unittest.TestCase):
    def setUp(self):
        self.execution = copy.deepcopy(load_config()['execution'])
        self.execution.update(buy_fee_bps=0, sell_fee_bps=0, sell_tax_bps=0,
                              slippage_bps=0, half_spread_bps=0, sell_tax_schedule=[])
        self.bars = session_bars(pd.DataFrame(dict(
            timestamp=pd.date_range('2026-09-01 00:01', periods=4, freq='min', tz='UTC'),
            open=101., high=101.5, low=100.5, close=101., volume=100000.)))
        self.entry = dict(entry_at='2026-08-31T15:30:00+09:00', entry_price=100., quantity=100, capital=10000.)

    def trade(self, **kwargs):
        return overnight_exit(self.bars, self.entry, self.execution, **kwargs)

    def test_immediate_cost_be_without_rise_trigger(self):
        self.bars.loc[0, ['open', 'high', 'low', 'close']] = [100., 100.1, 99.9, 100.]
        result = self.trade()
        self.assertAlmostEqual(result['realized_net_return'], 0.)
        self.assertEqual(result['tp3_quantity'], 0)
        self.assertEqual(result['fills'][0]['bar_end'], self.bars.timestamp.iloc[0].isoformat())

    def test_opening_gap_below_be_is_loss(self):
        self.bars.loc[0, ['open', 'high', 'low', 'close']] = [97., 99., 96., 98.]
        self.assertAlmostEqual(self.trade()['realized_net_return'], -.03)

    def test_targets_keep_original_35_35_quantity_then_be(self):
        self.bars.loc[0, ['high', 'close']] = [103.1, 103.]
        self.bars.loc[1, ['open', 'high', 'low', 'close']] = [104., 105.1, 104., 105.]
        self.bars.loc[2, 'low'] = 99.9
        result = self.trade()
        self.assertEqual((result['tp3_quantity'], result['tp5_quantity']), (35, 35))
        self.assertAlmostEqual(result['realized_net_return'], .028)
        self.assertEqual(sum(v['quantity'] for v in result['fills']), 100)

    def test_stop_wins_same_bar_conflict(self):
        self.bars.loc[0, ['high', 'low']] = [106., 99.]
        result = self.trade()
        self.assertEqual(result['tp3_quantity'], 0)
        self.assertTrue(result['ambiguous'])

    def test_first_minute_capacity_is_not_charged_for_yesterday_buy(self):
        self.bars.loc[0, ['low', 'volume']] = [99., 10000.]
        result = self.trade()
        self.assertTrue(result['complete'])
        self.assertEqual(result['fills'][0]['quantity'], 100)

    def test_partial_stop_latches_and_uses_next_open(self):
        self.bars.loc[0, ['low', 'volume']] = [99., 5000.]
        self.bars.loc[1, ['open', 'high', 'low', 'close']] = [98., 99., 97., 98.]
        result = self.trade()
        self.assertEqual([v['quantity'] for v in result['fills']], [50, 50])
        self.assertAlmostEqual(result['realized_net_return'], -.01)

    def test_cost_be_recovers_all_costs_but_price_be_does_not(self):
        self.execution = copy.deepcopy(load_config()['execution'])
        self.entry['capital'] = 10000 * (1 + self.execution['buy_fee_bps'] / 10000)
        self.bars.loc[0, 'low'] = 99.
        self.assertAlmostEqual(self.trade()['realized_net_return'], 0., places=12)
        self.assertLess(self.trade(cost_be=False)['realized_net_return'], 0.)

    def test_bearish_alarm_waits_for_next_bar_and_session_end_exits(self):
        result = self.trade(bearish=np.array([True, False, False, False]))
        self.assertEqual(result['fills'][0]['bar_end'], self.bars.timestamp.iloc[1].isoformat())
        self.bars.timestamp = pd.date_range('2026-09-01 06:17', periods=4, freq='min', tz='UTC')
        # Include the whole day prefix so the opening and end-of-day invariants hold.
        earlier = session_bars(pd.DataFrame(dict(timestamp=pd.date_range('2026-09-01 00:01', periods=380,
            freq='min', tz='UTC'), open=101., high=101.5, low=100.5, close=101., volume=100000.)))
        result = overnight_exit(earlier, self.entry, self.execution)
        self.assertEqual(result['exit_reason'], 'session_end')
        self.assertEqual(pd.Timestamp(result['exit_bar_end']).tz_convert('Asia/Seoul').strftime('%H:%M'), '15:20')

    def test_missing_or_zero_volume_leaves_unclosed_exposure(self):
        self.bars.volume = 0.
        result = self.trade()
        self.assertIsNone(result['realized_net_return'])
        self.assertEqual(result['remaining_quantity'], 100)
        self.assertFalse(overnight_exit(self.bars.iloc[:0], self.entry, self.execution)['complete'])

    def test_gap_and_same_day_purchase_rejected(self):
        with self.assertRaises(ValueError):
            overnight_exit(self.bars.drop(index=1), self.entry, self.execution)
        self.entry['entry_at'] = '2026-09-01T09:00:00+09:00'
        with self.assertRaises(ValueError):
            self.trade()

    @unittest.skipUnless((ROOT / 'trading/closing_bet_replay.mjs').exists(), 'trading/ scanners are not part of this checkout')
    def test_closing_selection_is_invariant_to_next_day_prices(self):
        bars = [dict(date=d.strftime('%Y-%m-%d'), open=100., high=101., low=99., close=100., volume=1000.)
                for d in pd.bdate_range('2026-01-01', periods=25)]
        bars[20].update(open=100., high=108., low=100., close=107.)
        bars[21].update(open=107., high=109., low=107., close=108.)
        bars[22].update(open=108., high=109.5, low=107., close=109.)
        day = bars[22]['date']

        def replay(values):
            result = subprocess.run(['node', str(ROOT / 'trading/closing_bet_replay.mjs')],
                input=json.dumps(dict(symbols=[dict(symbol='005930', bars=values)], start=day, end=day)),
                text=True, capture_output=True, check=True)
            return json.loads(result.stdout)

        expected = replay(bars[:23])
        self.assertEqual(len(expected['rows']), 1)
        altered = copy.deepcopy(bars)
        altered[23].update(open=1000., high=3000., low=1., close=2000.)
        self.assertEqual(expected, replay(bars))
        self.assertEqual(expected, replay(altered))


if __name__ == '__main__':
    unittest.main()
