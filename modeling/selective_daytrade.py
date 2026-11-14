"""D-close top-three watchlist, causal entry gates and shared-cash replay."""
from __future__ import annotations

import argparse
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .config import atomic_json,digest
from .daily_trend_study import OUTPUT as TREND,POLICIES
from .daytrade import bearish_structure,make_entry,partial_trade as old_orders,session_bars
from .daytrade_research import summarize
from .execution import portfolio,sell_tax_bps
from .liquid_research import FEATURES,OUTPUT as LIQUID,validate
from .liquid_trend import alarms,intraday_context
from .market_data import OUTPUT as SOURCE
from .market_research import known_prefix
from .nextday_eval import daily_frame
from .pattern_eval import sha
from .protected_orders import categorize,category_counts,partial_trade
from .shape_experiment import DEVELOP,ROOT
from .shape_v2 import SHAPE_FEATURES,detect
from .trade_accuracy import measure

OUTPUT=ROOT/'outputs/model-research/selective-daytrade/20261101-v2'
MODES={'current':'장중 기존 모델 신호','confirmed':'눌림·재상승 구조 확인 추가',
       'confirmed_flow':'구조 확인 + 최근 거래량 회복 추가'}
METHODS=('system_only','system_bearish_transfer_model')
WEIGHTS={'momentum20':.5,'ma60_slope20':.25,'atr20_pct':.15,'turnover20':.1}
_UP=_DOWN=_EXECUTION=_MINUTE_HASHES=None


def rank_daily(frame):
    """Rank all D-close candidates, including names that will never signal tomorrow."""
    f=frame.copy()
    if f.duplicated(['symbol','day']).any() or not f.selection_day.lt(f.day).all():raise ValueError('Invalid preselected calendar')
    valid=f.macro & np.isfinite(f[list(WEIGHTS)]).all(axis=1)
    f['daily_strength_score']=np.nan;f['daily_strength_rank']=np.nan
    work=f.loc[valid].copy();score=pd.Series(0.,index=work.index)
    for name,weight in WEIGHTS.items():score+=weight*work.groupby('day')[name].rank(method='average',pct=True)
    work['daily_strength_score']=score
    work=work.sort_values(['day','daily_strength_score','turnover20','symbol'],ascending=[True,False,False,True])
    work['daily_strength_rank']=work.groupby('day').cumcount()+1
    f.loc[work.index,['daily_strength_score','daily_strength_rank']]=work[['daily_strength_score','daily_strength_rank']]
    f['top3']=f.daily_strength_rank.le(3)
    return f.sort_values(['day','daily_strength_rank','symbol']).reset_index(drop=True)


def gated_alarms(scores,frame,mode):
    if mode=='current':return alarms(scores,frame,True)
    valid=frame.atr_pct.notna() & frame.minute_eligible & frame.candidate & (np.asarray(scores)>=.5)
    if mode=='confirmed_flow':valid &= frame.volume_expansion5.ge(1.)
    elif mode!='confirmed':raise ValueError('Unknown entry gate')
    prior=valid.shift(fill_value=False)&frame.segment.eq(frame.segment.shift())
    return (valid & ~prior).to_numpy(bool)


def prepare_selection(output):
    watch=pd.read_parquet(SOURCE/'watchlist.parquet')
    macro=pd.read_parquet(TREND/'selection.parquet')[['symbol','day','macro','ma60_slope20']]
    hashes=json.loads((SOURCE/'daily-collection.json').read_text())['sources'];parts=[]
    for symbol in sorted(watch.symbol.unique()):
        path=SOURCE/'daily'/f'{symbol}-1d-adjusted.parquet'
        if sha(path)!=hashes[str(path)]:raise ValueError('Changed daily source')
        a=daily_frame(path);prev=a.close.shift()
        tr=pd.concat([a.high-a.low,(a.high-prev).abs(),(a.low-prev).abs()],axis=1).max(axis=1)
        parts.append(pd.DataFrame(dict(symbol=symbol,selection_day=a.date,momentum20=a.close.pct_change(20),atr20_pct=tr.rolling(20).mean()/a.close)))
    f=watch.merge(macro,on=['symbol','day'],validate='one_to_one').merge(pd.concat(parts,ignore_index=True),on=['symbol','selection_day'],validate='many_to_one')
    f=rank_daily(f);f.to_parquet(output/'selection.parquet',index=False)
    assert f.loc[f.top3].groupby('day').size().le(3).all()
    return f


