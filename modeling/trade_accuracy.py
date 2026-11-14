"""User-defined accuracy: completely liquidated trades whose net PnL is >= zero."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import atomic_json
from .pattern_eval import sha, wilson
from .sector_study import OUTPUT

NAMES={"system_only":"고정 익절·본절만", "system_bearish_rule":"고정 주문 + 하락 구조 규칙",
       "system_bearish_transfer_model":"고정 주문 + 하락 전이 모델"}
TOLERANCE=1e-12


def measure(frame):
    """Residual positions stay in the denominator and never count as successes."""
    n=len(frame)
    if not n:return dict(entries=0,successes=0,accuracy=None)
    if frame.complete.isna().any():raise ValueError("Completion state unknown")
    done=frame.complete.astype(bool)
    net=pd.to_numeric(frame.realized_net_return,errors="raise")
    if not np.isfinite(net.loc[done]).all():raise ValueError("Completed trade missing realized net return")
    if (frame.remaining_quantity.lt(0) | (done & frame.remaining_quantity.ne(0)) | (~done & frame.remaining_quantity.eq(0))).any():
        raise ValueError("Completion state and shares disagree")
    profit=done & net.gt(TOLERANCE)
    flat=done & net.abs().le(TOLERANCE)
    loss=done & net.lt(-TOLERANCE)
    success=profit|flat
    if int(profit.sum()+flat.sum()+loss.sum()+(~done).sum())!=n:raise AssertionError("Outcome categories must partition entries")
    groups=pd.DataFrame(dict(day=frame.day.to_numpy(),success=success.to_numpy())).groupby("day").success.agg(["sum","size"]).to_numpy()
    rng=np.random.default_rng(20261030)
    draw=groups[rng.integers(len(groups),size=(2000,len(groups)))].sum(axis=1)
    interval=np.quantile(draw[:,0]/draw[:,1],[.025,.975]).tolist()
    return dict(entries=n,successes=int(success.sum()),accuracy=float(success.mean()),
        profitable=int(profit.sum()),flat=int(flat.sum()),losses=int(loss.sum()),unclosed=int((~done).sum()),
        closed=int(done.sum()),accuracy_among_closed=float(success.sum()/done.sum()) if done.any() else None,
        wilson_95=wilson(int(success.sum()),n),day_bootstrap_95=interval,
        mean_realized_return_closed=float(net.loc[done].mean()) if done.any() else None,
        mean_marked_return_all=float(frame.marked_net_return.mean()))


def run(source,output):
    if output.exists():raise ValueError("Use a new accuracy output; previous records are immutable")
    path=source/"replay/trades.parquet"
    frame=pd.read_parquet(path)
    if frame.duplicated(["symbol","day","method"]).any():raise ValueError("Expected one entry per stock/day/method")
    original=json.loads((source/"replay/evaluation.json").read_text())
    audit=json.loads((source/"replay/entry-rule-audit.json").read_text())
    if sha(path)!=audit["sources"]["trades"]:raise ValueError("Audited entries changed")
    definition=dict(name="매매 정확도",version="trade-accuracy-v1",as_of="2026-10-30",
        numerator="Fully liquidated entries with aggregate net proceeds (including all partial exits) >= initial capital including buy fees",
        denominator="All actually filled entries in the same cohort; incomplete exits remain failures in the primary statistic",
        costs="Buy/sell commission, dated sell tax, spread/slippage assumptions already included in frozen simulation",
        not_executed="Price merely touching break-even, unfinished profitable marks, or a profitable partial exit do not suffice",
        tolerance=TOLERANCE,tolerance_note="Floating-point zero only; no practical loss band treated as break-even",
        interpretation="Trade outcome success rate; shape precision/recall remains separately defined, no new profit-prediction training target",
        cancelled_instruction="Do not add the immediately retracted blanket entry exclusion below MA20; existing study filters are recorded separately",
        sources={"trades":sha(path),"evaluation":sha(source/"replay/evaluation.json"),
                 "entry_audit":sha(source/"replay/entry-rule-audit.json"),"module":sha(Path(__file__))})
    stats={}
    for scope,part in (("full_year",frame),("post_training",frame.loc[frame.period.eq("post_training")])):
        stats[scope]=dict(by_method={},by_sector={},by_entry_session={})
        for method,g in part.groupby("method"):
            measured=measure(g)
            previous=original["summaries"][scope]["by_method"][method]
            assert measured["entries"]==previous["entries"] and measured["successes"]==previous["nonnegative"]
            assert np.array_equal(g.net_nonnegative.to_numpy(),(g.complete & g.realized_net_return.ge(-TOLERANCE)).to_numpy())
            stats[scope]["by_method"][method]=measured
        for name,col in (("by_sector","sector"),("by_entry_session","entry_session")):
            for group,g in part.groupby(col):
                stats[scope][name][group]={m:measure(v) for m,v in g.groupby("method")}
        pivot=part.pivot(index=["symbol","day"],columns="method",values="net_nonnegative")
        assert pivot.notna().all().all()
        stats[scope]["paired_vs_system"]={method:dict(
            rescued=int((~pivot.system_only & pivot[method]).sum()),
            lost_previous_success=int((pivot.system_only & ~pivot[method]).sum()),
            both_succeeded=int((pivot.system_only & pivot[method]).sum()),
            both_failed=int((~pivot.system_only & ~pivot[method]).sum()))
            for method in NAMES if method!="system_only"}
    output.mkdir(parents=True)
    atomic_json(output/"definition.json",definition)
    atomic_json(output/"statistics.json",dict(statistics=stats,entry_rule_audit=audit,
        limits=["Current implementation includes proxy and experimental entry rules; NOT full reproduction of user entry rules",
               "First fillable entry per stock/day; identical paired cohorts, not cash-constrained account accuracy",
               "Full year is retrospective; post-training dates already exposed; current-sector/leader selection is ex-post",
               "Assumed OHLCV fills, no order book; dedicated bearish-label training and shape evaluation pending"],
        model_profit_training=False,policy_active=False))
    frame[["symbol","day","sector","period","entry_session","method","complete","remaining_quantity",
           "realized_net_return","marked_net_return","net_nonnegative"]].to_parquet(output/"outcomes.parquet",index=False)
    render(output,stats)
    print(json.dumps(stats["post_training"]["by_method"],ensure_ascii=False,indent=2))


def render(output,stats):
    def table(groups):
        rows=[]
        for method in NAMES:
            s=groups.get(method)
            if not s or not s["entries"]:continue
            ci=s["day_bootstrap_95"]
            rows.append(f"<tr><td>{NAMES[method]}</td><td>{s['successes']}/{s['entries']}</td><td><b>{s['accuracy']:.1%}</b></td>"
                f"<td>{s['profitable']} / {s['flat']}</td><td>{s['losses']}</td><td>{s['unclosed']}</td>"
                f"<td>{ci[0]:.1%}–{ci[1]:.1%}</td><td>{s['mean_marked_return_all']:.3%}</td></tr>")
        return '<table><tr><th>清算 방식</th><th>본절 이상 / 전체 진입</th><th>정확도</th><th>수익 / 정확한 본절</th><th>손실</th><th>미청산</th><th>날짜 묶음95% 구간</th><th>평균 순손익</th></tr>'+''.join(rows)+'</table>'
    sectors=''.join(f'<h3>{html.escape(name)}</h3>'+table(group) for name,group in stats['post_training']['by_sector'].items())
    sessions=''.join(f'<h3>{html.escape(name)}</h3>'+table(group) for name,group in stats['post_training']['by_entry_session'].items())
    doc=f'''<!doctype html><meta charset="utf-8"><title>본절 이상 청산 기준 매매 정확도</title>
    <style>body{{font:16px/1.7 system-ui;max-width:1250px;margin:35px auto;padding:0 20px;color:#183044}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #ccd;padding:8px;text-align:left}}th{{background:#eef3f5}}</style>
    <h1>매매 정확도 = 비용 포함 본절 이상으로 청산한 거래 / 전체 진입</h1>
    <p>사용자의 2026-10-30 정의를 적용했다. 3%·5% 부분 익절과 잔량 청산을 모두 합한 최종 순손익이 0 이상이고 전량 청산이 완료된 경우를 성공으로 센다. 잠깐 본절 가격에 닿거나 일부 물량만 수익으로 매도한 경우는 성공을 확정하지 않는다. 미청산은 전체 진입 분모에 남기고 성공에서 제외한다. 부동소수점 오차 1e-12만 허용하며 작은 손실을 본절로 반올림하지 않는다.</p>
    <p><b>현재 구현 기준의 통계다.</b> 전일 기준봉·시크릿/20일선 유지 또는 의지 대리 조건으로 선정한 뒤 상승 감지기·유동성/추세·시간대 조건으로 진입했다. 진입 직전 시크릿/포인트 유지 확인과 강한 의지 정의는 완전히 재현하지 않았다. 즉 정리한 사용자 규칙을 전부 엄격 적용한 성능은 아직 별도 재생이 필요하다. 방금 취소한 '20일선 아래 무조건 제외' 조건을 새로 추가하지 않았다.</p>
    <h2>학습 기준일 이후: 2026-08-27~2026-10-28, 동일 진입364건</h2>{table(stats['post_training']['by_method'])}
    <p>모델은 2026-08-26까지의 자료로 학습했다. 이후 날짜도 이미 공개된 연구 기간이며 섹터와 현재 대표종목을 사후 선택했다. 독립 최종 검증이나 미래 정확도 보장이 아니다. 날짜 묶음 bootstrap2,000회 구간은 관측 표본의 의존성을 일부 반영한 설명용 구간이다.</p>
    <h2>과거1년 전체: 동일 진입2,589건</h2>{table(stats['full_year']['by_method'])}
    <p>전체 연도는 이미 학습된 모델을 과거에 적용한 회고적 재생이다. 과거 시점마다 모델을 재학습한 walk-forward 통계가 아니다. 종목마다 첫 유효 진입 하나를 세 청산 방식에 동일하게 사용했으며 종목 간 현금·동시 보유 한도를 합산하지 않았다.</p>
    <h2>학습 기준일 이후 섹터별</h2>{sectors}<h2>진입 시간대별</h2>{sessions}
    <p>정확도는 매매 결과 평가 지표로 기록한다. 하락 형태의 정답·재현율·오탐과 분리해 보존하며 본절 결과를 새 하락 정답으로 복제하거나 익절 판단을 모델에 학습시키지 않는다. 하락 전이 모델은 전용 하락 라벨로 새로 학습한 모델이 아니다.</p>
    <p><a href="definition.json">지표 정의·원본 해시</a> · <a href="statistics.json">전체 통계·진입 규칙 대조</a> · <a href="outcomes.parquet">거래별 성공/실패</a> · <a href="../replay/REPORT.html">고정 주문·하락 탐지 실험</a></p>'''
    (output/"REPORT.html").write_text(doc.replace('清算 방식','청산 방식'))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=OUTPUT)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    run(args.source,args.output)
