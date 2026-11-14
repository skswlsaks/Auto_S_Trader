"""Cost-inclusive escape study on frozen entries, without refitting the detector."""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from .config import atomic_json
from .execution import sell_tax_bps
from .features import regular_bars
from .liquid_research import OUTPUT, validate
from .pattern_eval import sha, wilson

TARGETS = (0., .001, .002)
POLICIES = [(f"net_{round(t*1000):d}permil_delay{d}", t, d)
            for d in (1, 2) for t in TARGETS] + [("stop_timeout", None, 1)]


def sell_factor(execution, at):
    impact = (execution["slippage_bps"] + execution["half_spread_bps"]) / 10000
    fee = (execution["sell_fee_bps"] + sell_tax_bps(execution, at)) / 10000
    return (1-impact)*(1-fee)


def escape_trade(future, entry, execution, target=0., delay=1):
    """Observe completed bars; sell from a later bar, carrying all partial fills.

    Whole-minute participation priced at that minute's impacted open is a proxy,
    not proof an order could fill at that price. High touches never trigger sales.
    """
    if delay < 1 or future.empty:
        raise ValueError("Need future bars and at least one-bar reaction")
    first_end = pd.Timestamp(entry["signal_at"]) + pd.Timedelta(minutes=1)
    if (future.timestamp.iloc[0] != first_end or
        not future.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all()):
        raise ValueError("Unknown execution path: missing minute")
    q, capital = entry["quantity"], entry["capital"]
    if q <= 0 or capital <= 0:
        raise ValueError("Need a frozen filled entry")
    impact = (execution["slippage_bps"] + execution["half_spread_bps"]) / 10000
    expected = future.open.iloc[0]*(1+impact)
    if not np.isclose(expected, entry["entry_price"], rtol=1e-12):
        raise ValueError("Entry source differs from frozen probe")
    factor = sell_factor(execution, future.timestamp.iloc[0])
    values = future[["open", "high", "low", "close", "volume"]].to_numpy(float)
    horizon = execution["max_holding_minutes"]
    left, proceeds, trigger, reason, complete_at, first_sale = q, 0., None, None, None, None
    target_triggered = False
    for i, (opening, high, low, close, volume) in enumerate(values):
        if trigger is not None and i >= trigger+delay and left:
            sold = min(left, int(np.floor(volume*execution["participation"])))
            if sold:
                proceeds += sold*opening*factor
                left -= sold
                if first_sale is None:
                    first_sale = i
                if not left:
                    complete_at = i
                    break
        if trigger is None:
            # A touched stop precedes a close-based target on the same bar.
            if low <= entry["stop_price"]:
                trigger, reason = i, "structural_stop"
            elif target is not None and close*q*factor/capital-1 >= target-1e-12:
                trigger, reason, target_triggered = i, "net_target", True
            elif i+1 >= horizon:
                trigger, reason = i, "timeout"
    mark = (proceeds+left*values[-1, 3]*factor)/capital-1
    net = proceeds/capital-1 if not left else None
    observed = future.timestamp.iloc[trigger].isoformat() if trigger is not None else None
    return dict(complete=not bool(left), remaining_quantity=left, trigger_reason=reason,
                target_triggered=target_triggered, trigger_at=observed,
                first_sale_at=future.timestamp.iloc[first_sale].isoformat() if first_sale is not None else None,
                complete_at=future.timestamp.iloc[complete_at].isoformat() if complete_at is not None else None,
                holding_bars=complete_at+1 if complete_at is not None else len(future),
                realized_net_return=net, marked_net_return=float(mark),
                net_nonnegative=bool(net is not None and net >= -1e-12),
                net_small_profit=bool(net is not None and net >= .001-1e-12))


def opportunity(future, entry, execution):
    """Price reachability is distinct from a causal, volume-limited exit."""
    f = future.iloc[:execution["max_holding_minutes"]]
    factor = sell_factor(execution, f.timestamp.iloc[0])
    scale = entry["quantity"]*factor/entry["capital"]
    high_net, close_net = f.high.to_numpy()*scale-1, f.close.to_numpy()*scale-1
    stopped = np.flatnonzero(f.low.to_numpy() <= entry["stop_price"])
    stop = int(stopped[0]) if len(stopped) else len(f)
    row = dict(cost_cover_raw_rise=1/scale/(entry["entry_price"]/(1+(execution["slippage_bps"]+execution["half_spread_bps"])/10000))-1,
               end30_net=float(close_net[-1]), max_high_net=float(high_net.max()))
    for t in TARGETS:
        key = f"net_{round(t*1000):d}permil"
        hits = np.flatnonzero(high_net >= t-1e-12)
        closes = np.flatnonzero(close_net >= t-1e-12)
        first = int(hits[0]) if len(hits) else len(f)
        row[key+"_high_any30"] = bool(len(hits))
        row[key+"_high_before_stop_lower"] = bool(first < stop)
        row[key+"_high_before_stop_upper"] = bool(first <= stop and len(hits))
        row[key+"_same_bar_stop_ambiguous"] = bool(first == stop and len(hits))
        row[key+"_close_before_stop"] = bool(len(closes) and closes[0] < stop)
        for minutes in (3, 5, 10, 30):
            row[key+f"_high_by{minutes}"] = bool(first < min(minutes, stop))
    row["bounce_then_nonpositive30"] = bool(high_net.max() >= .001 and close_net[-1] <= 0)
    return row