def init_models(execution):
    global _UP,_DOWN,_EXECUTION,_MINUTE_HASHES
    threadpool_limits(1);_EXECUTION=execution
    _UP=joblib.load(LIQUID/'model.joblib');_DOWN=joblib.load(DEVELOP/'confirmation-model.joblib')
    _MINUTE_HASHES=json.loads((SOURCE/'minute-source-hashes.json').read_text())


def prepare_one(output,daily,contract_id):
    output=Path(output);symbol,day=daily['symbol'],daily['day']
    target=output/'day-results'/f'{symbol}-{day}.json';path=SOURCE/'minutes'/f'{symbol}-{day}.parquet'
    source_hash=sha(path)
    if source_hash!=_MINUTE_HASHES[str(path)]:raise ValueError('Changed minute source')
    if target.exists():
        r=json.loads(target.read_text())
        if r['contract_id']!=contract_id or r['minute_sha256']!=source_hash:raise ValueError('Frozen result changed')
        return
    bars=session_bars(pd.read_parquet(path))
    base=dict(symbol=symbol,day=day,contract_id=contract_id,minute_sha256=source_hash,trades=[],diagnostic=None)
    if bars.minute.between(541,920).sum()!=380:
        atomic_json(target,dict(**base,status='incomplete_regular_session'));return
    f=intraday_context(detect(bars,symbol),daily,_EXECUTION)
    scores=_UP.predict_proba(f[FEATURES])[:,1];f['up_score']=scores
    f['ema20_price']=f.groupby('segment',sort=False).close.transform(lambda s:s.ewm(span=20,adjust=False).mean())
    f['vwap_price']=f.close/(1+f.vwap_distance)
    first=f.open.where(f.volume.gt(0)&f.volume.gt(0).cumsum().eq(1)).ffill()
    f['open_return']=f.close/first-1
    f['vwap_extension_atr']=(f.close-f.vwap_price)/(f.atr_pct*f.close)
    f['current_alarm']=gated_alarms(scores,f,'current')
    f.to_parquet(output/'frames'/f'{symbol}-{day}.parquet',index=False)
    if pd.notna(daily.get('baseline_signal_at')):
        at=pd.Timestamp(daily['baseline_signal_at']);indices=np.flatnonzero(f.timestamp.eq(at))
        if len(indices)!=1:raise ValueError('Missing baseline signal')
        i=int(indices[0]);assert f.current_alarm.iloc[i]
        names=['rule_stage','candidate','structure_type','up_score','close','volume_expansion5','turnover5',
               'ema20_slope5','ema5_ema20','vwap_distance','vwap_extension_atr','open_return',
               'pullback_low','resistance_price','structural_low','anchor_high_1','anchor_high_2','segment']
        diagnostic={name:(None if pd.isna(f[name].iloc[i]) else f[name].iloc[i].item() if isinstance(f[name].iloc[i],np.generic) else f[name].iloc[i]) for name in names}
        base['diagnostic']=dict(symbol=symbol,day=day,selection_day=daily['selection_day'],signal_at=at.isoformat(),
            signal_index=i,top3=bool(daily['top3']),daily_strength_rank=None if pd.isna(daily['daily_strength_rank']) else int(daily['daily_strength_rank']),
            daily_strength_score=None if pd.isna(daily['daily_strength_score']) else float(daily['daily_strength_score']),**diagnostic)
    if daily['top3']:
        down=bearish_structure(bars,symbol);bear=alarms(_DOWN.predict_proba(down[SHAPE_FEATURES])[:,1],down,False)
        for mode in MODES:
            for index in np.flatnonzero(gated_alarms(scores,f,mode)):
                entry=make_entry(f,int(index),_EXECUTION)
                if not entry['filled']:continue
                assert pd.Timestamp(entry['signal_at'])>pd.Timestamp(daily['known_at'])
                future=known_prefix(bars.iloc[index+1:].reset_index(drop=True));mask=bear[index+1:index+1+len(future)]
                meta=dict(symbol=symbol,day=day,selection_day=daily['selection_day'],market=daily['market'],mode=mode,
                    period='post_training' if day>'2026-08-26' else 'retrospective_training_period',
                    signal_at=entry['signal_at'],entry_at=entry['entry_at'],entry_price=entry['entry_price'],stop_price=entry['stop_price'],
                    daily_strength_score=float(daily['daily_strength_score']),daily_strength_rank=int(daily['daily_strength_rank']),
                    rule_stage=f.rule_stage.iloc[index],data_censored=bool(len(future)<len(bars)-index-1))
                for policy in POLICIES:
                    for method in METHODS:
                        if policy=='after_tp3':result=old_orders(future,entry,_EXECUTION,mask,method!=METHODS[0])
                        else:result=partial_trade(future,entry,_EXECUTION,mask,method!=METHODS[0],activation_delay=policy=='half_percent_delay1')
                        assert sum(v['quantity'] for v in result['fills'])+result['remaining_quantity']==entry['quantity']
                        base['trades'].append(dict(**meta,policy=policy,method=method,**result))
                break
    atomic_json(target,dict(**base,status='processed'))


