"""Paired daily trend filters and user-selected +0.5% system BE replay."""
from __future__ import annotations

import argparse
import json
import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .config import atomic_json,digest
from .daily_trend import COHORTS,PARAMETERS,context
from .daytrade import RULES,bearish_structure,entry_time_allowed,session_bars
from .daytrade_research import summarize
from .liquid_trend import alarms
from .market_data import OUTPUT as SOURCE
from .market_research import METHODS,known_prefix
from .nextday_eval import daily_frame
from .pattern_eval import sha
from .protected_orders import BE_GAIN,categorize,category_counts,partial_trade
from .shape_experiment import DEVELOP,ROOT
from .shape_v2 import SHAPE_FEATURES
from .trade_accuracy import NAMES,measure

OUTPUT=ROOT/'outputs/model-research/daily-trend/20261030-v1'
POLICIES={'after_tp3':'기존: 첫3% 익절 후 본절','half_percent':'변경: +0.5%에서 본절',
          'half_percent_delay1':'민감도: 본절 발동 한 봉 지연'}
_DOWN=_EXECUTION=_MINUTE_HASHES=None


def daily_selection(output):
    watch=pd.read_parquet(SOURCE/'watchlist.parquet')
    sources=json.loads((SOURCE/'daily-collection.json').read_text())['sources']
    rows=[]
    for n,(symbol,group) in enumerate(watch.groupby('symbol'),1):
        path=SOURCE/'daily'/f'{symbol}-1d-adjusted.parquet'
        if sha(path)!=sources[str(path)]:raise ValueError('Frozen adjusted daily source changed')
        f=context(daily_frame(path)).assign(symbol=symbol)
        rows.append(group[['symbol','day','selection_day','known_at','market']].merge(f,on=['symbol','selection_day'],validate='one_to_one'))
        if n%200==0:print(f'Daily trend histories {n}/{watch.symbol.nunique()}',flush=True)
    selected=pd.concat(rows,ignore_index=True).sort_values(['day','symbol']).reset_index(drop=True)
    assert len(selected)==len(watch) and selected.selection_day.lt(selected.day).all()
    selected.to_parquet(output/'selection.parquet',index=False)
    return selected


def init_models(execution):
    global _DOWN,_EXECUTION,_MINUTE_HASHES
    threadpool_limits(1)
    _DOWN=joblib.load(DEVELOP/'confirmation-model.joblib');_EXECUTION=execution
    _MINUTE_HASHES=json.loads((SOURCE/'minute-source-hashes.json').read_text())


def replay_one(output,row,contract_id):
    output=Path(output);symbol,day=row['symbol'],row['day']
    target=output/'day-results'/f'{symbol}-{day}.json'
    path=SOURCE/'minutes'/f'{symbol}-{day}.parquet';source_hash=sha(path)
    if source_hash!=_MINUTE_HASHES[str(path)]:raise ValueError('Baseline minute source changed')
    if target.exists():
        r=json.loads(target.read_text())
        if r['contract_id']!=contract_id or r['minute_sha256']!=source_hash:raise ValueError('Frozen replay changed')
        return
    bars=session_bars(pd.read_parquet(path));entry_end=pd.Timestamp(row['entry_at'])+pd.Timedelta(minutes=1)
    positions=np.flatnonzero(bars.timestamp.eq(entry_end).to_numpy())
    if len(positions)!=1:raise ValueError('Entry bar not unique')
    start=int(positions[0]);future=known_prefix(bars.iloc[start:].reset_index(drop=True))
    down=bearish_structure(bars,symbol)
    rule=down.candidate.to_numpy(bool)
    model=alarms(_DOWN.predict_proba(down[SHAPE_FEATURES])[:,1],down,False)
    entry=dict(filled=True,entry_bar_end=entry_end.isoformat(),entry_price=row['entry_price'],
        stop_price=row['stop_price'],quantity=row['quantity'],capital=row['capital'])
    trades=[]
    for policy,delay in [('half_percent',False),('half_percent_delay1',True)]:
        for method,mask,enabled in [(METHODS[0],np.zeros(len(future),bool),False),
                (METHODS[1],rule[start:start+len(future)],True),(METHODS[2],model[start:start+len(future)],True)]:
            result=partial_trade(future,entry,_EXECUTION,mask,enabled,activation_delay=delay)
            assert sum(v['quantity'] for v in result['fills'])+result['remaining_quantity']==entry['quantity']
            trades.append({**row,'method':method,'policy':policy,'data_censored':len(future)<len(bars)-start,**result})
    atomic_json(target,dict(symbol=symbol,day=day,contract_id=contract_id,minute_sha256=source_hash,trades=trades))


