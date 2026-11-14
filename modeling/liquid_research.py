"""Retrain inside a liquid, strong daily trend cohort; audit detection and stop exits separately."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .config import atomic_json, load_config
from .features import contract_hashes, regular_bars
from .liquid_trend import CONTEXT_FEATURES, PARAMETERS, alarms, contract as liquid_contract
from .liquid_trend import daily_context, exit_summary, intraday_context, stop_probe
from .pattern_eval import draw, match_events, sha
from .shape_experiment import AUDIT, DEVELOP, ROOT, WATCH, aggregate, day_bars, event_metrics, load_labels
from .shape_v2 import SHAPE_FEATURES, contract as shape_contract, detect

OUTPUT = ROOT/"outputs/model-research/liquid-trend/20261030-v1"
FEATURES = SHAPE_FEATURES+CONTEXT_FEATURES
PRIOR_AUDITS = [AUDIT, ROOT/"outputs/model-research/pattern-audit/20261030-v1",
                ROOT/"outputs/model-research/shape-v2/20261030-blind-v1"]


def fit(output):
    if output.exists():
        raise ValueError("Choose a new experiment directory")
    c = load_config()
    data = Path(c["data_dir"])
    original = json.loads((DEVELOP/"manifest.json").read_text())
    if original["shape_contract"] != shape_contract() or original["v1_contract"] != contract_hashes() or sha(WATCH) != original["watchlist_sha256"]:
        raise ValueError("Frozen source changed")
    symbols = json.loads((data/"universe.json").read_text())["symbols"]
    contexts, sources = [], {}
    for symbol in symbols:
        paths = [data/"candles"/f"{symbol}-1d-{name}.parquet" for name in ("adjusted", "raw")]
        f = daily_context(*(pd.read_parquet(p) for p in paths))
        f["symbol"] = symbol
        contexts.append(f)
        sources.update({str(p): sha(p) for p in paths})
    watch = pd.read_parquet(WATCH).merge(pd.concat(contexts, ignore_index=True), on=["symbol", "selection_day"], how="left", validate="many_to_one")
    watch["focus_selected"] = watch.selected & watch.daily_focus.fillna(False)
    focused = watch.loc[watch.focus_selected].copy()
    labels = pd.read_parquet(DEVELOP/"development-bars.parquet")
    labels = labels.loc[labels.event_confirmation_target.notna() & labels.day.le(PARAMETERS["training_cutoff"])]
    for _, g in labels.groupby(["symbol", "timestamp"]):
        if g.event_confirmation_target.nunique() != 1:
            raise ValueError("Conflicting labels")
    labels = labels.drop_duplicates(["symbol", "timestamp"])
    frames, checks = [], 0
    for (symbol, day), group in labels.groupby(["symbol", "day"]):
        daily = focused.loc[focused.symbol.eq(symbol) & focused.day.eq(day)]
        if daily.empty:
            continue
        bars, path = day_bars(symbol, day)
        sources[str(path)] = sha(path)
        frame = intraday_context(detect(bars, symbol), daily.iloc[0], c["execution"])
        cut = group.timestamp.max()
        prefix = intraday_context(detect(bars.loc[bars.timestamp <= cut], symbol), daily.iloc[0], c["execution"])
        pd.testing.assert_frame_equal(frame.loc[frame.timestamp <= cut].reset_index(drop=True), prefix)
        checks += 1
        selected = group[["timestamp", "sample_id", "event_confirmation_target", "label_source"]].merge(frame, on="timestamp", validate="one_to_one")
        selected["symbol"] = symbol
        frames.append(selected)
    training = pd.concat(frames, ignore_index=True)
    if training.event_confirmation_target.nunique() != 2:
        raise ValueError("Focused labels need positives and negatives")
    model = make_pipeline(SimpleImputer(strategy="constant", fill_value=0, add_indicator=True, keep_empty_features=True),
                          StandardScaler(), LogisticRegression(C=.1, class_weight="balanced", max_iter=1000, random_state=42))
    model.fit(training[FEATURES], training.event_confirmation_target.astype(int))
    output.mkdir(parents=True)
    watch.to_parquet(output/"watchlist.parquet", index=False)
    training.to_parquet(output/"training.parquet", index=False)
    joblib.dump(model, output/"model.joblib")
    event_source = json.loads((AUDIT/"annotations.json").read_text())
    event_samples = {a["id"] for a in event_source["samples"] if a["events"] and a["status"] == "clear"}
    summary = dict(prior_selected_stock_days=int(watch.selected.sum()), focused_stock_days=len(focused),
                   focused_symbols=int(focused.symbol.nunique()), training_rows=len(training),
                   positive_training_rows=int(training.event_confirmation_target.sum()),
                   positive_training_events=len(set(training.sample_id)&event_samples),
                   training_stock_days=training[["symbol", "day"]].drop_duplicates().shape[0],
                   validation_positive_events=len(set(training.loc[training.day.gt("2026-06-30"), "sample_id"])&event_samples),
                   threshold_searched=False, prefix_checks=checks, stage_model_trained=False, policy_active=False)
    atomic_json(output/"fit.json", summary)
    atomic_json(output/"manifest.json", dict(liquid_contract=liquid_contract(), shape_contract=shape_contract(),
        v1_contract=contract_hashes(), features=FEATURES, parameters=PARAMETERS, execution=c["execution"],
        original_watchlist=str(WATCH), original_watchlist_sha256=sha(WATCH), watchlist_sha256=sha(output/"watchlist.parquet"),
        training_sha256=sha(output/"training.parquet"), model_sha256=sha(output/"model.joblib"),
        prior_model_sha256=sha(DEVELOP/"confirmation-model.joblib"),
        source_development_sha256=sha(DEVELOP/"manifest.json"),
        source_annotations_sha256=sha(AUDIT/"annotations.json"), source_hashes=sources,
        trained_symbols=sorted(training.symbol.unique().tolist()), prior_audits={str(p):sha(p/"manifest.json") for p in PRIOR_AUDITS},
        label_source="AI provisional; 0..2 bars after confirmation within reviewed interval", policy_active=False,
        stop_assumptions="Minute-close stop observation; next-bar exits at 1% of whole-minute volume; no order-book fill guarantee"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def validate(output):
    m = json.loads((output/"manifest.json").read_text())
    if (m["liquid_contract"] != liquid_contract() or m["shape_contract"] != shape_contract() or m["v1_contract"] != contract_hashes() or
        m["model_sha256"] != sha(output/"model.joblib") or m["watchlist_sha256"] != sha(output/"watchlist.parquet") or
        m["training_sha256"] != sha(output/"training.parquet") or m["prior_model_sha256"] != sha(DEVELOP/"confirmation-model.joblib")):
        raise ValueError("Frozen experiment changed")
    return m


def prepare(output, count=32, seed=20261033):
    m = validate(output)
    test = output/"blind-test"
    if test.exists():
        raise ValueError("Test sample is frozen")
    excluded = set()
    for path, expected in m["prior_audits"].items():
        path = Path(path)
        if sha(path/"manifest.json") != expected:
            raise ValueError("Prior audit changed")
        excluded.update((s["symbol"], s["day"]) for s in json.loads((path/"manifest.json").read_text())["samples"])
    watch = pd.read_parquet(output/"watchlist.parquet")
    available = watch.loc[watch.focus_selected & watch.day.gt(PARAMETERS["training_cutoff"]) & ~watch.symbol.isin(m["trained_symbols"])].sort_values(["day", "symbol"])
    available = available.loc[[(s, d) not in excluded for s, d in zip(available.symbol, available.day)]].reset_index(drop=True)
    rng, samples, sources = np.random.default_rng(seed), [], {}
    for choice in rng.permutation(len(available)):
        row = available.iloc[choice]
        bars, path = day_bars(row.symbol, row.day)
        start = int(rng.integers(120, 321))
        shown = bars.iloc[start-120:start+60].reset_index(drop=True)
        if len(shown) != 180 or not shown.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all():
            continue
        sid = f"L{len(samples)+1:03}"
        file = test/"bars"/f"{sid}.parquet"
        file.parent.mkdir(parents=True, exist_ok=True)
        shown.to_parquet(file, index=False)
        draw(shown, sid, test/"blind"/f"{sid}.png", 120, 179)
        sources[str(path)] = sha(path)
        samples.append(dict(id=sid, symbol=row.symbol, day=row.day, score_start=120, score_end=179,
                            bars_sha256=sha(file), timestamps=[t.isoformat() for t in shown.timestamp]))
        if len(samples) == count:
            break
    if len(samples) != count:
        raise ValueError("Not enough complete windows")
    atomic_json(test/"manifest.json", dict(protocol={"version":"liquid-trend-shape-stop-v1",
        "shape":"Same lecture shape criteria; >=10-bar structure; reversal or flat resistance/rising lows; breakout/pullback/rebound; no return labels",
        "matching":"One-to-one +/-5 minutes; references and alarms scored in core125..174; 50 min/window",
        "sampling":"D-close liquid strong-trend focus only; no conditioning on intraday gate, alarms or future stop outcome",
        "test_scope":"Dates after training cutoff, symbols absent from focused training, previously reviewed stock-days excluded",
        "limits":"AI provisional labels; earlier structure design used October examples; no wholly untouched calendar-period claim"},
        experiment_manifest_sha256=sha(output/"manifest.json"), seed=seed, source_hashes=sources, samples=samples))
    atomic_json(test/"annotations-template.json", dict(protocol="liquid-trend-shape-stop-v1", manifest_sha256=sha(test/"manifest.json"),
        samples=[dict(id=s["id"],status="pending",category="",reason="",events=[]) for s in samples]))
    print(json.dumps(dict(windows=len(samples),predictions_hidden=True,models_and_rules_frozen=True)))


def evaluate(output):
    m = validate(output)
    test = output/"blind-test"
    tm, labels = load_labels(test)
    if tm["experiment_manifest_sha256"] != sha(output/"manifest.json"):
        raise ValueError("Test belongs to a different model")
    frozen = test/"annotations-before-unblinding.json"
    if frozen.exists() and sha(frozen) != sha(test/"annotations.json"):
        raise ValueError("Labels changed after unblinding")
    if not frozen.exists():
        frozen.write_bytes((test/"annotations.json").read_bytes())
    watch = pd.read_parquet(output/"watchlist.parquet").set_index(["symbol", "day"])
    prior = joblib.load(DEVELOP/"confirmation-model.joblib")
    model = joblib.load(output/"model.joblib")
    details, probes, frames, checks, eligible_refs, refs_total = [], [], [], 0, 0, 0
    names = ("prior_raw", "prior_filtered", "retrained_filtered")
    for s in tm["samples"]:
        daily = watch.loc[(s["symbol"], s["day"])]
        if not daily.focus_selected or s["symbol"] in m["trained_symbols"] or s["day"] <= PARAMETERS["training_cutoff"]:
            raise ValueError("Test outside held-out focus cohort")
        bars, path = day_bars(s["symbol"], s["day"])
        if sha(path) != tm["source_hashes"][str(path)]:
            raise ValueError("Minute source changed")
        f = intraday_context(detect(bars, s["symbol"]), daily, m["execution"])
        old_score, score = prior.predict_proba(f[SHAPE_FEATURES])[:, 1], model.predict_proba(f[FEATURES])[:, 1]
        signals = dict(prior_raw=alarms(old_score,f,False),prior_filtered=alarms(old_score,f,True),retrained_filtered=alarms(score,f,True))
        for cut in (s["timestamps"][120],s["timestamps"][179]):
            cut = pd.Timestamp(cut)
            part = intraday_context(detect(bars.loc[bars.timestamp.le(cut)], s["symbol"]), daily, m["execution"])
            pd.testing.assert_frame_equal(f.loc[f.timestamp.le(cut)].reset_index(drop=True),part)
            part_score = model.predict_proba(part[FEATURES])[:,1]
            np.testing.assert_allclose(score[:len(part)], part_score,rtol=0,atol=1e-12)
            np.testing.assert_array_equal(signals["retrained_filtered"][:len(part)],alarms(part_score,part,True))
            checks += 1
        f["prior_score"],f["score"] = old_score,score
        for name,v in signals.items():f[name]=v
        f["sample_id"],f["symbol"],f["dataset_role"] = s["id"],s["symbol"],"held_out_test"
        frames.append(f)
        a = labels[s["id"]]
        if a["status"] == "uncertain":continue
        refs = [e["confirmation"] for e in a["events"]]
        ix = {pd.Timestamp(t):i for i,t in enumerate(s["timestamps"])}
        ix_full = {t:i for i,t in enumerate(f.timestamp)}
        refs_total += sum(125<=r<=174 for r in refs)
        eligible_refs += sum(125<=r<=174 and f.minute_eligible.iloc[ix_full[pd.Timestamp(s["timestamps"][r])]] for r in refs)
        d = dict(id=s["id"])
        for name,v in signals.items():
            preds=[ix[t] for t in f.loc[v,"timestamp"] if t in ix and 120<=ix[t]<=179]
            d[name]=event_metrics(refs,preds)
            matched={j for i,j in match_events(refs,preds)}
            for j,index in enumerate(preds):
                if not 125<=index<=174:continue
                full_index=ix_full[pd.Timestamp(s["timestamps"][index])]
                for reaction in (1,2):
                    probe=stop_probe(f,full_index,{"execution":m["execution"]},reaction_bars=reaction)
                    probes.append(dict(id=s["id"],method=name,window_index=index,visual_false_alarm=j not in matched,
                                       visual_category=a["category"],**probe))
            shown=pd.read_parquet(test/"bars"/f'{s["id"]}.parquet')
            draw(shown,s["id"]+" "+name,test/name/f'{s["id"]}.png',120,179,a["events"],preds)
        details.append(d)
    metrics={name:aggregate([d[name] for d in details]) for name in names}
    exits={name:{str(reaction):{group:exit_summary([p for p in probes if p["method"]==name and p["reaction_bars"]==reaction and (group=="all" or p["visual_false_alarm"])])
               for group in ("all","false_alarms")} for reaction in (1,2)} for name in names}
    result=dict(protocol=tm["protocol"],sampled=len(tm["samples"]),clear=len(details),uncertain=len(tm["samples"])-len(details),
                metrics=metrics,exit_audit=exits,intraday_gate_eligible_reference_events=int(eligible_refs),all_reference_events=int(refs_total),
                details=details,prefix_checks=checks,annotations_frozen_sha256=sha(frozen),evaluator_sha256=sha(__file__),
                limitations=["Exit capacity uses whole-minute volume, not executable quotes or depth","Stop touched in a bar is observed at its close; earliest sales in the next bar; no same-bar fill credit",
                             "All methods use identical causal stops and sizing; false-alarm labels do not cause exits","Position reduced using past five-bar volume; also report capacity for a fixed 3m KRW order",
                             "No-stop cases are not counted as successful stop exits; residuals are not silently removed","Small provisional shape labels; no live fill or profitability claim"],policy_active=False)
    pd.concat(frames,ignore_index=True).to_parquet(test/"predictions.parquet",index=False)
    atomic_json(test/"stop-probes.json",probes)
    atomic_json(test/"evaluation.json",result)
    pct=lambda x:f"{100*x:.1f}%" if x is not None else "측정 불가"
    table=''.join(f'<tr><td>{name}</td><td>{v["tp"]}/{v["tp"]+v["fn"]} ({pct(v["recall"])})</td><td>{pct(v["precision"])}</td><td>{v["fp"]}</td></tr>' for name,v in metrics.items())
    stops=''.join(f'<h3>{name}: 오탐 손절</h3><pre>{html.escape(json.dumps(exits[name]["1"]["false_alarms"],ensure_ascii=False,indent=2))}</pre>' for name in names)
    cards=''.join(f'<article><h3>{s["id"]} · {s["symbol"]} · {s["day"]}</h3><p>{html.escape(labels[s["id"]]["reason"])}</p><a href="blind/{s["id"]}.png">신호를 숨긴 원본</a>'+''.join(f'<details><summary>{name}</summary><img loading="lazy" src="{name}/{s["id"]}.png"></details>' for name in names if (test/name/f'{s["id"]}.png').exists())+'</article>' for s in tm["samples"])
    (test/"REPORT.html").write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>거래 활발·강한 추세 재학습과 손절 평가</title><style>body{max-width:1100px;margin:30px auto;padding:20px;font-family:system-ui;line-height:1.7}img{width:100%}td,th{padding:12px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}article{border-top:1px solid #aaa}</style><h1>유동성·추세 조건 재학습과 손절 평가</h1><p>기준봉·의지·시크릿 전일 선정에 전일 20일 중앙 거래대금 100억 이상, 종가>MA20>MA60, MA20 상승을 추가했습니다. 당일 신호는 최근 5분 거래대금 1억 이상, 거래 봉 4/5 이상, EMA5>EMA20 상승, VWAP 위 조건을 사용했습니다. 수치들은 이번 실험 가정입니다.</p><p>손절선은 확인 가능한 눌림 저점 또는 최근 5분 저점−0.1 ATR이며, 다음 봉 진입가 대비 1% 초과는 진입 거절합니다. 주문 상한 300만원, 직전 5분 거래량 1%로 수량을 줄이고 진입 봉 거래량 1%로 체결량을 제한합니다. 손절 접촉 봉의 종가에서 감지한 다음 봉부터 거래량 1%까지 매도하는 분봉 가정입니다. 실제 호가·주문 체결 또는 초 단위 즉시 청산의 증거가 아닙니다.</p><table><tr><th>동일 일봉 선정 구간</th><th>재현율</th><th>알림 정밀도</th><th>오탐</th></tr>'+table+'</table><p>prior_raw는 기존 분류기, prior_filtered는 같은 기존 분류기에 당일 유동성·추세 조건만 적용, retrained_filtered는 같은 조건에 새로 학습한 분류기입니다. 재현율 분모는 일봉 선정 범위의 모든 정답이며 당일 조건 탈락 정답도 누락으로 셉니다. ±5분 일대일 매칭, 구간당 50분, AI 임시 정답입니다.</p><p><a href="evaluation.json">전체 지표·한 봉 지연 스트레스</a> · <a href="stop-probes.json">신호별 진입·손절 경로</a> · <a href="predictions.parquet">봉별 특징·점수</a> · <a href="annotations-before-unblinding.json">공개 전 정답</a> · <a href="../fit.json">학습 범위</a></p>'+stops+cards+'</html>')
    print(json.dumps({k:v for k,v in result.items() if k not in ("details","protocol")},ensure_ascii=False,indent=2))


def replay(output):
    """Isolated execution probes for all held-out focus days, without shape accuracy claims."""
    m=validate(output)
    if (output/"heldout-replay.json").exists():
        raise ValueError("Replay is frozen")
    watch=pd.read_parquet(output/"watchlist.parquet")
    selected=watch.loc[watch.focus_selected & watch.day.gt(PARAMETERS["training_cutoff"])]
    model=joblib.load(output/"model.joblib")
    prior=joblib.load(DEVELOP/"confirmation-model.joblib")
    probes,sources,day_count,bar_count=[],{},0,0
    for symbol, group in selected.groupby("symbol"):
        path=Path(load_config()["data_dir"])/"candles"/f"{symbol}-1m-raw.parquet"
        sources[str(path)]=sha(path)
        minutes=regular_bars(pd.read_parquet(path))
        needed=set(group.day)
        daily=group.set_index("day")
        for day,bars in minutes.loc[minutes.day.isin(needed)].groupby("day",sort=True):
            f=intraday_context(detect(bars.reset_index(drop=True),symbol),daily.loc[day],m["execution"])
            scores={"prior_filtered":prior.predict_proba(f[SHAPE_FEATURES])[:,1],
                    "retrained_filtered":model.predict_proba(f[FEATURES])[:,1]}
            for name,score in scores.items():
                for i in np.flatnonzero(alarms(score,f,True)):
                    for reaction in (1,2):
                        probes.append(dict(symbol=symbol,day=day,method=name,
                                           score=float(score[i]),**stop_probe(f,int(i),{"execution":m["execution"]},reaction_bars=reaction)))
            day_count+=1;bar_count+=len(f)
    if day_count!=len(selected):
        raise ValueError("Selected day missing minute history")
    summaries={name:{str(reaction):exit_summary([r for r in probes if r["method"]==name and r["reaction_bars"]==reaction])
                     for reaction in (1,2)} for name in ("prior_filtered","retrained_filtered")}
    result=dict(scope="All post-cutoff daily-focus sessions; shape labels unavailable, not false-positive accuracy or portfolio PnL",
                selected_stock_days=day_count,symbols=int(selected.symbol.nunique()),stock_hours=bar_count/60,
                exits=summaries,minute_source_hashes=sources,experiment_manifest_sha256=sha(output/"manifest.json"),
                limitations=["Each alarm probed independently; overlapping positions and shared cash/volume not netted",
                             "Conditional whole-minute capacity simulation; actual executable depth/spread/latency unknown"],policy_active=False)
    atomic_json(output/"heldout-probes.json",probes)
    atomic_json(output/"heldout-replay.json",result)
    print(json.dumps({k:v for k,v in result.items() if k!="minute_source_hashes"},ensure_ascii=False,indent=2))


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument("command",choices=["fit","prepare","evaluate","replay"])
    p.add_argument("--output",type=Path,default=OUTPUT)
    a=p.parse_args()
    {"fit":fit,"prepare":prepare,"evaluate":evaluate,"replay":replay}[a.command](a.output.resolve())


if __name__=="__main__":main()