def summary(rows):
    n = len(rows)
    if not n:
        return dict(entries=0)
    success = sum(r["net_nonnegative"] for r in rows)
    small = sum(r["net_small_profit"] for r in rows)
    marked = np.array([r["marked_net_return"] for r in rows])
    days = sorted({r["day"] for r in rows})
    counts = np.array([[sum(r["net_nonnegative"] for r in rows if r["day"] == d),
                        sum(r["day"] == d for r in rows)] for d in days])
    rng = np.random.default_rng(20261030)
    sampled = counts[rng.integers(len(days), size=(2000, len(days)))].sum(axis=1)
    interval = np.quantile(sampled[:, 0]/sampled[:, 1], [.025, .975]).tolist()
    triggers = [r for r in rows if r["target_triggered"]]
    return dict(entries=n, complete=sum(r["complete"] for r in rows), residual=sum(not r["complete"] for r in rows),
                net_nonnegative=success, nonnegative_fraction=success/n,
                nonnegative_wilson=wilson(success,n), nonnegative_day_bootstrap_95=interval,
                net_small_profit=small, small_profit_fraction=small/n,
                target_triggers=len(triggers), target_trigger_but_net_loss_or_residual=sum(not r["net_nonnegative"] for r in triggers),
                target_conditional_nonnegative_fraction=sum(r["net_nonnegative"] for r in triggers)/len(triggers) if triggers else None,
                reasons=dict(Counter(r["trigger_reason"] for r in rows)),
                mean_marked_net_return=float(marked.mean()), median_marked_net_return=float(np.median(marked)),
                loss_p95=float(np.quantile(np.maximum(-marked,0),.95)), worst_loss=float(max(0,-marked.min())),
                total_marked_pnl_independent=float(sum(r["capital"]*r["marked_net_return"] for r in rows)),
                median_holding_bars=float(np.median([r["holding_bars"] for r in rows])))


def opportunity_summary(rows):
    n = len(rows)
    if not n:
        return dict(entries=0)
    fields = [k for k,v in rows[0].items() if isinstance(v,bool)]
    bounce = [r for r in rows if r["bounce_then_nonpositive30"]]
    return dict(entries=n, fractions={k:sum(r[k] for r in rows)/n for k in fields},
                counts={k:sum(r[k] for r in rows) for k in fields},
                median_raw_rise_to_cost_cover=float(np.median([r["cost_cover_raw_rise"] for r in rows])),
                bounce_then_nonpositive30=len(bounce))