def verify_orders(frame,execution):
    fills_checked=0
    for (symbol,day),group in frame.groupby(['symbol','day'],sort=False):
        bars=pd.read_parquet(SOURCE/'minutes'/f'{symbol}-{day}.parquet').set_index('timestamp')
        assert group.entry_at.nunique()==group.entry_price.nunique()==group.quantity.nunique()==group.capital.nunique()==1
        assert len(group)==len(METHODS)*len(POLICIES)
        for row in group.itertuples(index=False):
            assert entry_time_allowed(row.entry_at)
            used={pd.Timestamp(row.entry_at)+pd.Timedelta(minutes=1):row.quantity}
            sold=0
            for fill in row.fills:
                at=pd.Timestamp(fill['bar_end']);qty=int(fill['quantity']);assert qty>0
                used[at]=used.get(at,0)+qty;sold+=qty;fills_checked+=1
                if fill['reason'] in ('tp3','tp5'):
                    assert fill['price']>=row.entry_price*(1+(.03 if fill['reason']=='tp3' else .05))-1e-8
            assert sold+row.remaining_quantity==row.quantity
            for at,qty in used.items():assert qty<=np.floor(bars.loc[at,'volume']*execution['participation'])
    return fills_checked


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    baseline_protocol=json.loads((SOURCE/'protocol.json').read_text())
    for name,expected in baseline_protocol['source_modules'].items():
        if sha(Path(__file__).with_name(name))!=expected:raise ValueError('Frozen baseline source changed')
    if sha(DEVELOP/'confirmation-model.joblib')!=baseline_protocol['bearish_model_sha256']:raise ValueError('Frozen model changed')
    source_names=('daily_trend.py','protected_orders.py','daily_trend_study.py')
    protocol=dict(source_protocol_sha256=sha(SOURCE/'protocol.json'),source_trades_sha256=sha(SOURCE/'trades.parquet'),
        source_watchlist_sha256=sha(SOURCE/'watchlist.parquet'),sources={name:sha(Path(__file__).with_name(name)) for name in source_names},
        source_daily_manifest_sha256=sha(SOURCE/'daily-collection.json'),source_minute_manifest_sha256=sha(SOURCE/'minute-source-hashes.json'),
        daily_parameters=PARAMETERS,be_gain=BE_GAIN,execution=baseline_protocol['execution'],system_rules=RULES,
        policies=POLICIES,cohorts=COHORTS,
        methodology='Additional D-close filters on the existing selected cohort; exact same first filled entry and protective initial stop; fixed model weights and all partial targets unchanged',
        success='Completed aggregate net >=0, including actual BE exits; incomplete/gap-loss BE is not success; split profit vs rise-then-BE without double counting',
        price_trigger='Raw minute high >= executed buy price *1.005, with cost-cover floor; open activation precedes subsequent low; initially ambiguous stop wins',
        uncertainty='OHLC cannot recover activation/low order, gaps, queue or latency; report ambiguous probes and one-bar activation-delay sensitivity, not guaranteed real fills',
        scope='Current KOSPI/KOSDAQ snapshot; parent daily/liquidity/proxy rules retained; selected complete regular sessions only; no independent/unseen performance claim',
        model_retrained=False,reinforcement_learning=False,policy_active=False)
    contract_id=digest(protocol);protocol['contract_id']=contract_id
    path=output/'protocol.json'
    if path.exists() and json.loads(path.read_text())!=protocol:raise ValueError('Frozen experiment changed')
    if not path.exists():atomic_json(path,protocol)
    selection=daily_selection(output)
    baseline=pd.read_parquet(SOURCE/'trades.parquet');baseline['policy']='after_tp3'
    entries=baseline.loc[baseline.method.eq(METHODS[0]),['symbol','day','market','period','entry_at','signal_at',
        'entry_price','stop_price','quantity','capital','entry_session']]
    (output/'day-results').mkdir(exist_ok=True)
    with ProcessPoolExecutor(max_workers=3,mp_context=multiprocessing.get_context('spawn'),
            initializer=init_models,initargs=(protocol['execution'],)) as pool:
        futures=[pool.submit(replay_one,str(output),row,contract_id) for row in entries.to_dict('records')]
        for n,future in enumerate(as_completed(futures),1):
            future.result()
            if n%500==0 or n==len(futures):
                atomic_json(output/'progress.json',dict(stage='replay',completed=n,total=len(futures)))
                print(f'Replayed early BE {n}/{len(futures)}',flush=True)
    rows=[];minute_hashes={}
    for row in entries.itertuples(index=False):
        r=json.loads((output/'day-results'/f'{row.symbol}-{row.day}.json').read_text())
        assert r['contract_id']==contract_id
        minute=SOURCE/'minutes'/f'{row.symbol}-{row.day}.parquet'
        assert sha(minute)==r['minute_sha256'];minute_hashes[str(minute)]=r['minute_sha256']
        rows.extend(r['trades'])
    frame=categorize(pd.concat([baseline,pd.DataFrame(rows)],ignore_index=True))
    frame=frame.merge(selection[['symbol','day',*COHORTS]],on=['symbol','day'],validate='many_to_one')
    assert not frame.duplicated(['symbol','day','method','policy']).any()
    filled=verify_orders(frame,protocol['execution'])
    frame.to_parquet(output/'trades.parquet',index=False)
    stats={}
    for period,part in [('full_year',frame),('post_training',frame.loc[frame.period.eq('post_training')])]:
        stats[period]={}
        for cohort in COHORTS:
            groups=part.loc[part[cohort]]
            stats[period][cohort]={policy:{method:dict(accuracy=measure(g),categories=category_counts(g),execution=summarize(g.to_dict('records')))
                for method,g in group.groupby('method')} for policy,group in groups.groupby('policy')}
            for group in stats[period][cohort].values():
                for v in group.values():assert v['accuracy']['successes']==v['categories']['successes']
    support={cohort:dict(selected_sessions=int(selection[cohort].sum()),selected_symbols=int(selection.loc[selection[cohort],'symbol'].nunique()),
        entries=int(frame.loc[frame[cohort]&frame.policy.eq('after_tp3')&frame.method.eq(METHODS[0])].shape[0])) for cohort in COHORTS}
    result=dict(support=support,statistics=stats,accuracy_definition=protocol['success'],parameters=PARAMETERS,
        verification=dict(status='passed',parent_entries_preserved=len(entries),policy_probes=len(frame),fills_checked=filled,
            share_capacity_reconciled=True,identical_entries=True,source_models_unchanged=True,selection_sha256=sha(output/'selection.parquet')),
        policy_active=False,model_retrained=False)
    atomic_json(output/'statistics.json',result);atomic_json(output/'minute-source-hashes.json',minute_hashes)
    render(output,result)
    atomic_json(output/'progress.json',dict(stage='finished',entries=len(entries),support=support))
    print(json.dumps(dict(support=support,post_training=stats['post_training']),ensure_ascii=False,indent=2),flush=True)


