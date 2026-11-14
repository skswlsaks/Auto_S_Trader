"""Replay fixed partial orders; independently compare bearish-only exit detectors."""
from __future__ import annotations

import argparse
import html
import json
import time
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .config import atomic_json, load_config
from .daytrade import RULES, bearish_structure, make_entry, partial_trade, session_bars
from .liquid_research import FEATURES, OUTPUT as LIQUID, validate
from .liquid_trend import alarms, intraday_context
from .pattern_eval import draw, sha, wilson
from .sector_study import OUTPUT
from .shape_experiment import DEVELOP
from .shape_v2 import SHAPE_FEATURES, contract, detect


def summarize(rows):
    if not rows:return dict(entries=0)
    f=pd.DataFrame(rows)
    values=f.marked_net_return.to_numpy()
    complete=f.loc[f.complete,"realized_net_return"].astype(float)
    wins=complete[complete>1e-12];losses=complete[complete < -1e-12]
    return dict(entries=len(f),completed=int(f.complete.sum()),residual=int((~f.complete).sum()),
        nonnegative=int(f.net_nonnegative.sum()),nonnegative_fraction=float(f.net_nonnegative.mean()),
        nonnegative_wilson=wilson(int(f.net_nonnegative.sum()),len(f)),
        mean_net_return=float(values.mean()),median_net_return=float(np.median(values)),
        mean_winner=float(wins.mean()) if len(wins) else None,mean_loser=float(losses.mean()) if len(losses) else None,
        worst_loss=float(max(0,-values.min())),max_gain=float(max(0,values.max())),
        tp3_any=int(f.tp3_quantity.gt(0).sum()),tp5_any=int(f.tp5_quantity.gt(0).sum()),
        tp3_full=int(f.tp3_complete.sum()),tp5_full=int(f.tp5_complete.sum()),
        be_armed=int(f.be_armed.sum()),be_observed=int(f.be_observed.sum()),bear_exit_triggered=int(f.bear_observed.sum()),
        ambiguous=int(f.ambiguous.sum()),median_capital=float(f.capital.median()),
        exit_reasons=dict(Counter(f.exit_reason)))


