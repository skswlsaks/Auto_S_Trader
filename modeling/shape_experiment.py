"""Development replay, provisional supervised baseline and blinded paired test.

uv run --locked python -m modeling.shape_experiment develop|prepare|evaluate --output PATH
"""
from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .config import atomic_json, load_config
from .features import contract_hashes, regular_bars, structural_features
from .pattern_eval import draw, match_events, sha, wilson
from .shape_v2 import SHAPE_FEATURES, contract, detect

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "outputs/model-research/pattern-audit/20261030-nextday-v1"
WATCH = ROOT / "outputs/model-research/nextday/20261030-v1/watchlist.parquet"
DEVELOP = ROOT / "outputs/model-research/shape-v2/20261030-development-v1"
TEST = ROOT / "outputs/model-research/shape-v2/20261030-blind-v1"


def day_bars(symbol, day):
    path = Path(load_config()["data_dir"])/"candles"/f"{symbol}-1m-raw.parquet"
    raw = pd.read_parquet(path)
    dates = pd.to_datetime(raw.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    return regular_bars(raw.loc[dates.eq(day)]), path


def event_metrics(refs, predictions):
    refs, predictions = list(refs), list(predictions)
    pairs = match_events(refs, predictions, tolerance=5)
    ri, pi = {i for i, _ in pairs}, {j for _, j in pairs}
    core = lambda x: 125 <= x <= 174
    return {"tp_reference": sum(core(x) and i in ri for i, x in enumerate(refs)),
            "fn": sum(core(x) and i not in ri for i, x in enumerate(refs)),
            "tp_alarm": sum(core(x) and j in pi for j, x in enumerate(predictions)),
            "fp": sum(core(x) and j not in pi for j, x in enumerate(predictions)),
            "stock_hours": 50/60,
            "delays": [predictions[j]-refs[i] for i, j in pairs if core(refs[i])],
            "references": refs, "predictions": predictions}


def aggregate(rows):
    tr, fn, ta, fp = [sum(r[k] for r in rows) for k in ("tp_reference", "fn", "tp_alarm", "fp")]
    hours = sum(r["stock_hours"] for r in rows)
    delays = [d for r in rows for d in r["delays"]]
    return dict(tp=tr, fn=fn, alarm_tp=ta, fp=fp, stock_hours=hours,
                recall=tr/(tr+fn) if tr+fn else None, recall_wilson95=wilson(tr, tr+fn),
                precision=ta/(ta+fp) if ta+fp else None,
                false_alarms_per_stock_hour=fp/hours if hours else None,
                matched_delays_minutes=delays,
                median_absolute_delay=float(np.median(np.abs(delays))) if delays else None)


def supervised_alarms(frame, model, threshold=.5):
    scores = model.predict_proba(frame[SHAPE_FEATURES])[:, 1]
    eligible = frame.atr_pct.notna().to_numpy()
    above = (scores >= threshold) & eligible
    prior = np.r_[False, above[:-1]] & frame.segment.eq(frame.segment.shift()).to_numpy()
    return above & ~prior, scores


def load_labels(root):
    m = json.loads((root/"manifest.json").read_text())
    a = json.loads((root/"annotations.json").read_text())
    if a.get("manifest_sha256") != sha(root/"manifest.json"):
        raise ValueError("Labels belong to another manifest")
    if a.get("protocol") != m["protocol"]["version"]:
        raise ValueError("Annotation protocol differs")
    labels = {r["id"]: r for r in a["samples"]}
    if len(a["samples"]) != len(m["samples"]) or set(labels) != {s["id"] for s in m["samples"]}:
        raise ValueError("Exactly one label per sample required")
    for s in m["samples"]:
        r = labels[s["id"]]
        if r["status"] not in ("clear", "uncertain"):
            raise ValueError("Finish blind annotations before evaluation")
        if len({e["confirmation"] for e in r["events"]}) != len(r["events"]):
            raise ValueError("Duplicate event")
        if sha(root/"bars"/f'{s["id"]}.parquet') != s["bars_sha256"]:
            raise ValueError("Chart source changed")
        for e in r["events"]:
            if (type(e["confirmation"]) is not int or not s["score_start"] <= e["confirmation"] <= s["score_end"] or
                not e.get("prefix_verified") or e["type"] not in ("reversal", "rising_low_convergence")):
                raise ValueError("Invalid or unverified event")
            for key in ("highs", "lows"):
                if any(type(i) is not int or not 0 <= i <= e["confirmation"] for i in e.get(key, [])):
                    raise ValueError("Invalid event anchor")
    return m, labels


def develop(output):
    if (output/"manifest.json").exists():
        raise ValueError("Use a new development directory; previous runs are frozen")
    m, labels = load_labels(AUDIT)
    if m["detector_contract"] != contract_hashes():
        raise ValueError("Frozen detector changed")
    selected = pd.read_parquet(WATCH)
    allowed = set(zip(selected.loc[selected.selected, "symbol"], selected.loc[selected.selected, "day"]))
    frames, details, sources = [], [], {}
    prefix_checks = 0
    started = time.perf_counter()
    for s in m["samples"]:
        if (s["symbol"], s["day"]) not in allowed:
            raise ValueError("Development sample outside D-close selection")
        bars, source = day_bars(s["symbol"], s["day"])
        sources[str(source)] = sha(source)
        if sources[str(source)] != m["source_hashes"][str(source)]:
            raise ValueError("Minute source changed")
        new = detect(bars, s["symbol"])
        old = structural_features(bars, s["symbol"], load_config())
        ix = {pd.Timestamp(t): i for i, t in enumerate(s["timestamps"])}
        old_preds = [ix[t] for t in old.loc[old.candidate, "timestamp"] if t in ix]
        if old_preds != s["predictions"]:
            raise ValueError("Frozen v1 replay differs")
        for end in (s["score_start"], len(s["timestamps"])-1):
            cut = pd.Timestamp(s["timestamps"][end])
            prefix = detect(bars.loc[bars.timestamp <= cut], s["symbol"])
            pd.testing.assert_frame_equal(new.loc[new.timestamp <= cut].reset_index(drop=True), prefix)
            prefix_checks += 1
        shown = new.loc[new.timestamp.isin(ix)].copy().reset_index(drop=True)
        a = labels[s["id"]]
        shown["sample_id"], shown["symbol"], shown["sample_kind"] = s["id"], s["symbol"], s["kind"]
        shown["manual_stage_target"] = pd.NA
        shown["label_source"] = "AI_provisional" if a["status"] == "clear" else "uncertain_excluded"
        shown["human_verified"] = False
        shown["event_confirmation_target"] = pd.Series(pd.NA, index=shown.index, dtype="Int64")
        reviewed = shown.index.to_series().between(s["score_start"], s["score_end"])
        if a["status"] == "clear":
            shown.loc[reviewed, "event_confirmation_target"] = 0
            for e in a["events"]:
                # A completed event remains a positive training example for two bars.
                targets = reviewed & shown.index.to_series().between(e["confirmation"], e["confirmation"]+2)
                shown.loc[targets, "event_confirmation_target"] = 1
            if s["kind"] == "window":
                refs = [e["confirmation"] for e in a["events"]]
                pred = lambda f: [ix[t] for t in f.loc[f.candidate, "timestamp"] if t in ix and 120 <= ix[t] <= 179]
                details.append({"id": s["id"], "v1": event_metrics(refs, pred(old)), "v2": event_metrics(refs, pred(new))})
        frames.append(shown)
    labeled = pd.concat(frames, ignore_index=True)
    # One stock/minute cannot appear in both partitions or carry contradictory labels.
    train_rows = labeled.loc[labeled.event_confirmation_target.notna() & labeled.day.le("2026-08-26")].copy()
    for _, group in train_rows.groupby(["symbol", "timestamp"]):
        if group.event_confirmation_target.nunique() != 1:
            raise ValueError("Overlapping annotations disagree")
    train_rows = train_rows.drop_duplicates(["symbol", "timestamp"])
    train, valid = train_rows.loc[train_rows.day.le("2026-06-30")], train_rows.loc[train_rows.day.gt("2026-06-30")]
    if min(train.event_confirmation_target.nunique(), valid.event_confirmation_target.nunique()) != 2:
        raise ValueError("Both chronological partitions need positives and negatives")
    make_model = lambda: make_pipeline(SimpleImputer(strategy="constant", fill_value=0, add_indicator=True, keep_empty_features=True),
                                      StandardScaler(), LogisticRegression(C=.1, class_weight="balanced", max_iter=1000, random_state=42))
    model = make_model().fit(train[SHAPE_FEATURES], train.event_confirmation_target.astype(int))
    valid_scores = model.predict_proba(valid[SHAPE_FEATURES])[:, 1]
    # Fixed threshold: three validation events cannot support a threshold search.
    validation = {"rows": len(valid), "positive_rows": int(valid.event_confirmation_target.sum()),
                  "mean_positive_score": float(valid_scores[valid.event_confirmation_target.eq(1)].mean()),
                  "mean_negative_score": float(valid_scores[valid.event_confirmation_target.eq(0)].mean()),
                  "threshold": .5, "threshold_searched": False,
                  "note": "Sparse, previously exposed AI labels; development diagnostic only"}
    model = make_model().fit(train_rows[SHAPE_FEATURES], train_rows.event_confirmation_target.astype(int))
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output/"confirmation-model.joblib")
    labeled.to_parquet(output/"development-bars.parquet", index=False)
    result = {"purpose": "Development replay; not an untouched test", "v1": aggregate([d["v1"] for d in details]),
              "v2": aggregate([d["v2"] for d in details]), "details": details, "validation": validation,
              "training_rows": len(train_rows), "positive_training_rows": int(train_rows.event_confirmation_target.sum()),
              "training_events": sum(len(labels[s["id"]]["events"]) for s in m["samples"] if s["day"] <= "2026-08-26" and labels[s["id"]]["status"] == "clear"),
              "stage_model_trained": False, "confirmation_baseline_trained": True,
              "policy_active": False, "reinforcement_learning_started": False,
              "prefix_checks": prefix_checks, "v1_replays": len(m["samples"]),
              "seconds": time.perf_counter()-started}
    atomic_json(output/"development.json", result)
    atomic_json(output/"manifest.json", {"shape_contract": contract(), "v1_contract": contract_hashes(),
                "watchlist": str(WATCH), "watchlist_sha256": sha(WATCH),
                "source_audit": str(AUDIT), "source_audit_manifest_sha256": sha(AUDIT/"manifest.json"),
                "source_annotations_sha256": sha(AUDIT/"annotations.json"), "source_hashes": sources,
                "training_cutoff": "2026-08-26", "validation_train_cutoff": "2026-06-30",
                "trained_symbols": sorted(train_rows.symbol.unique().tolist()),
                "all_development_symbols": sorted(labeled.symbol.unique().tolist()),
                "model_sha256": sha(output/"confirmation-model.joblib"),
                "threshold": .5, "label_source": "AI_provisional", "human_verified": False,
                "stage_targets": "Missing; rule_stage must not be used as a gold target",
                "label_target": "Reference confirmation bar and next two bars within reviewed interval",
                "research_only": True, "policy_active": False})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def prepare(output, development=DEVELOP, count=32, seed=20261032):
    if output.exists():
        raise ValueError("Use a new blind-test directory")
    dm = json.loads((development/"manifest.json").read_text())
    if (dm["shape_contract"] != contract() or dm["v1_contract"] != contract_hashes() or
        dm["model_sha256"] != sha(development/"confirmation-model.joblib")):
        raise ValueError("Development detector/model changed")
    if sha(WATCH) != dm["watchlist_sha256"]:
        raise ValueError("Watchlist changed")
    watch = pd.read_parquet(WATCH)
    seen_pairs = set()
    for path in (AUDIT, ROOT/"outputs/model-research/pattern-audit/20261030-v1"):
        seen_pairs.update((s["symbol"], s["day"]) for s in json.loads((path/"manifest.json").read_text())["samples"])
    available = watch.loc[watch.selected & watch.day.gt(dm["training_cutoff"]) & ~watch.symbol.isin(dm["all_development_symbols"])].copy()
    available = available.loc[[(s, d) not in seen_pairs for s, d in zip(available.symbol, available.day)]]
    available = available.sort_values(["day", "symbol"]).reset_index(drop=True)
    if len(available) < count:
        raise ValueError("Insufficient untouched selected pairs")
    rng, samples, sources = np.random.default_rng(seed), [], {}
    for choice in rng.permutation(len(available)):
        pair = available.iloc[choice]
        full, source = day_bars(pair.symbol, pair.day)
        start = int(rng.integers(120, 321))
        frame = full.iloc[start-120:start+60].reset_index(drop=True)
        if len(frame) != 180 or not frame.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all():
            continue
        sid = f"B{len(samples)+1:03}"
        path = output/"bars"/f"{sid}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
        draw(frame, sid, output/"blind"/f"{sid}.png", 120, 179)
        sources[str(source)] = sha(source)
        samples.append(dict(id=sid, symbol=pair.symbol, day=pair.day, score_start=120, score_end=179,
                            bars_sha256=sha(path), timestamps=[t.isoformat() for t in frame.timestamp]))
        if len(samples) == count:
            break
    if len(samples) != count:
        raise ValueError("Insufficient complete windows")
    manifest = {"protocol": {"version": "shape-v2-blind-paired-v1", "positive": "Same lecture shape criteria as source audit; confirmation in 120..179; >=10-bar structure",
                  "reviewer": "AI provisional; not human gold", "matching": "One-to-one +/-5 minutes, core125..174, 50 minutes/window",
                  "sampling": "Uniform permutation of selected stock-days, uniform 120..320 start, reject incomplete/gapped windows",
                  "test_scope": "Dates after 2026-08-26 and symbols absent from all 64 development charts; all previous audit stock-days excluded",
                  "limits": "Structure design used earlier audit examples including October. This is a fresh sample test, not a wholly unseen calendar-period claim. No return labels."},
                "seed": seed, "development": str(development), "development_manifest_sha256": sha(development/"manifest.json"),
                "shape_contract": contract(), "v1_contract": contract_hashes(), "model_sha256": dm["model_sha256"],
                "watchlist_sha256": sha(WATCH), "source_hashes": sources, "samples": samples}
    atomic_json(output/"manifest.json", manifest)
    atomic_json(output/"annotations-template.json", {"protocol": manifest["protocol"]["version"], "manifest_sha256": sha(output/"manifest.json"),
                "samples": [dict(id=s["id"], status="pending", category="", reason="", events=[]) for s in samples]})
    print(json.dumps({"output": str(output), "blind_windows": len(samples), "unseen_development_symbols": True,
                      "predictions_hidden": True, "model_frozen": True}))