def render(output,result):
    def table(period,method):
        rows=[]
        for cohort,label in COHORTS.items():
            for policy,pname in POLICIES.items():
                v=result['statistics'][period][cohort].get(policy,{}).get(method)
                if not v:continue
                s=v['accuracy'];c=v['categories'];e=v['execution']
                rows.append(f"<tr><td>{label}</td><td>{pname}</td><td>{s['successes']}/{s['entries']}</td><td>{s['accuracy']:.1%}</td>"
                    f"<td>{c['profit_success']}</td><td>{c['rise_then_breakeven_success']}</td><td>{c['other_flat_success']}</td>"
                    f"<td>{c['loss']}</td><td>{c['unclosed']}</td><td>{c['be_protection_failed']}</td><td>{e['ambiguous']}</td></tr>")
        return '<table><tr><th>일봉 선정</th><th>본절 시스템</th><th>성공/진입</th><th>정확도</th><th>수익 성공</th><th>상승 후 본절 보호 성공</th><th>기타 본절</th><th>손실</th><th>미청산</th><th>본절 보호 실패</th><th>순서 모호</th></tr>'+''.join(rows)+'</table>'
    body=''.join(f'<h2>학습 기준일 이후: {NAMES[method]}</h2>'+table('post_training',method)+
                 f'<h2>전체 연도: {NAMES[method]}</h2>'+table('full_year',method) for method in METHODS)
    doc='''<!doctype html><meta charset="utf-8"><title>일봉 큰 추세·추세선 돌파와 +0.5% 본절</title>
    <style>body{font:15px/1.7 system-ui;margin:30px auto;max-width:1500px;padding:15px;color:#163044}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:7px;text-align:left}th{background:#eef3f5}</style>
    <h1>일봉 큰 상승 추세 → 하락 추세선 돌파 → 다음 거래일 기존 장중 규칙</h1>
    <p>기존 전체 시장 전일 선정14,190일을 출발점으로 필터를 추가했다. 기존 일봉 대리 조건·유동성·추세 조건은 유지하므로 사용자 진입 규칙 전체를 완성한 통계가 아니다. 큰 추세는 종가>MA60, MA60이20거래일 전보다 상승으로 정의했다. 추세선은 과거60일 안에서5일 이상 떨어진 두 개의 낮아지는 고점을 잇는다. 양쪽2봉으로 확인하고 D-1까지 알려진 고점만 사용해 D 종가의 돌파를 확인한다. 돌파 후 종가가 선 아래로 내려가면 무효화하고 되살리지 않는다. 최근5일과 유지20일은 결과를 보기 전에 고정한 실험값이다.</p>
    <p>정확도는 수익 성공 + 상승 후 본절 보호 성공 + 기타 정확한 본절 / 실제 진입 전체다. 부분 익절과 잔량을 합친 비용 후 총손익>=0이고 전량 청산됐을 때만 성공이다. 본절 보호 성공은 실제 본절 매도 체결이 있고 전체 순손익이0 이상인 경우이며 부분 익절 이익이 포함될 수 있다. 그래서 수익 성공과 중복 세지 않는다. 갭·체결 부족으로 본절 주문 뒤 손실이나 잔량이 남으면 성공이 아니다. 단지 가격이 본절에 닿은 경우도 성공이 아니다.</p>
    <p>사용자 선택대로 매수 체결가 대비+0.5% 관측 시 비용 포함 본절 보호를 켠다. +3%/35%, +5%/35%, 잔량30%와 초기 손절·진입 시간은 유지한다. 새 본절과 되돌림의 봉내 순서는 OHLC로 확정할 수 없어 모호 사례 수를 공개하고, 완료 봉의 고가를 본 뒤 다음 봉부터 본절을 켜는 한 봉 지연 민감도도 비교했다. 이것은 호가·주문 지연·실제 체결 검증이나 엄밀한 수익 하한이 아니다. 보호 발동과 주문은 시스템이고 모델은 하락 구조만 판단한다.</p>
    <p>동일 진입·동일 초기 수량·동결 모델을 사용한 연구 재생이다. 전체 연도2025-10-29~2026-10-28은 회고적이고 학습 기준일 이후2026-08-27~10-28도 이미 본 연구 기간이다. 현재 상장 목록 편향과380봉 규격에 맞는 세션만 평가한 한계, 가정 비용과 독립 종목별 사례라는 한계를 유지한다.</p>'''+body+'''
    <p><a href="statistics.json">전체 통계·성공 분류·검산</a> · <a href="protocol.json">고정 조건·소스 해시</a> · <a href="selection.parquet">일봉 선정 근거·추세선 고점</a> · <a href="trades.parquet">거래별 체결·분류</a></p>'''
    (output/'REPORT.html').write_text(doc)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=OUTPUT)
    run(parser.parse_args().output)