def replay(output):
    target=output/"replay"
    if (target/"evaluation.json").exists():raise ValueError("Replay frozen; choose another output")
    m=validate(LIQUID)
    model=joblib.load(LIQUID/"model.joblib")
    bearish_model=joblib.load(DEVELOP/"confirmation-model.joblib")
    e=load_config()["execution"]
    if e!=m["execution"]:raise ValueError("Execution assumptions changed")
    watch=pd.read_parquet(output/"watchlist.parquet")
    collected=json.loads((output/"collection-progress.json").read_text())
    if not collected.get("complete"):print("Following immutable day files while collection finishes",flush=True)
    target.mkdir(exist_ok=True)
    protocol=dict(system_rules=RULES,execution=e,
        target_prices="Gross executed buy price *1.03/*1.05, NOT net profit; limit only fills when impacted high reaches order price",
        initial_stop="Prior pullback/recent low minus .1 ATR, max 1% expected initial price risk; not a realized loss cap",
        intrabar="Existing stop before targets on ambiguous bar; after TP3, ambiguous newly armed BE before TP5; shared 1% minute volume including entry",
        model="No learned profit/target/BE decision. Frozen bullish entry detector retained; exit model sees mirrored shape features only",
        bearish_training="Transfer of frozen bullish shape classifier through reciprocal-price features; NO dedicated bearish-label training or bearish accuracy claim",
        comparison=["system_only","system_bearish_rule","system_bearish_transfer_model"],
        entry_cohort="First fillable eligible signal per stock/day, identical entries across all exit methods; no cash/shared-stock portfolio claim",
        sessions="08:01–08:50 and09:01–15:20 bar-end times; indicators reset at session/gap, positions persist through scheduled break",
        dates="Full year replay is retrospective (model trained through 2026-08-26); post-cutoff reported separately, all exposed dates",
        sector_selection="Ex-post current-cap leader basket ranking; current membership and selection bias; no independent sector performance claim",
        rules_sha256=sha(Path(__file__).with_name("daytrade.py")),shape_contract=contract(),
        replay_source_sha256=sha(Path(__file__)),model_sha256=sha(LIQUID/"model.joblib"),
        bearish_transfer_model_sha256=sha(DEVELOP/"confirmation-model.joblib"),
        watchlist_sha256=sha(output/"watchlist.parquet"),ranking_sha256=sha(output/"sector-ranking.json"),
        policy_active=False,reinforcement_learning=False)
    atomic_json(target/"protocol.json",protocol)
    trades,rejections,coverage,sources,events=[],[],[],{},[]
    # Review windows are chosen from post-cutoff frames before returns are consulted.
    review_pool=[]
    for n,(_,daily) in enumerate(watch.iterrows(),1):
        path=output/"minutes"/f"{daily.symbol}-{daily.day}.parquet"
        for _ in range(120):
            if path.exists():break
            time.sleep(.5)
        else:raise ValueError("Minute file not ready after 60 seconds; complete collection and rerun")
        sources[str(path)]=sha(path)
        bars=session_bars(pd.read_parquet(path))
        regular=bars.minute.between(541,920)
        coverage.append(dict(symbol=daily.symbol,day=daily.day,sector=daily.sector,
            regular_bars=int(regular.sum()),premarket_bars=int(bars.minute.between(481,530).sum()),
            premarket_active=int((bars.minute.between(481,530)&bars.volume.gt(0)).sum())))
        if int(regular.sum())!=380:
            rejections.append(dict(symbol=daily.symbol,day=daily.day,status="incomplete_regular_session"));continue
        f=intraday_context(detect(bars,daily.symbol),daily,e)
        down=bearish_structure(bars,daily.symbol)
        up_scores=model.predict_proba(f[FEATURES])[:,1]
        up=alarms(up_scores,f,True)
        down_scores=bearish_model.predict_proba(down[SHAPE_FEATURES])[:,1]
        down_model=alarms(down_scores,down,False)
        down_rule=down.candidate.to_numpy(bool)
        events.extend(dict(symbol=daily.symbol,day=daily.day,sector=daily.sector,at=t.isoformat(),kind=kind)
            for kind,mask in (("bearish_rule",down_rule),("bearish_transfer_model",down_model)) for t in f.loc[mask,"timestamp"])
        if daily.day>"2026-08-26":
            review_pool.append(dict(symbol=daily.symbol,day=daily.day,sector=daily.sector,path=str(path),
                                    rule_indices=np.flatnonzero(down_rule).tolist(),model_indices=np.flatnonzero(down_model).tolist()))
        for index in np.flatnonzero(up):
            entry=make_entry(f,int(index),e)
            if not entry["filled"]:
                rejections.append(dict(symbol=daily.symbol,day=daily.day,**entry));continue
            future=bars.iloc[index+1:].reset_index(drop=True)
            meta=dict(symbol=daily.symbol,day=daily.day,sector=daily.sector,
                period="post_training" if daily.day>"2026-08-26" else "retrospective_training_period",
                signal_at=entry["signal_at"],entry_at=entry["entry_at"],entry_price=entry["entry_price"],
                entry_session="premarket" if f.minute.iloc[index]<540 else "morning" if f.minute.iloc[index]<=630 else "afternoon")
            for name,mask,enabled in (("system_only",np.zeros(len(future),bool),False),
                  ("system_bearish_rule",down_rule[index+1:],True),
                  ("system_bearish_transfer_model",down_model[index+1:],True)):
                result=partial_trade(future,entry,e,mask,enabled)
                if sum(v["quantity"] for v in result["fills"])+result["remaining_quantity"]!=entry["quantity"]:
                    raise AssertionError("Shares disappeared")
                trades.append(dict(**meta,method=name,**result))
            break
        if n%100==0:
            atomic_json(target/"progress.json",dict(completed_stockdays=n,total=len(watch)))
            print(f"Replayed {n}/{len(watch)} selected stockdays",flush=True)
    f=pd.DataFrame(trades)
    if f.empty:raise ValueError("No entries")
    f.to_parquet(target/"trades.parquet",index=False)
    pd.DataFrame(coverage).to_parquet(target/"coverage.parquet",index=False)
    pd.DataFrame(rejections).to_parquet(target/"rejections.parquet",index=False)
    pd.DataFrame(events).to_parquet(target/"bearish-events.parquet",index=False)
    atomic_json(target/"minute-source-hashes.json",sources)
    atomic_json(target/"review-pool.json",review_pool)
    summaries={}
    for scope,mask in (("full_year",np.ones(len(f),bool)),("post_training",f.period.eq("post_training").to_numpy())):
        groups=f.loc[mask]
        summaries[scope]=dict(by_method={method:summarize(g.to_dict("records")) for method,g in groups.groupby("method")},
            by_sector={sector:{method:summarize(g.to_dict("records")) for method,g in sector_g.groupby("method")} for sector,sector_g in groups.groupby("sector")},
            by_entry_session={session:{method:summarize(g.to_dict("records")) for method,g in session_g.groupby("method")} for session,session_g in groups.groupby("entry_session")})
    report=dict(stockdays=len(watch),symbols=int(watch.symbol.nunique()),coverage=dict(
        complete_regular_days=sum(v["regular_bars"]==380 for v in coverage),
        full_premarket_days=sum(v["premarket_bars"]==50 for v in coverage),
        days_with_active_premarket=sum(v["premarket_active"]>0 for v in coverage)),
        rejections=dict(Counter(v["status"] for v in rejections)),summaries=summaries,
        bearish_accuracy_evaluated=False,dedicated_bearish_training=False,policy_active=False,reinforcement_learning=False)
    atomic_json(target/"evaluation.json",report)
    render(output,report)
    print(json.dumps(report["summaries"]["post_training"]["by_method"],ensure_ascii=False,indent=2))