def run(source, output):
    if output.exists():
        raise ValueError("Use a new output directory; prior studies are immutable")
    m = validate(source)
    replay = json.loads((source/"heldout-replay.json").read_text())
    if replay["experiment_manifest_sha256"] != sha(source/"manifest.json"):
        raise ValueError("Replay source mismatch")
    probes = [p for p in json.loads((source/"heldout-probes.json").read_text()) if p["reaction_bars"] == 1]
    blind = json.loads((source/"blind-test/stop-probes.json").read_text())
    samples = {s["id"]:s for s in json.loads((source/"blind-test/manifest.json").read_text())["samples"]}
    false_keys = {(p["method"], samples[p["id"]]["symbol"],p["signal_at"]) for p in blind
                  if p["reaction_bars"] == 1 and p["visual_false_alarm"]}
    execution = m["execution"]
    output.mkdir(parents=True)
    protocol = dict(targets_net=TARGETS, policies=POLICIES, source_manifest_sha256=sha(source/"manifest.json"),
        probes_sha256=sha(source/"heldout-probes.json"), replay_sha256=sha(source/"heldout-replay.json"),
        blind_probes_sha256=sha(source/"blind-test/stop-probes.json"),
        annotations_sha256=sha(source/"blind-test/annotations-before-unblinding.json"),
        module_sha256=sha(Path(__file__)), execution=execution,
        scope="Exploratory exit study on already exposed post-training dates; no threshold search, no new independent test",
        observation="Completed bar low observes stop; completed close triggers net target; stop first on conflict",
        execution_rule="Frozen entries; sell later minute at impacted open, capped at 1% whole-minute volume; partials carried to session end",
        timeout="Observe 30th bar then exit from later bar; no same-bar hindsight sale; end-session residual is failure",
        opportunity="High touches are unexecuted opportunity only; same-bar high/stop ambiguity reported as bounds",
        dependence="Independent overlapping signal probes, not a portfolio. Day-block bootstrap; first-entry-per-stock-day sensitivity",
        policy_active=False, reinforcement_learning=False, minute_source_hashes=replay["minute_source_hashes"])
    atomic_json(output/"protocol.json",protocol)
    results, opportunities = [], []
    by_symbol = {}
    for p in probes:
        if p["filled"]:
            by_symbol.setdefault(p["symbol"],[]).append(p)
    for number,(symbol,items) in enumerate(sorted(by_symbol.items()),1):
        path = next(Path(p) for p in replay["minute_source_hashes"] if Path(p).name == f"{symbol}-1m-raw.parquet")
        if sha(path) != replay["minute_source_hashes"][str(path)]:
            raise ValueError("Minute data changed")
        bars = regular_bars(pd.read_parquet(path))
        days = {d:g.reset_index(drop=True) for d,g in bars.groupby("day") if d > "2026-08-26"}
        for p in items:
            day = days[p["day"]]
            future = day.loc[day.timestamp > pd.Timestamp(p["signal_at"])].reset_index(drop=True)
            meta = {k:p[k] for k in ("method","symbol","day","signal_at","capital","quantity")}
            meta["visual_false_alarm"] = (p["method"],symbol,p["signal_at"]) in false_keys
            opportunities.append({**meta,**opportunity(future,p,execution)})
            for name,target,delay in POLICIES:
                results.append({**meta,"policy":name,**escape_trade(future,p,execution,target,delay)})
        if number % 10 == 0:
            print(f"Read and verified {number}/{len(by_symbol)} symbols",flush=True)
    methods = sorted({p["method"] for p in probes})
    report = dict(scope=protocol["scope"], coverage={}, policies={}, opportunities={}, false_alarm_policies={},
                  first_stockday_policies={}, policy_active=False, reinforcement_learning=False)
    for method in methods:
        selected = [p for p in probes if p["method"] == method]
        report["coverage"][method] = dict(signals=len(selected), filled=sum(p["filled"] for p in selected),
                                        rejected=dict(Counter(p["status"] for p in selected if not p["filled"])))
        opp = [r for r in opportunities if r["method"] == method]
        report["opportunities"][method] = opportunity_summary(opp)
        report["policies"][method],report["false_alarm_policies"][method],report["first_stockday_policies"][method] = {},{},{}
        first = {(r["symbol"],r["day"]):r["signal_at"] for r in sorted(opp,key=lambda r:r["signal_at"],reverse=True)}
        for policy,_,_ in POLICIES:
            rows = [r for r in results if r["method"] == method and r["policy"] == policy]
            report["policies"][method][policy] = summary(rows)
            report["false_alarm_policies"][method][policy] = summary([r for r in rows if r["visual_false_alarm"]])
            report["first_stockday_policies"][method][policy] = summary([r for r in rows if first[(r["symbol"],r["day"])] == r["signal_at"]])
    pd.DataFrame(results).to_parquet(output/"trades.parquet",index=False)
    pd.DataFrame(opportunities).to_parquet(output/"opportunities.parquet",index=False)
    atomic_json(output/"evaluation.json",report)
    render(output,report)
    print(json.dumps(report["policies"]["retrained_filtered"],ensure_ascii=False,indent=2))


