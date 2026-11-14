import copy
import unittest

import numpy as np
import pandas as pd

from modeling.config import load_config
from modeling.daytrade import session_bars
from modeling.execution import portfolio
from modeling.protected_orders import partial_trade
from modeling.selective_daytrade import gated_alarms,portfolio_path,rank_daily
from modeling.selective_review import choose_cases


class SelectiveDaytradeTest(unittest.TestCase):
    def candidates(self):
        return pd.DataFrame([dict(symbol=str(i),day=day,selection_day=prior,macro=True,
            momentum20=i,ma60_slope20=i,atr20_pct=i,turnover20=1e10+i)
            for day,prior in [('2026-09-01','2026-08-31'),('2026-09-02','2026-09-01')] for i in range(5)])

    def test_top_three_are_fixed_before_tomorrows_signals(self):
        f=self.candidates();ranked=rank_daily(f)
        self.assertEqual(set(ranked.loc[ranked.top3,'symbol']),{'2','3','4'})
        self.assertTrue(ranked.loc[ranked.top3].groupby('day').size().eq(3).all())
        # A future lack of entry must not replace the strongest preselected stock.
        f['tomorrow_signals']=f.symbol.ne('4')
        pd.testing.assert_series_equal(rank_daily(f).top3,ranked.top3)

    def test_future_days_cannot_reorder_prior_ranks_and_ties_are_stable(self):
        f=self.candidates();a=rank_daily(f)
        f.loc[f.day.eq('2026-09-02'),'momentum20']*=-100
        b=rank_daily(f)
        pd.testing.assert_frame_equal(a.loc[a.day.eq('2026-09-01')].reset_index(drop=True),
            b.loc[b.day.eq('2026-09-01')].reset_index(drop=True))
        f=self.candidates();f[['momentum20','ma60_slope20','atr20_pct','turnover20']]=1.
        self.assertEqual(rank_daily(f).loc[lambda r:r.day.eq('2026-09-01')&r.top3,'symbol'].tolist(),['0','1','2'])

    def test_gate_can_confirm_after_model_score_already_crossed(self):
        f=pd.DataFrame(dict(atr_pct=[.01]*4,minute_eligible=[True]*4,candidate=[False,False,True,False],segment=[1]*4,volume_expansion5=[1.2]*4))
        self.assertEqual(gated_alarms([.8]*4,f,'current').tolist(),[True,False,False,False])
        self.assertEqual(gated_alarms([.8]*4,f,'confirmed').tolist(),[False,False,True,False])
        f.loc[2,'volume_expansion5']=.9
        self.assertFalse(gated_alarms([.8]*4,f,'confirmed_flow').any())

    def test_adapter_reconciles_costs_partial_exits_and_shared_cash(self):
        e=copy.deepcopy(load_config()['execution'])
        bars=session_bars(pd.DataFrame(dict(timestamp=pd.date_range('2026-09-01 00:32',periods=4,freq='min',tz='UTC'),
            open=[100.,102.,104.,101.],high=[100.1,103.5,105.5,101.2],low=[99.9,102.,104.,100.],close=[100.,103.,105.,100.5],volume=100000.)))
        capital=10000*(1+e['buy_fee_bps']/10000)
        entry=dict(filled=True,entry_price=100.,stop_price=99.,quantity=100,capital=capital,entry_bar_end=bars.timestamp.iloc[0].isoformat())
        outcome=partial_trade(bars,entry,e,np.zeros(len(bars),bool),False)
        row={**entry,**outcome,'symbol':'A','day':'2026-09-01','entry_at':(bars.timestamp.iloc[0]-pd.Timedelta(minutes=1)).isoformat()}
        path=portfolio_path(row,bars,e)
        self.assertAlmostEqual(path['path_pnl'][-1],capital*outcome['realized_net_return'],places=7)
        work=pd.DataFrame([path,{**path,'symbol':'B'}])
        e['initial_cash_krw']=15000.
        result=portfolio(work,[1.,1.],0.,{'execution':e},all_days=['2026-09-01'])
        self.assertEqual(result['trades'],1);self.assertGreaterEqual(result['available_cash_krw'],0)

    def test_review_sampling_does_not_use_future_outcomes(self):
        f=pd.DataFrame(dict(symbol=[str(i) for i in range(60)],candidate=[i%2==0 for i in range(60)],
            volume_expansion5=[.8,1.2]*30,open_return=[.02]*60,vwap_extension_atr=[3.]*60,top3=[True]*60,
            realized_net_return=np.linspace(-.1,.1,60)))
        before=choose_cases(f)[['symbol','review_group']]
        f['realized_net_return']=-f.realized_net_return
        pd.testing.assert_frame_equal(choose_cases(f)[['symbol','review_group']],before)
        self.assertFalse(before.symbol.duplicated().any())


if __name__=='__main__':unittest.main()