def render(output,r):
    target=output/"replay"
    def table(groups):
        rows=[]
        for name,s in groups.items():
            if not s["entries"]:continue
            rows.append(f"<tr><td>{html.escape(name)}</td><td>{s['entries']}</td><td>{s['nonnegative_fraction']:.1%}</td>"
                f"<td>{s['mean_net_return']:.3%}</td><td>{s['tp3_any']} / {s['tp5_any']}</td><td>{s['bear_exit_triggered']}</td>"
                f"<td>{s['worst_loss']:.2%}</td><td>{s['residual']}</td><td>{s['ambiguous']}</td></tr>")
        return '<table><tr><th>청산 방식</th><th>진입</th><th>본절 이상</th><th>평균 순손익</th><th>3% / 5% 일부 체결</th><th>하락 신호 청산</th><th>최대 손실</th><th>잔량</th><th>봉내 순서 모호</th></tr>'+''.join(rows)+'</table>'
    rank=json.loads((output/"sector-ranking.json").read_text())
    sector_rows=''.join(f"<tr><td>{html.escape(v['sector'])}</td><td>{v['return_1y']:.1%}</td><td>{v['coverage']}</td></tr>" for v in rank['ranking'])
    comparisons=''.join(f'<h3>{html.escape(k)}</h3>'+table(v) for k,v in r['summaries']['post_training']['by_sector'].items())
    doc=f'''<!doctype html><meta charset="utf-8"><title>고정 익절·본절과 하락 뒤틀림 연구</title>
    <style>body{{font:16px/1.7 system-ui;max-width:1250px;margin:35px auto;padding:0 20px;color:#183044}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #ccd;padding:8px;text-align:left}}th{{background:#eef3f5}}</style>
    <h1>익절·본절은 시스템, 모델 청산 판단은 하락 구조</h1>
    <p>익절 목표와 본절 보호는 학습시키지 않는다. 3%에서 원수량 35%, 5%에서 추가 35%, 나머지 30%에 본절 보호와 하락 뒤틀림 청산을 적용한다. 비중은 기존 설정을 유지한 실험 가정이다. 목표는 매수 체결가 대비 비용 전 가격 상승률이고 표의 손익은 비용 후다. 첫 익절 실제 체결 후 모든 잔여 수량에 각 주의 비용 포함 본절을 적용한다. 정수 수량 3주 미만은 세 부분으로 나눌 수 없어 진입 제외한다.</p>
    <p>진입은 기존 상승 감지기·전일 기준봉/의지 대리·시크릿·MA20·유동성·강한 추세 조건을 유지하고, 실제 모의 진입 시각을 08:00–10:30 또는13:00–14:30으로 제한했다. 종목·거래일 첫 유효 진입을 세 청산 방식에 동일하게 적용한다. 30분 시간 제한을 없애고 15:19 관측 후 15:20까지 당일 청산하며 남은 수량은 미청산 실패로 기록한다. 계좌 현금과 종목 간 공동 거래량은 재생하지 않은 독립 사례다.</p>
    <p>하락 규칙은 상승 지지선 이탈→낮은 반등→재하락의 반대 구조를 가격 역수 변환으로 탐지한다. 전이 모델은 기존 상승 형태 분류기를 같은 변환 특징에 적용한 초안이다. <b>새 하락 정답으로 학습·검증한 모델은 아니다.</b> 수익 여부로 하락 정답을 만들지 않는다. 전이 점수는 보정된 하락 확률로 사용하지 않는다.</p>
    <h2>학습 기준일 이후 비교: 2026-08-27~10-28</h2>{table(r['summaries']['post_training']['by_method'])}
    <p>이미 공개된 날짜이며 섹터·구조 설계도 이 기간을 참고했다. 새 독립 검증이나 운영 승격 근거로 해석하지 않는다. 하락 신호 청산은 종가 관측 다음 봉부터 실행하고, 시스템 목표·기존 손절은 미리 놓인 주문으로 모의한다. 한 봉 안에서 손절과 익절이 모두 닿으면 손절을 우선한다. 3% 익절 직후 본절도 닿는 순서 모호 봉에서는 5% 익절 전에 본절을 우선하는 보수적 경로를 사용한다. 각 봉 거래량 1% 한도를 진입·모든 매도 주문이 공유한다. 호가 대기열과 실제 체결은 미검증이다.</p>
    <h2>과거 1년 전체: 회고적 재생</h2>{table(r['summaries']['full_year']['by_method'])}
    <p>모델은 2026-08-26까지의 일부 반도체 자료로 이미 학습되어 있다. 전체 연도 재생을 과거 시점마다 재학습한 walk-forward 성과로 해석하지 않는다.</p>
    <h2>학습 기준일 이후 섹터별</h2>{comparisons}
    <h2>최근 1년 대표종목 바스켓 탐색</h2><table><tr><th>API 섹터 분류</th><th>대표종목 가격 수익률</th><th>연간 이력 종목 수</th></tr>{sector_rows}</table>
    <p>39개 국내 상위 분류의 현재 시가총액 상위 5개 종목을 조사했다. 위 순위는 최소 3개의 연간 이력, 대표종목 중앙 거래대금 100억원 이상 범위다. 현재 대표종목을 시작 시점 동일 비중으로 보유한 수정주가 수익률이며 배당을 제외한다. 공식 업종 지수 성과가 아니며 과거 편입·상장폐지·현재 시총 선택 편향이 있다. API 분류는 겹친다. 반도체 밖 상위4개 바스켓의 대표종목으로 범위를 확대했다. D 일봉 선정에는 D+1 수익을 쓰지 않았다.</p>
    <p>선정 {r['stockdays']}개 종목·거래일, {r['symbols']}종목. 정규장 380봉 완비 {r['coverage']['complete_regular_days']}일, 프리마켓50봉 완비 {r['coverage']['full_premarket_days']}일, 프리마켓 실제 거래가 관측된 날 {r['coverage']['days_with_active_premarket']}일. 08:50–09:00 거래 공백은 봉을 만들지 않고 보유를 유지하며 특징은 재시작한다. <a href="https://nextrade.co.kr/main.do">NXT 거래시간</a>을 참조했다. 거래 가능한 과거 종목 목록 자체는 복원하지 못해 통합 시세의 실제 거래량을 유동성 근사로 사용한다.</p>
    <p><a href="evaluation.json">집계</a> · <a href="protocol.json">고정 규칙·모델 해시</a> · <a href="trades.parquet">주문별 모의 체결</a> · <a href="../sector-ranking.json">섹터 바스켓·원자료 해시</a> · <a href="bearish-events.parquet">하락 구조 신호</a></p>'''
    (target/"REPORT.html").write_text(doc)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=OUTPUT)
    args=parser.parse_args()
    replay(args.output)