def render(output, report):
    method = "retrained_filtered"
    def table(groups):
        rows = []
        for name,s in groups.items():
            if not s["entries"]:
                continue
            ci = s["nonnegative_day_bootstrap_95"]
            rows.append(f"<tr><td>{html.escape(name)}</td><td>{s['net_nonnegative']}/{s['entries']} ({s['nonnegative_fraction']:.1%})</td>"
                        f"<td>{ci[0]:.1%}–{ci[1]:.1%}</td><td>{s['small_profit_fraction']:.1%}</td>"
                        f"<td>{s['mean_marked_net_return']:.3%}</td><td>{s['loss_p95']:.2%} / {s['worst_loss']:.2%}</td>"
                        f"<td>{s['residual']}</td><td>{s['target_triggers']} / {s['target_trigger_but_net_loss_or_residual']}</td></tr>")
        return "<table><tr><th>규칙</th><th>비용 포함 본절 이상</th><th>날짜 묶음 95% 구간</th><th>순익 ≥0.1%</th><th>평균 순손익</th><th>손실 p95 / 최대</th><th>잔량</th><th>목표 발동 / 이후 손실·잔량</th></tr>"+"".join(rows)+"</table>"
    opportunity_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v:.1%}</td></tr>" for k,v in report["opportunities"][method]["fractions"].items())
    doc = f"""<!doctype html><meta charset="utf-8"><title>뒤틀림 본절·약익절 청산 연구</title>
    <style>body{{font:16px/1.65 system-ui;max-width:1250px;margin:35px auto;padding:0 20px;color:#183044}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #cad5dd;padding:8px;text-align:left}}th{{background:#e9f0f4}}code{{background:#eef3f5}}a{{color:#075e9c}}</style>
    <h1>뒤틀림 이후 본절·약익절로 나올 수 있는가</h1>
    <p>형태가 맞아도 이후 하락할 수 있다. 이번 목표는 감지 이후의 비용 포함 청산 확률이다. 재학습 없이 기존 진입을 고정했다. 사후 공개된 기간에 대한 탐색이며 새 독립 검증이 아니다.</p>
    <p>모델 학습 이후 2026-08-27~2026-10-28의 유동성·강한 추세 선정 범위. 새 모델 신호 {report['coverage'][method]['signals']:,}건 중 진입 {report['coverage'][method]['filled']:,}건이 분모다. 가격 위험 초과·잔여 시간·진입 거래량 부족은 진입 제외로 별도 저장한다. 표본은 겹치는 독립 신호 모의이며 계좌 승률이 아니다.</p>
    <h2>규칙과 비용</h2><p>net_0permil은 비용 회수, net_1permil은 순익 +0.1%, net_2permil은 +0.2%다. 봉 마감 시 해당 순손익 가격을 넘으면 다음 봉부터 전량 청산을 시도한다. delay2는 한 봉 추가 지연이다. 고가만 닿으면 팔았다고 인정하지 않는다. 구조 저점 손절을 먼저 관측하면 손절하며, 목표·손절 미발동도 30번째 봉 마감 뒤 청산한다. 각 매도 봉 거래량 1% 한도로 부분 청산하고 장 종료 잔량은 성공에서 제외한다.</p>
    <p>왕복 수수료·매도세금·양방향 7bp 충격 가정에서 매수 원시 시가 대비 비용 회수에 필요한 상승은 {report['opportunities'][method]['median_raw_rise_to_cost_cover']:.3%}다. 호가·주문 순서·실제 계좌 비용은 검증되지 않았다. 분 전체 거래량을 그 봉 시가에서 매도 가능한 용량으로 쓰는 근사이므로 실체결률이 아니다.</p>
    <h2>재학습 모델: 고정 진입 전체</h2>{table(report['policies'][method])}
    <p>본절 성공 분모는 모든 진입이다. 목표에 도달한 사례만 고른 조건부 확률과 다르다. 평균은 잔량을 장 종료 가격·청산 비용으로 평가한 독립 신호 평균이다. 날짜별 상관을 고려한 bootstrap 2,000회 구간이며 겹치는 신호·같은 종목의 의존성을 모두 해결한 미래 확률 구간은 아니다.</p>
    <h2>종목·거래일마다 첫 진입만 남긴 민감도</h2>{table(report['first_stockday_policies'][method])}
    <h2>기존 분류기 + 동일 유동성 조건</h2>{table(report['policies']['prior_filtered'])}
    <h2>기존 블라인드 판독에서 형태 오탐으로 분류된 진입</h2>{table(report['false_alarm_policies'][method])}
    <p>이 작은 부분표본은 AI 잠정 형태 판독이다. 이후 수익·손실로 형태 정답을 수정하지 않았다.</p>
    <h2>가격 도달 기회: 체결 성공과 구분</h2><table>{opportunity_rows}</table>
    <p>high_before_stop_lower는 손절 접촉 봉 이전에 고가 도달, upper는 같은 봉 도달도 포함한다. 같은 봉에서 고가와 저가 중 무엇이 먼저인지 알 수 없어 범위로 남긴다. high_byN은 첫 손절 접촉 봉을 제외하고 N분 내 도달한 비율이다. close_before_stop은 손절 전에 봉 종가도 순손익 기준에 도달한 경우다. bounce_then_nonpositive30은 고가 기준 순익 +0.1% 기회가 있었지만 30분 끝의 순청산 가치가 본절 이하인 사후 설명이다.</p>
    <p>현재 기간에서 가장 좋은 규칙을 골라 미래 확률이라고 주장하지 않는다. 확정 규칙은 이후 새 거래일의 모의 체결로 재검증하고, 호가·체결 자료로 지연과 충격을 보정해야 한다. 강화학습·실주문은 연결하지 않았다.</p>
    <p><a href="evaluation.json">집계 JSON</a> · <a href="protocol.json">규칙·원본 해시</a> · <a href="trades.parquet">모의 청산 전체</a> · <a href="opportunities.parquet">가격 기회 전체</a></p>"""
    (output/"REPORT.html").write_text(doc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=OUTPUT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source,args.output)