def evaluate(output):
    m, labels = load_labels(output)
    development = Path(m["development"])
    if (contract() != m["shape_contract"] or contract_hashes() != m["v1_contract"] or
        sha(development/"manifest.json") != m["development_manifest_sha256"] or
        sha(development/"confirmation-model.joblib") != m["model_sha256"] or sha(WATCH) != m["watchlist_sha256"]):
        raise ValueError("Frozen detector, model or selection changed")
    frozen = output/"annotations-before-unblinding.json"
    if frozen.exists() and sha(frozen) != sha(output/"annotations.json"):
        raise ValueError("Blind annotations changed after unblinding")
    if not frozen.exists():
        frozen.write_bytes((output/"annotations.json").read_bytes())
    model = joblib.load(development/"confirmation-model.joblib")
    details, checks, latencies, prediction_frames = [], 0, [], []
    cards = []
    for s in m["samples"]:
        bars, source = day_bars(s["symbol"], s["day"])
        if sha(source) != m["source_hashes"][str(source)]:
            raise ValueError("Minute source changed")
        t = time.perf_counter()
        new = detect(bars, s["symbol"])
        alarms, scores = supervised_alarms(new, model)
        latencies.append((time.perf_counter()-t)*1000)
        new["confirmation_score"], new["supervised_alarm"] = scores, alarms
        old = structural_features(bars, s["symbol"], load_config())
        saved = new.copy()
        saved["v1_candidate"] = old.candidate.to_numpy()
        saved["sample_id"], saved["symbol"], saved["dataset_role"] = s["id"], s["symbol"], "held_out_test"
        prediction_frames.append(saved)
        ix = {pd.Timestamp(t): i for i, t in enumerate(s["timestamps"])}
        for end in (120, 179):
            cut = pd.Timestamp(s["timestamps"][end])
            prefix = detect(bars.loc[bars.timestamp <= cut], s["symbol"])
            pd.testing.assert_frame_equal(new.loc[new.timestamp <= cut].drop(columns=["confirmation_score", "supervised_alarm"]).reset_index(drop=True), prefix)
            prefix_alarms, prefix_scores = supervised_alarms(prefix, model)
            np.testing.assert_array_equal(alarms[:len(prefix)], prefix_alarms)
            np.testing.assert_allclose(scores[:len(prefix)], prefix_scores, rtol=0, atol=1e-12)
            checks += 1
        a = labels[s["id"]]
        if a["status"] == "uncertain":
            continue
        refs = [e["confirmation"] for e in a["events"]]
        convert = lambda times: [ix[t] for t in times if t in ix and 120 <= ix[t] <= 179]
        predictions = {"v1": convert(old.loc[old.candidate, "timestamp"]), "v2": convert(new.loc[new.candidate, "timestamp"]),
                       "supervised": convert(new.loc[new.supervised_alarm, "timestamp"])}
        detail = dict(id=s["id"], **{k: event_metrics(refs, v) for k, v in predictions.items()})
        details.append(detail)
        shown = pd.read_parquet(output/"bars"/f'{s["id"]}.parquet')
        for name, preds in predictions.items():
            draw(shown, s["id"]+" "+name, output/name/f'{s["id"]}.png', 120, 179, a["events"], preds)
        cards.append(f'<article id="{s["id"]}"><h2>{s["id"]} · {html.escape(s["symbol"])} · {s["day"]}</h2><p>{html.escape(a["reason"])}</p>'+''.join(f'<details><summary>{name}</summary><img loading="lazy" src="{name}/{s["id"]}.png"></details>' for name in predictions)+f'<pre>{html.escape(json.dumps(detail, ensure_ascii=False, indent=2))}</pre></article>')
    metrics = {name: aggregate([d[name] for d in details]) for name in ("v1", "v2", "supervised")}
    # Chart bootstrap keeps within-window events together; labels remain provisional.
    rng = np.random.default_rng(m["seed"])
    bootstrap = {name: [] for name in metrics}
    for _ in range(2000):
        indices = rng.integers(0, len(details), len(details))
        for name in metrics:
            bootstrap[name].append(aggregate([details[i][name] for i in indices]))
    for name in metrics:
        metrics[name]["window_bootstrap95"] = {}
        for key in ("recall", "precision", "false_alarms_per_stock_hour"):
            values = [b[key] for b in bootstrap[name] if b[key] is not None]
            metrics[name]["window_bootstrap95"][key] = np.quantile(values, [.025, .975]).tolist() if values else None
    result = {"scope": m["protocol"], "sampled": len(m["samples"]), "clear": len(details),
              "uncertain": len(m["samples"])-len(details), "metrics": metrics, "details": details,
              "prefix_checks": checks, "annotations_frozen_sha256": sha(frozen),
              "shape_contract": contract(), "model_sha256": m["model_sha256"],
              "evaluator_sha256": sha(__file__),
              "full_day_detection_plus_prediction_ms": {"median": float(np.median(latencies)), "p95": float(np.quantile(latencies, .95)), "note": "Batch replay on this Mac; not a sustained live-feed benchmark"},
              "policy_active": False, "human_gold_labels": False, "reinforcement_learning_started": False,
              "promotion_rule": "Research only; no automatic promotion from this small provisional test"}
    pd.concat(prediction_frames, ignore_index=True).to_parquet(output/"test-predictions.parquet", index=False)
    atomic_json(output/"evaluation.json", result)
    pct = lambda x: f"{100*x:.1f}%" if x is not None else "산출 불가"
    table = ''.join(f'<tr><td>{name}</td><td>{v["tp"]}/{v["tp"]+v["fn"]} · {pct(v["recall"])}</td><td>{pct(v["precision"])}</td><td>{v["fp"]}</td><td>{v["false_alarms_per_stock_hour"]:.3f}</td></tr>' for name, v in metrics.items())
    prefix_links = ''.join(f'<a href="confirmed-prefix/{s["id"]}-E{j+1}.png">{s["id"]} 사건 {j+1}</a> · ' for s in m["samples"] for j, _ in enumerate(labels[s["id"]]["events"]))
    counts = f'{len(m["samples"])}개 제안 구간 중 {len(details)}개 확정·{result["uncertain"]}개 보류, {metrics["v1"]["stock_hours"]:.2f} 종목·시간입니다. 핵심 구간 125~174에 있는 {metrics["v1"]["tp"]+metrics["v1"]["fn"]}개 사건을 재현율에 집계했습니다.'
    (output/"REPORT.html").write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>뒤틀림 v2 독립 표본 비교</title><style>body{max-width:1100px;margin:30px auto;font-family:system-ui;line-height:1.7;padding:20px}img{width:100%}td,th{padding:12px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}article{border-top:1px solid #aaa}</style><h1>뒤틀림 v2 · 지도학습 초안 비교</h1><p>전일 규칙 선별 → 다음날, 개발에 없던 종목·새 표본의 동일 구간 비교. AI 임시 정답이며 실거래 활성화 없음. 구조 설계에는 기존 10월 사례도 참고하여 날짜 전체가 미열람인 실험은 아닙니다.</p><table><tr><th>모델</th><th>감지/정답</th><th>정밀도</th><th>오탐</th><th>오탐/종목·시간</th></tr>'+table+'</table><p>±5분 일대일 매칭. '+counts+' 경계 매칭으로 정밀도용 맞춘 알림 수와 재현율용 맞춘 사건 수는 다를 수 있습니다. 서로 다른 프레임의 이전 신호 정밀도 32.4%와 직접 비교하지 않습니다.</p><p>확인봉 회복을 넓게 허용한 v2와 지도학습 초안은 오탐이 증가하여 운영에 승격하지 않았습니다. 자동 단계는 정답으로 학습하지 않았습니다. 지도학습 초안은 기존 AI 확인봉과 이후 2봉을 양성으로 학습한 작은 선형 분류기이며, 클래스 가중치를 사용한 점수를 보정된 확률로 해석하지 않습니다. 다음 단계는 미완성 반등·상승 연장·반복 틱 박스의 어려운 반례와 검수된 단계 라벨을 늘리는 것입니다.</p><p><a href="evaluation.json">전체 지표</a> · <a href="annotations-before-unblinding.json">공개 전 고정 판독</a> · <a href="manifest.json">표본·고정 모델</a> · <a href="test-predictions.parquet">봉별 특징·판단</a></p><details><summary>양성 확인 시점 차트</summary>'+prefix_links+'</details>'+''.join(cards)+'</html>')
    print(json.dumps({k: v for k, v in result.items() if k != "details"}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=["develop", "prepare", "evaluate"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--development", type=Path, default=DEVELOP, help="Frozen development run for prepare")
    args = parser.parse_args()
    output = args.output or (DEVELOP if args.command == "develop" else TEST)
    if args.command == "prepare":
        prepare(output.resolve(), development=args.development.resolve())
    else:
        {"develop": develop, "evaluate": evaluate}[args.command](output.resolve())


if __name__ == "__main__":
    main()
