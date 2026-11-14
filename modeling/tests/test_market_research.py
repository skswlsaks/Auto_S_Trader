import tempfile
import unittest
from pathlib import Path

import pandas as pd

from modeling.market_data import ever_liquid,share_universe
from modeling.market_research import known_prefix,normalized_day,render
from modeling.daytrade import session_bars


class WholeMarketTest(unittest.TestCase):
    def test_universe_includes_preferred_foreign_and_dr_without_sector_limit(self):
        rows=[dict(symbol=str(i),market=market,securityType=kind,isCommonShare=False)
              for i,(market,kind) in enumerate([('KOSPI','STOCK'),('KOSDAQ','FOREIGN_STOCK'),
                    ('KOSDAQ','DEPOSITARY_RECEIPT'),('KOSPI','ETF'),('KONEX','STOCK')])]
        self.assertEqual([v['symbol'] for v in share_universe(rows)],['0','1','2'])
        with self.assertRaises(ValueError):share_universe(rows+[dict(rows[0],market='KOSDAQ')])

    def test_liquidity_prefilter_uses_twenty_observed_days_and_selection_dates(self):
        f=pd.DataFrame(dict(timestamp=pd.date_range('2025-09-01',periods=20,tz='UTC'),close=10000.,volume=1e6))
        self.assertFalse(ever_liquid(f))  # High turnover before the study is insufficient.
        f.timestamp=pd.date_range('2025-10-09',periods=20,tz='UTC')
        self.assertTrue(ever_liquid(f))
        self.assertFalse(ever_liquid(f.iloc[:19]))
        f.timestamp=pd.date_range('2026-10-28',periods=20,tz='UTC')
        self.assertFalse(ever_liquid(f))

    def test_day_normalization_filters_sessions_and_rejects_revisions(self):
        def bar(time):return dict(timestamp=f'2026-09-01T{time}:00+09:00',open=100.,high=101.,low=99.,close=100.,volume=100.)
        records=[bar(t) for t in ['08:00','08:01','08:50','08:51','09:00','09:01','15:20','15:21']]
        f=normalized_day(records+[records[1]],'2026-09-01')
        self.assertEqual(len(f),4)
        self.assertTrue(normalized_day([],'2026-09-01').empty)
        with self.assertRaises(ValueError):normalized_day(records+[dict(records[1],close=100.5)],'2026-09-01')

    def test_only_scheduled_session_gap_is_allowed_in_held_position(self):
        times=['08:49','08:50','09:01','09:02','09:04','09:05']
        f=pd.DataFrame(dict(timestamp=pd.to_datetime([f'2026-09-01T{t}:00+09:00' for t in times],utc=True),
                            open=100.,high=101.,low=99.,close=100.,volume=100.))
        bars=session_bars(f)
        self.assertEqual(len(known_prefix(bars)),4)
        self.assertEqual(len(known_prefix(bars.iloc[:4])),4)

    def test_report_supports_overall_nested_and_market_flat_statistics(self):
        accuracy=dict(successes=1,entries=2,accuracy=.5,losses=1,unclosed=0,mean_marked_return_all=0.)
        groups={'system_only':dict(accuracy=accuracy,execution=dict(tp3_any=1,tp5_any=0))}
        post=dict(by_method=groups,by_market={'KOSPI':{'system_only':accuracy}})
        result=dict(universe=2741,screening=dict(daily_status_counts={'ready':1},selected_symbols=1,selected_stockdays=2),
                    statistics=dict(full_year=post,post_training=post),compute=dict(minute_errors={},replay_errors={}),session_status={'entered':2})
        with tempfile.TemporaryDirectory() as temp:
            render(Path(temp),result)
            self.assertIn('50.0%',(Path(temp)/'REPORT.html').read_text())


if __name__=='__main__':unittest.main()