def portfolio_path(row,bars,execution):
    """Adapter to the existing cash/slot engine, with conservative full-exit cash release."""
    start=pd.Timestamp(row['entry_at'])+pd.Timedelta(minutes=1)
    end=pd.Timestamp(row['exit_bar_end']) if row['complete'] else bars.timestamp.iloc[-1]
    f=known_prefix(bars.loc[bars.timestamp.ge(start)&bars.timestamp.le(end)].reset_index(drop=True))
    fills={}
    for fill in row['fills']:fills.setdefault(pd.Timestamp(fill['bar_end']),[]).append(fill)
    left=int(row['quantity']);proceeds=0.;times=[];pnls=[];remaining=[]
    impact=(execution['slippage_bps']+execution['half_spread_bps'])/10000
    for bar in f.itertuples(index=False):
        fee=(execution['sell_fee_bps']+sell_tax_bps(execution,bar.timestamp))/10000
        for fill in fills.get(bar.timestamp,[]):
            proceeds+=fill['quantity']*fill['price']*(1-fee);left-=fill['quantity']
        times.append(bar.timestamp.value);remaining.append(left)
        pnls.append(proceeds+left*bar.close*(1-impact)*(1-fee)-row['capital'])
    if not times or left!=row['remaining_quantity']:raise ValueError('Unreconciled portfolio path')
    expected=row['realized_net_return'] if row['complete'] else row['marked_net_return']
    if not np.isclose(pnls[-1],row['capital']*expected,rtol=0,atol=1e-6):raise ValueError('PnL path changed')
    risk=(row['entry_price']-row['stop_price'])*row['quantity']
    return {**row,'timestamp':pd.Timestamp(row['entry_at']),'exit_at':pd.Timestamp(row['exit_bar_end']) if row['complete'] else pd.NaT,
        'filled':True,'label_status':'complete' if row['complete'] else 'censored_open','net_pnl':pnls[-1],'net_R':pnls[-1]/risk,
        'path_times':times,'path_pnl':pnls,'path_remaining':remaining,'path_ambiguous':[bool(row['ambiguous'])]*len(times)}


