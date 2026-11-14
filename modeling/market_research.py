"""Whole-market daily screening, selected-session collection and paired exit replay."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor,ThreadPoolExecutor,as_completed
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .config import atomic_json,digest,load_config
from .data import normalized_candle
from .daytrade import RULES,bearish_structure,make_entry,partial_trade,session_bars
from .daytrade_research import summarize
from .liquid_research import FEATURES,OUTPUT as LIQUID,validate
from .liquid_trend import alarms,daily_context,intraday_context
from .market_data import OUTPUT
from .nextday_eval import daily_frame,prior_sessions,replay_daily
from .pattern_eval import sha
from .sector_study import OUTPUT as PRIOR,SOURCE
from .shape_experiment import DEVELOP
from .shape_v2 import SHAPE_FEATURES,detect
from .toss import TossAPI
from .trade_accuracy import NAMES,measure

METHODS=("system_only","system_bearish_rule","system_bearish_transfer_model")
_UP=_DOWN=_EXECUTION=None


def screening(output):
    universe=json.loads((output/"universe.json").read_text())
    collection=json.loads((output/"daily-collection.json").read_text())
    contract=dict(universe=sha(output/"universe.json"),daily=sha(output/"daily-collection.json"))
    saved=output/"screening.json"
    if saved.exists():
        result=json.loads(saved.read_text())
        if result["source_contract"]!=contract or result["watchlist_sha256"]!=sha(output/"watchlist.parquet"):
            raise ValueError("Frozen screening inputs changed; use a new experiment")
        return pd.read_parquet(output/"watchlist.parquet")
    ready=[v for v in universe["symbols"] if json.loads((output/"daily-status"/f"{v['symbol']}.json").read_text())["status"]=="ready"]
    days=json.loads((SOURCE/"dataset.json").read_text())["days"]
    rows=[];scanned=0
    for offset in range(0,len(ready),25):
        group=ready[offset:offset+25]
        inputs=[];contexts=[];prices={};markets={v["symbol"]:v["market"] for v in group}
        for v in group:
            symbol=v["symbol"]
            a,r=[daily_frame(output/"daily"/f"{symbol}-1d-{kind}.parquet") for kind in ("adjusted","raw")]
            inputs.append(dict(symbol=symbol,bars=a[["date","open","high","low","close","volume"]].to_dict("records")))
            contexts.append(daily_context(a,r).assign(symbol=symbol))
            prices[symbol]=r[["date","close"]].merge(a[["date","close"]],on="date",suffixes=("_raw","_adj"),validate="one_to_one").set_index("date")
        previous=prior_sessions(days,[b["date"] for v in inputs for b in v["bars"]])
        next_day={v:k for k,v in previous.items()}
        records=[]
        for row in replay_daily(inputs,sorted(next_day))["rows"]:
            selection=row.pop("asOf");ref=row.pop("reference",None);point=row.pop("point",None);broken=row.pop("broken",None)
            d=prices[row["symbol"]].loc[selection];factor=d.close_raw/d.close_adj
            records.append(dict(**row,selection_day=selection,day=next_day[selection],market=markets[row["symbol"]],
                known_at=selection+"T23:59:59+09:00",reference_type=ref["type"] if ref else None,
                reference_day=ref["date"] if ref else None,secret_price=ref["secret"]*factor if ref and ref["secret"] is not None else None,
                reference_price=ref["level"]*factor if ref else None,point_price=point["close"]*factor if point else None,
                ma20_price=row.get("ma20",np.nan)*factor,invalidation_day=broken["date"] if broken else None))
        if records:
            f=pd.DataFrame(records).merge(pd.concat(contexts,ignore_index=True),on=["symbol","selection_day"],validate="many_to_one")
            scanned+=len(f);f["focus_selected"]=f.selected & f.daily_focus.fillna(False)
            rows.append(f.loc[f.focus_selected])
        print(f"Screened daily histories {min(offset+25,len(ready))}/{len(ready)}",flush=True)
    watch=pd.concat(rows,ignore_index=True).sort_values(["day","symbol"]).reset_index(drop=True)
    if watch.empty or watch.duplicated(["symbol","day"]).any() or not watch.selection_day.lt(watch.day).all():
        raise ValueError("Invalid whole-market next-session selection")
    watch.to_parquet(output/"watchlist.tmp",index=False);(output/"watchlist.tmp").replace(output/"watchlist.parquet")
    result=dict(source_contract=contract,watchlist_sha256=sha(output/"watchlist.parquet"),
        universe=len(universe["symbols"]),daily_status_counts=collection["status_counts"],
        screened_stockdays=scanned,selected_stockdays=len(watch),selected_symbols=int(watch.symbol.nunique()),
        by_market=watch.groupby("market").agg(symbols=("symbol","nunique"),stockdays=("day","size")).to_dict("index"),
        rule="D-close valid reference OR MA20-recovery will proxy, then unchanged daily liquidity/trend filters; no sector/top-N restriction",
        limits="Proxy and experimental entry rules retained; not full reproduction of all user rules; unknown history is not treated as a negative chart")
    atomic_json(saved,result);print(json.dumps(result,ensure_ascii=False),flush=True)
    return watch


def normalized_day(records,day):
    f=pd.DataFrame(records,columns=["timestamp","open","high","low","close","volume"])
    f.timestamp=pd.to_datetime(f.timestamp,utc=True)
    local=f.timestamp.dt.tz_convert("Asia/Seoul");minute=local.dt.hour*60+local.dt.minute
    f=f.loc[local.dt.strftime("%Y-%m-%d").eq(day)&(minute.between(481,530)|minute.between(541,920))]
    for _,g in f.loc[f.timestamp.duplicated(False)].groupby("timestamp"):
        if len(g[["open","high","low","close","volume"]].drop_duplicates())!=1:
            raise ValueError("Revised overlapping minute")
    return f.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def fetch_day(output,daily,api):
    symbol,day=daily["symbol"],daily["day"]
    path=output/"minutes"/f"{symbol}-{day}.parquet"
    if not path.exists():
        old=PRIOR/"minutes"/path.name
        if old.exists():path.symlink_to(old.resolve())
        else:
            records=[];before=day+"T15:20:00+09:00"
            for _ in range(3):
                page=api.get("/api/v1/candles",symbol=symbol,interval="1m",adjusted="false",count=200,before=before)
                values=[normalized_candle(v) for v in page["candles"]]
                times=[pd.Timestamp(v["timestamp"]) for v in values]
                if times!=sorted(times,reverse=True) or any(t>pd.Timestamp(before) for t in times):raise ValueError("Unordered or future candle page")
                records.extend(values)
                if not times or times[-1]<=pd.Timestamp(day+"T09:01:00+09:00"):break
                before=(times[-1]-pd.Timedelta(microseconds=1)).isoformat()
            else:raise ValueError("Regular-minute pagination budget exhausted")
            page=api.get("/api/v1/candles",symbol=symbol,interval="1m",adjusted="false",count=200,before=day+"T08:50:00+09:00")
            values=[normalized_candle(v) for v in page["candles"]]
            if any(pd.Timestamp(v["timestamp"])>pd.Timestamp(day+"T08:50:00+09:00") for v in values):raise ValueError("Future premarket candle")
            records.extend(values)
            f=normalized_day(records,day)
            f.to_parquet(path.with_suffix(".tmp"),index=False);path.with_suffix(".tmp").replace(path)
    return path


def known_prefix(future):
    """Unknown gaps censor open exposure; scheduled 08:50->09:01 is allowed."""
    gap=future.timestamp.diff().ne(pd.Timedelta(minutes=1))
    scheduled=future.minute.eq(541)&future.minute.shift().eq(530)
    bad=np.flatnonzero((gap & ~scheduled).to_numpy()[1:])+1
    return future.iloc[:int(bad[0])].reset_index(drop=True) if len(bad) else future


def init_models(execution):
    global _UP,_DOWN,_EXECUTION
    threadpool_limits(1)
    _UP=joblib.load(LIQUID/"model.joblib")
    _DOWN=joblib.load(DEVELOP/"confirmation-model.joblib")
    _EXECUTION=execution


def replay_day(output,daily,contract_id):
    output=Path(output);symbol,day=daily["symbol"],daily["day"]
    path=output/"minutes"/f"{symbol}-{day}.parquet"
    target=output/"day-results"/f"{symbol}-{day}.json"
    source_hash=sha(path)
    if target.exists():
        saved=json.loads(target.read_text())
        if saved["contract_id"]!=contract_id or saved["minute_sha256"]!=source_hash:raise ValueError("Frozen day result changed")
        return saved["status"]
    bars=session_bars(pd.read_parquet(path))
    base=dict(symbol=symbol,day=day,market=daily["market"],contract_id=contract_id,minute_sha256=source_hash,
        regular_bars=int(bars.minute.between(541,920).sum()),premarket_bars=int(bars.minute.between(481,530).sum()),
        premarket_active=int((bars.minute.between(481,530)&bars.volume.gt(0)).sum()),trades=[],attempts={})
    if base["regular_bars"]!=380:
        atomic_json(target,dict(**base,status="incomplete_regular_session"));return "incomplete_regular_session"
    f=intraday_context(detect(bars,symbol),daily,_EXECUTION)
    down=bearish_structure(bars,symbol)
    up=alarms(_UP.predict_proba(f[FEATURES])[:,1],f,True)
    down_model=alarms(_DOWN.predict_proba(down[SHAPE_FEATURES])[:,1],down,False)
    down_rule=down.candidate.to_numpy(bool)
    rejections=Counter()
    for index in np.flatnonzero(up):
        entry=make_entry(f,int(index),_EXECUTION)
        if not entry["filled"]:
            rejections[entry["status"]]+=1;continue
        if pd.Timestamp(entry["entry_at"])<=pd.Timestamp(daily["known_at"]):raise AssertionError("Entry before D selection")
        whole=bars.iloc[index+1:].reset_index(drop=True);future=known_prefix(whole)
        meta=dict(symbol=symbol,day=day,market=daily["market"],
            period="post_training" if day>"2026-08-26" else "retrospective_training_period",
            entry_at=entry["entry_at"],signal_at=entry["signal_at"],entry_price=entry["entry_price"],
            stop_price=entry["stop_price"],entry_session="premarket" if f.minute.iloc[index]<540 else "morning" if f.minute.iloc[index]<=630 else "afternoon",
            data_censored=len(future)<len(whole))
        for method,mask,enabled in ((METHODS[0],np.zeros(len(future),bool),False),
            (METHODS[1],down_rule[index+1:index+1+len(future)],True),
            (METHODS[2],down_model[index+1:index+1+len(future)],True)):
            outcome=partial_trade(future,entry,_EXECUTION,mask,enabled)
            assert sum(v["quantity"] for v in outcome["fills"])+outcome["remaining_quantity"]==entry["quantity"]
            base["trades"].append(dict(**meta,method=method,**outcome))
        break
    base["attempts"]=dict(rejections)
    status="entered" if base["trades"] else "no_entry"
    atomic_json(target,dict(**base,status=status))
    return status


def compute(output,watch):
    m=validate(LIQUID);execution=load_config()["execution"]
    if execution!=m["execution"]:raise ValueError("Execution changed")
    for name in ("minutes","day-results","minute-errors","replay-errors"):(output/name).mkdir(exist_ok=True)
    sources={name:sha(Path(__file__).with_name(name)) for name in ("market_research.py","market_data.py","daytrade.py","shape_v2.py","liquid_trend.py","features.py","execution.py","trade_accuracy.py")}
    protocol=dict(system_rules=RULES,execution=execution,source_modules=sources,
        model_sha256=sha(LIQUID/"model.joblib"),bearish_model_sha256=sha(DEVELOP/"confirmation-model.joblib"),
        watchlist_sha256=sha(output/"watchlist.parquet"),universe_sha256=sha(output/"universe.json"),
        entry="Unchanged first fillable signal per stock/day; daily proxy/liquidity/trend plus bullish classifier and time windows",
        universe="Whole KOSPI/KOSDAQ share list; no sector, current-cap-leader, or annual-return restriction",
        accuracy="Fully liquidated aggregate net PnL>=0 / all entered trades. Residual/censored open exposure is not success",
        limits=["Current listing snapshot; delisted API returned no historical names", "Not full reproduction of all user entry rules",
                "Full year retrospective, post-training dates exposed; bearish transfer not trained on dedicated bearish labels",
                "Independent probes, no shared account cash/positions; minute liquidity/price and costs are assumptions"],
        policy_active=False,reinforcement_learning=False)
    contract_id=digest(protocol);protocol["contract_id"]=contract_id
    target=output/"protocol.json"
    if target.exists() and json.loads(target.read_text())!=protocol:raise ValueError("Frozen run changed; use new output directory")
    if not target.exists():atomic_json(target,protocol)
    c=load_config();api=TossAPI(c["credentials_file"],c["api_min_interval_seconds"])
    statuses=Counter();download_errors={};replay_errors={}
    jobs=watch.to_dict("records");cpu_futures={}
    with ProcessPoolExecutor(max_workers=3,mp_context=multiprocessing.get_context("spawn"),initializer=init_models,initargs=(execution,)) as cpu:
        with ThreadPoolExecutor(max_workers=6) as io:
            futures={io.submit(fetch_day,output,row,api):row for row in jobs}
            for n,future in enumerate(as_completed(futures),1):
                row=futures[future];key=f"{row['symbol']}-{row['day']}"
                try:
                    future.result()
                    (output/"minute-errors"/f"{key}.json").unlink(missing_ok=True)
                    cpu_futures[cpu.submit(replay_day,str(output),row,contract_id)]=row
                except Exception as error:
                    download_errors[key]=f"{type(error).__name__}: {error}"
                    atomic_json(output/"minute-errors"/f"{key}.json",dict(symbol=row["symbol"],day=row["day"],error=download_errors[key]))
                if n%100==0 or n==len(jobs):
                    progress=dict(stage="minute_collection_and_replay",sessions_processed=n,total_sessions=len(jobs),
                        minute_errors=len(download_errors),replays_finished=sum(v.done() for v in cpu_futures))
                    atomic_json(output/"progress.json",progress);print(json.dumps(progress),flush=True)
        for n,future in enumerate(as_completed(cpu_futures),1):
            row=cpu_futures[future];key=f"{row['symbol']}-{row['day']}"
            try:
                statuses[future.result()]+=1;(output/"replay-errors"/f"{key}.json").unlink(missing_ok=True)
            except Exception as error:
                replay_errors[key]=f"{type(error).__name__}: {error}"
                atomic_json(output/"replay-errors"/f"{key}.json",dict(symbol=row["symbol"],day=row["day"],error=replay_errors[key]))
            if n%1000==0:print(f"Joined replay results {n}/{len(cpu_futures)}",flush=True)
    atomic_json(output/"compute-summary.json",dict(requested_sessions=len(jobs),statuses=dict(statuses),
        minute_errors=download_errors,replay_errors=replay_errors,all_sessions_accounted=len(jobs)==sum(statuses.values())+len(download_errors)+len(replay_errors)))
    aggregate(output,watch)


def aggregate(output,watch):
    rows=[];status=Counter();attempts=Counter();sources={};coverage=[]
    contract_id=json.loads((output/"protocol.json").read_text())["contract_id"]
    for v in watch.itertuples(index=False):
        path=output/"day-results"/f"{v.symbol}-{v.day}.json"
        if not path.exists():continue
        result=json.loads(path.read_text())
        if result["contract_id"]!=contract_id or result["minute_sha256"]!=sha(output/"minutes"/f"{v.symbol}-{v.day}.parquet"):
            raise ValueError("Frozen result or minute source changed before aggregation")
        status[result["status"]]+=1;attempts.update(result["attempts"])
        sources[str(output/"minutes"/f"{v.symbol}-{v.day}.parquet")]=result["minute_sha256"]
        rows.extend(result["trades"])
        coverage.append({k:result[k] for k in ("symbol","day","market","regular_bars","premarket_bars","premarket_active","status")})
    f=pd.DataFrame(rows)
    if f.empty:raise ValueError("No whole-market trades")
    if f.duplicated(["symbol","day","method"]).any():raise AssertionError("Duplicate probes")
    assert f.groupby(["symbol","day"]).size().eq(3).all()
    assert f.groupby(["symbol","day"]).capital.nunique().eq(1).all()
    f.to_parquet(output/"trades.parquet",index=False)
    pd.DataFrame(coverage).to_parquet(output/"coverage.parquet",index=False)
    atomic_json(output/"minute-source-hashes.json",sources)
    stats={}
    for period,part in (("full_year",f),("post_training",f.loc[f.period.eq("post_training")])):
        stats[period]=dict(by_method={method:dict(accuracy=measure(g),execution=summarize(g.to_dict("records"))) for method,g in part.groupby("method")},
            by_market={market:{method:measure(g) for method,g in groups.groupby("method")} for market,groups in part.groupby("market")},
            by_entry_session={session:{method:measure(g) for method,g in groups.groupby("method")} for session,groups in part.groupby("entry_session")})
    universe=json.loads((output/"universe.json").read_text());screen=json.loads((output/"screening.json").read_text())
    result=dict(universe=universe["total"],universe_by_market=universe["by_market"],screening=screen,
        session_status=dict(status),entry_rejections=dict(attempts),statistics=stats,
        sessions_with_premarket=int(sum(v["premarket_active"]>0 for v in coverage)),
        compute=json.loads((output/"compute-summary.json").read_text()),policy_active=False,profit_model_trained=False)
    atomic_json(output/"statistics.json",result)
    render(output,result)
    atomic_json(output/"progress.json",dict(stage="finished",universe=universe["total"],selected_sessions=len(watch),
        entered_sessions=int(f.loc[f.method.eq(METHODS[0])].shape[0]),session_status=dict(status),
        minute_errors=len(result["compute"]["minute_errors"]),replay_errors=len(result["compute"]["replay_errors"])))
    print(json.dumps(stats["post_training"]["by_method"],ensure_ascii=False,indent=2))


def render(output,result):
    def table(groups):
        rows=[]
        for method in METHODS:
            v=groups.get(method)
            if not v:continue
            s=v["accuracy"] if isinstance(v.get("accuracy"),dict) else v;e=v.get("execution")
            extra=f"<td>{e['tp3_any']} / {e['tp5_any']}</td>" if e else "<td>—</td>"
            rows.append(f"<tr><td>{NAMES[method]}</td><td>{s['successes']}/{s['entries']}</td><td>{s['accuracy']:.1%}</td>"
                f"<td>{s['losses']}</td><td>{s['unclosed']}</td><td>{s['mean_marked_return_all']:.3%}</td>{extra}</tr>")
        return '<table><tr><th>청산 방식</th><th>본절 이상/진입</th><th>정확도</th><th>손실</th><th>미청산</th><th>평균 순손익</th><th>3%/5% 일부 체결</th></tr>'+''.join(rows)+'</table>'
    post=result['statistics']['post_training'];full=result['statistics']['full_year']
    markets=''.join(f'<h3>{market}</h3>'+table(groups) for market,groups in post['by_market'].items())
    compute=result['compute'];daily=result['screening']['daily_status_counts']
    doc=f'''<!doctype html><meta charset="utf-8"><title>전체 시장 본절 이상 매매 정확도</title>
    <style>body{{font:16px/1.7 system-ui;max-width:1200px;margin:35px auto;padding:0 20px;color:#183044}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd;padding:9px;text-align:left}}th{{background:#eef3f5}}</style>
    <h1>코스피·코스닥 전체 주식 후보로 확장</h1>
    <p>API의 전체 상장 주식 {result['universe']:,}종목을 출발점으로 조사했다. 보통주·우선주·외국주식·예탁증권을 포함하고 ETF/ETN/리츠 등 별도 상품은 분리했다. 섹터·시총·연간 수익률로 후보 수를 제한하지 않는다. 현재 목록이며 상장폐지 조회가 빈 응답이라 과거 소멸 종목은 완전히 복원하지 못했다.</p>
    <p>일봉 수집 상태: {json.dumps(daily,ensure_ascii=False)}. 전일 선정·기존 유동성/추세 조건을 충족한 범위는 {result['screening']['selected_symbols']:,}종목·{result['screening']['selected_stockdays']:,}개 종목/거래일이다. 유동성 기준을 단 하루도 통과하지 못하는 종목도 원시 일봉으로 확인한 뒤 표시했다. 일부만 고른 섹터 대표종목 결과가 아니다.</p>
    <p>정확도는 부분 익절과 잔량 매도를 합친 비용 후 총손익이 본절 이상이고 전량 청산된 거래 / 실제 진입 전체다. 미청산·관측 공백으로 남은 수량은 성공에서 제외한다. 시스템은 매수 체결가 +3%에서35%, +5%에서35%, 잔량30%와 본절 보호다. 모델 청산 판단은 하락 구조뿐이며 새 수익 예측 모델을 학습하지 않았다.</p>
    <h2>학습 기준일 이후: 2026-08-27~10-28</h2>{table(post['by_method'])}
    <h2>과거1년 전체: 2025-10-29~2026-10-28</h2>{table(full['by_method'])}<h2>학습 기준일 이후 시장별</h2>{markets}
    <p>진입은 기존 일봉 대리 조건·상승 감지기와 08:00–10:30/13:00–14:30 시간대로 유지했다. 매수 직전 시크릿·포인트 재확인 등 모든 사용자 규칙을 완전히 재현한 성과는 아니다. 첫 익절 후 본절 활성화 시점과 초기 위험1% 등은 기존 실험 가정을 유지했고 취소한 새20일선 강제 필터를 추가하지 않았다.</p>
    <p>완료 상태: {json.dumps(result['session_status'],ensure_ascii=False)}. 분봉 수집 오류 {len(compute['minute_errors'])}건, 재생 오류 {len(compute['replay_errors'])}건은 별도 기록한다. 정규장380봉 불완전 세션은 이전 실험과 같은 기준으로 제외했다. 보유 중 뜻밖의 분봉 공백이 있으면 그 직전까지 재생하고 남은 수량을 검열·미청산으로 남겼다. 15:20 종료 잔량은 이후 종가 동시호가·애프터마켓 영구 미체결을 뜻하지 않는다.</p>
    <p>전체 연도는 이미 학습한 모델의 회고적 재생이고 학습 후 날짜도 공개된 연구 기간이다. 현재 상장 목록 편향, 미복원 과거 위험 상태, 분봉 체결·비용 가정이 있다. 종목·거래일 첫 유효 진입 하나를 세 청산 방식에 동일하게 적용한 독립 사례이며 공유 현금·동시 보유 한도를 반영한 계좌 승률은 아니다. 하락 전이 모델은 전용 하락 라벨로 새로 학습한 모델이 아니다.</p>
    <p><a href="statistics.json">전체 통계</a> · <a href="universe.json">전체 후보·상품 범위</a> · <a href="screening.json">전일 선정 범위</a> · <a href="protocol.json">고정 규칙·모델 해시</a> · <a href="trades.parquet">거래별 결과·부분 체결</a> · <a href="compute-summary.json">수집/재생 오류</a></p>'''
    (output/"REPORT.html").write_text(doc)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=OUTPUT)
    args=parser.parse_args()
    while not (args.output/"daily-collection.json").exists():time.sleep(1)
    compute(args.output,screening(args.output))