def evaluate(output,frame,selection,execution):
    stats={};accounts={};paths={}
    for key,g in frame.groupby(['symbol','day'],sort=False):
        bars=session_bars(pd.read_parquet(SOURCE/'minutes'/f'{key[0]}-{key[1]}.parquet'))
        for row in g.to_dict('records'):paths[(row['mode'],row['policy'],row['method'],*key)]=portfolio_path(row,bars,execution)
    for period,days in [('full_year',sorted(selection.day.unique())),('post_training',sorted(selection.loc[selection.day.gt('2026-08-26'),'day'].unique()))]:
        stats[period]={};accounts[period]={}
        for (mode,policy,method),group in frame.loc[frame.day.isin(days)].groupby(['mode','policy','method']):
            work=pd.DataFrame([paths[(mode,policy,method,r.symbol,r.day)] for r in group.itertuples(index=False)])
            account=portfolio(work,work.daily_strength_score.to_numpy(),0.,{'execution':execution},all_days=days)
            accepted={(r['symbol'],r['day']) for r in account['trade_log']}
            chosen=group.loc[[key in accepted for key in zip(group.symbol,group.day)]]
            counts=pd.Series([r['day'] for r in account['trade_log']]).value_counts()
            assert counts.empty or counts.max()<=3
            key=f'{mode}/{policy}/{method}'
            summary=summarize(chosen.to_dict('records'))
            stats[period][key]=dict(accuracy=measure(chosen),categories=category_counts(chosen),execution=summary,
                proposed_entries=len(group),accepted_entries=len(chosen),
                account={k:v for k,v in account.items() if k not in ['trade_log','daily_pnl']})
            accounts[period][key]=account
    atomic_json(output/'accounts.json',accounts)
    return stats


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    for name in ['frames','day-results']:(output/name).mkdir(exist_ok=True)
    parent=json.loads((TREND/'protocol.json').read_text());original=json.loads((SOURCE/'protocol.json').read_text())
    for name,expected in {**parent['sources'],**original['source_modules']}.items():
        if sha(Path(__file__).with_name(name))!=expected:raise ValueError('Frozen source changed')
    validate(LIQUID);execution=original['execution']
    protocol=dict(source_sha256=sha(Path(__file__)),parent_protocol=sha(TREND/'protocol.json'),
        parent_watchlist=sha(SOURCE/'watchlist.parquet'),parent_daily_manifest=sha(SOURCE/'daily-collection.json'),
        parent_minute_manifest=sha(SOURCE/'minute-source-hashes.json'),up_model=sha(LIQUID/'model.joblib'),down_model=sha(DEVELOP/'confirmation-model.joblib'),
        rank_weights=WEIGHTS,maximum_daily_entries=3,macro_gate='D-close>MA60 and MA60 rising over20days, existing watchlist unchanged',
        rank_timing='D-close cross-sectional percentiles; rank includes future non-signalling stocks; top3 fixed before D+1; no replacements',
        entry_modes=MODES,policies=POLICIES,execution=execution,
        account='Existing chronological cash/3-slot engine; initial10m KRW, fixed proposed quantities <=3m each, insufficient cash skipped, daily2% stop retained; residuals remain open; partial proceeds released only on final exit',
        review='Boundary cases are chosen using pre-signal features, not future PnL; heuristic strata are not human gold labels',
        limits=['Current universe and proxy daily rules retained','Already exposed retrospective dates, not independent evidence',
                'Minute OHLC ordering, quotes, spread/impact and actual costs unverified','No new model training or live orders'],policy_active=False)
    contract_id=digest(protocol);protocol['contract_id']=contract_id
    target=output/'protocol.json'
    if target.exists() and json.loads(target.read_text())!=protocol:raise ValueError('Frozen selection experiment changed')
    if not target.exists():atomic_json(target,protocol)
    selected=prepare_selection(output)
    old=pd.read_parquet(SOURCE/'trades.parquet');old=old.loc[old.period.eq('post_training')&old.method.eq('system_only')]
    baseline={key:at for key,at in zip(zip(old.symbol,old.day),old.signal_at)}
    jobs=selected.loc[selected.top3|pd.Series([key in baseline for key in zip(selected.symbol,selected.day)],index=selected.index)].copy()
    jobs['baseline_signal_at']=[baseline.get(key) for key in zip(jobs.symbol,jobs.day)]
    with ProcessPoolExecutor(max_workers=3,mp_context=multiprocessing.get_context('spawn'),initializer=init_models,initargs=(execution,)) as pool:
        futures=[pool.submit(prepare_one,str(output),row,contract_id) for row in jobs.to_dict('records')]
        for n,future in enumerate(as_completed(futures),1):
            future.result()
            if n%100==0 or n==len(futures):
                atomic_json(output/'progress.json',dict(stage='replay',completed=n,total=len(futures)))
                print(f'Selective replay {n}/{len(futures)}',flush=True)
    rows=[];diagnostics=[]
    for row in jobs.itertuples(index=False):
        r=json.loads((output/'day-results'/f'{row.symbol}-{row.day}.json').read_text());assert r['contract_id']==contract_id
        rows.extend(r['trades'])
        if r['diagnostic']:diagnostics.append(r['diagnostic'])
    frame=categorize(pd.DataFrame(rows));frame.to_parquet(output/'trades.parquet',index=False)
    pd.DataFrame(diagnostics).to_parquet(output/'diagnostics.parquet',index=False)
    stats=evaluate(output,frame,selected,execution)
    result=dict(statistics=stats,selected_stockdays=int(selected.top3.sum()),selected_symbols=int(selected.loc[selected.top3,'symbol'].nunique()),
        baseline_review_entries=len(diagnostics),review_stage_counts={k:int(v) for k,v in pd.DataFrame(diagnostics).rule_stage.value_counts().items()},
        model_retrained=False,policy_active=False)
    atomic_json(output/'statistics.json',result);atomic_json(output/'progress.json',dict(stage='finished',selected_stockdays=int(selected.top3.sum())))
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=OUTPUT)
    run(parser.parse_args().output)
