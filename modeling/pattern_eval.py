"""Frozen, return-blind visual audit of the existing candidate generator.

Run from autotrader: uv run --locked python -m modeling.pattern_eval prepare|prefixes|evaluate|verify.
Annotations are provisional reviewer judgements, never labels derived from profit.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from .config import atomic_json, load_config
from .features import contract_hashes, regular_bars, structural_features

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "outputs/model-research/pattern-audit/20261030-v1"
PROTOCOL = {
    "version": "lecture-shape-audit-v1",
    "reference": "Lecture PDF pages 4, 5, 22; trading notes 2026-10-29",
    "reviewer": "AI assistant visual judgement; provisional, not human gold labels",
    "positive": [
        "Visible descending resistance with >=2 separated swing highs, OR flat resistance plus rising lows",
        "Structure spans >=10 minute bars; isolated 2-3 bar noise and ordinary rising-trend continuation are negatives",
        "Close breaks resistance, subsequent pullback forms a higher structural low, then close recovers pullback local high",
        "Confirmation must be visible at that time; later gains/losses do not change the shape label",
    ],
    "types": ["reversal", "rising_low_convergence"],
    "nonpositive": ["ordinary_continuation", "micro_noise", "unconfirmed", "no_breakout", "uncertain"],
    "scale_note": "10-bar span is an audit hypothesis, not a parameter dictated by the lecture. Tick-chart bar counts are not copied.",
    "candidate_sampling": "40 simple random candidate rows without replacement from the full year; past <=120 bars only; positive confirmation within last 5 bars inclusive",
    "window_sampling": "24 uniformly proposed (symbol, global trading day, score-start index 120..320) triples, rejecting incomplete/gapped 180-bar windows; no candidate conditioning",
    "window_scoring": "180 displayed bars: context 0..119, annotate all confirmations 120..179; recall/false-alarm core 125..174 avoids 5-bar edge censoring",
    "matching": "One-to-one maximum-cardinality event matching, minimum absolute timing error; +/-5 minutes. Unmatched alarms, including repeats, are false positives.",
    "blinding": "No ticker/date/absolute price, detector markers, model scores, returns, or post-cutoff bars on review charts. Window event prefixes must be separately reviewed.",
    "uncertainty": "Uncertain samples excluded as a whole, with coverage reported; never silently counted negative",
    "selection": "No detector tuning or training on this audit; any later change requires a fresh untouched test sample",
}


def audit_protocol(selection=None):
    protocol = dict(PROTOCOL)
    if selection:
        protocol.update(version="lecture-shape-audit-nextday-v1",
            candidate_sampling="40 simple random candidate rows from D+1 sessions selected using D daily rules; no return conditioning",
            window_sampling="24 uniformly proposed (selected symbol/day pair, score-start index 120..320) triples; reject incomplete/gapped 180-bar windows; no signal conditioning")
    return protocol


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wilson(successes, total):
    if total == 0:
        return None
    z = 1.959963984540054
    p = successes / total
    den = 1 + z*z / total
    center = (p + z*z / (2*total)) / den
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / den
    return [max(0., center-half), min(1., center+half)]


def match_events(references, predictions, tolerance=5):
    """Indices are contiguous minute bars. Match duplicates at most once."""
    n, m = len(references), len(predictions)
    if not n or not m:
        return []
    cost = np.zeros((n+m, n+m))
    distance = np.abs(np.asarray(references)[:, None] - np.asarray(predictions)[None, :])
    # A match outweighs any possible sum of timing differences.
    reward = (n+m+1) * (tolerance+1)
    cost[:n, :m] = np.where(distance <= tolerance, distance-reward, reward)
    rows, cols = linear_sum_assignment(cost)
    return [(int(i), int(j)) for i, j in zip(rows, cols) if i < n and j < m and distance[i, j] <= tolerance]


def draw(frame, sample_id, path, start, end, events=None, predictions=None, bar_label="Bar index (1 bar = 1 minute)", levels=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import Rectangle

    fig, (ax, vol) = plt.subplots(2, 1, figsize=(14, 5.6), sharex=True,
                                gridspec_kw={"height_ratios": [4, 1]}, layout="constrained")
    x = np.arange(len(frame))
    p = frame[["open", "high", "low", "close"]].to_numpy() / frame.close.iloc[0] * 100
    colors = np.where(p[:, 3] >= p[:, 0], "#138d75", "#c0392b")
    ax.add_collection(LineCollection([[(i, l), (i, h)] for i, (_, h, l, _) in enumerate(p)], colors=colors, linewidths=.8))
    for i, (o, h, l, c) in enumerate(p):
        ax.add_patch(Rectangle((i-.32, min(o, c)), .64, max(abs(c-o), .00005), facecolor=colors[i], edgecolor=colors[i], linewidth=.6))
    ax.autoscale_view()
    vol.bar(x, frame.volume, color=colors, width=.7)
    for a in (ax, vol):
        a.axvspan(start-.5, min(end, len(frame)-1)+.5, color="#405c8c", alpha=.07)
        a.grid(alpha=.18)
        a.set_xlim(-1, len(frame))
    if events:
        for e in events:
            ax.axvline(e["confirmation"], color="#0072b2", linewidth=1.1)
            for key in ("highs", "lows"):
                anchors = e.get(key, [])
                if anchors:
                    col = "high" if key == "highs" else "low"
                    ax.plot(anchors, frame.iloc[anchors][col]/frame.close.iloc[0]*100, color="#0072b2", linestyle="--", linewidth=1)
    if predictions:
        for i in predictions:
            ax.axvline(i, color="#b45f06", linewidth=1, linestyle=":")
    if levels:
        for (label, value), color in zip(levels.items(), ("#4d7c0f", "#7c3aed", "#b45309")):
            if value is not None and np.isfinite(value):
                ax.axhline(value/frame.close.iloc[0]*100, color=color, linestyle="--", linewidth=.8, label=label)
        ax.legend(loc="best", fontsize=8)
    ax.set_title(sample_id + (" | blue: visual reference; orange: detector" if predictions is not None else " | past bars only; shaded: review interval"))
    ax.set_ylabel("Price index (first close=100)")
    vol.set_ylabel("Volume")
    vol.set_xlabel(bar_label)
    vol.set_xticks(np.arange(0, len(frame), 10))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def prepare(root, seed=20261030, watchlist=None):
    if (root/"manifest.json").exists():
        raise ValueError("Audit is frozen; use a new output directory rather than overwriting")
    c = load_config()
    data = Path(c["data_dir"])
    dataset = json.loads((data/"dataset.json").read_text())
    candidates = pd.read_parquet(data/"candidates.parquet", columns=["symbol", "day", "timestamp", "high_slope", "low_slope", "higher_low_atr"])
    if dataset["contract_hashes"] != contract_hashes():
        raise ValueError("Candidate source differs from the current detector")
    selection, selected_pairs = None, None
    hours = sum(x["minute_rows"] for x in dataset["coverage"])/60
    if watchlist is not None:
        watchlist = Path(watchlist).resolve()
        summary = json.loads(watchlist.with_name("selection.json").read_text())
        if sha(watchlist) != summary["watchlist_sha256"] or summary["dataset_sha256"] != dataset["dataset_sha256"]:
            raise ValueError("Daily selection checksum/dataset mismatch")
        watch = pd.read_parquet(watchlist)
        selected_pairs = watch.loc[watch.selected, ["symbol", "day"]].drop_duplicates().sort_values(["day", "symbol"]).reset_index(drop=True)
        candidates = candidates.merge(selected_pairs, on=["symbol", "day"], validate="many_to_one")
        selection = {"watchlist": str(watchlist), "watchlist_sha256": sha(watchlist), "selection_json": str(watchlist.with_name("selection.json")),
                     "selection_json_sha256": sha(watchlist.with_name("selection.json")), "summary": {k: v for k, v in summary.items() if k not in ("source_hashes", "rule_sources")}}
        hours = summary["selected_regular_stock_hours"]
    protocol = audit_protocol(selection)
    rng = np.random.default_rng(seed)
    cache = {}
    source_hashes = {}
    def get(symbol):
        if symbol not in cache:
            path = data/"candles"/f"{symbol}-1m-raw.parquet"
            source_hashes[str(path)] = sha(path)
            cache[symbol] = regular_bars(pd.read_parquet(path))
        return cache[symbol]
    entries = []
    for idx in rng.choice(len(candidates), 40, replace=False):
        row = candidates.iloc[idx]
        day = get(row.symbol).query("day == @row.day").reset_index(drop=True)
        endpoint = day.index[day.timestamp.eq(row.timestamp)].item()
        f = day.iloc[max(0, endpoint-119):endpoint+1].reset_index(drop=True)
        entries.append(({"kind": "candidate", "symbol": row.symbol, "day": row.day, "score_start": len(f)-6, "score_end": len(f)-1}, f))
    symbols = sorted(x["symbol"] for x in dataset["coverage"] if x["minute_rows"] > 0)
    days = dataset["days"]
    seen, tries = set(), 0
    while sum(x[0]["kind"] == "window" for x in entries) < 24:
        tries += 1
        if selected_pairs is None:
            symbol, day = str(rng.choice(symbols)), str(rng.choice(days))
        else:
            pair = selected_pairs.iloc[int(rng.integers(len(selected_pairs)))]
            symbol, day = pair.symbol, pair.day
        start = int(rng.integers(120, 321))
        key = (symbol, day, start)
        if key in seen:
            continue
        seen.add(key)
        f = get(symbol).query("day == @day").iloc[start-120:start+60].reset_index(drop=True)
        if len(f) != 180 or not f.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all():
            continue
        entries.append(({"kind": "window", "symbol": symbol, "day": day, "score_start": 120, "score_end": 179}, f))
    rng.shuffle(entries)
    samples = []
    for i, (entry, frame) in enumerate(entries, 1):
        sid = f"S{i:03}"
        path = root/"bars"/f"{sid}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
        predicted = candidates.loc[candidates.symbol.eq(entry["symbol"]) & candidates.day.eq(entry["day"]), "timestamp"]
        entry.update(id=sid, bars_sha256=sha(path), timestamps=[x.isoformat() for x in frame.timestamp],
                     predictions=frame.index[frame.timestamp.isin(predicted)].tolist())
        draw(frame, sid, root/"blind"/f"{sid}.png", entry["score_start"], entry["score_end"])
        samples.append(entry)
    manifest = {"protocol": protocol, "selection": selection, "seed": seed, "candidate_count": len(candidates), "source_hashes": source_hashes,
                "candidate_file_sha256": sha(data/"candidates.parquet"), "detector_contract": contract_hashes(), "window_proposals": tries,
                "regular_stock_hours": hours,
                "diagnostics_not_accuracy": {"candidate_last_two_lows_rising_fraction": float((candidates.higher_low_atr > 0).mean()),
                   "candidate_last_two_highs_nondecreasing_absent_fraction": float((candidates.high_slope <= 0).mean()),
                   "candidate_rising_lows_and_nonrising_highs_fraction": float(((candidates.higher_low_atr > 0) & (candidates.high_slope <= 0)).mean())},
                "samples": samples}
    atomic_json(root/"manifest.json", manifest)
    atomic_json(root/"annotations-template.json", {"reviewer": protocol["reviewer"], "protocol": protocol["version"],
        "manifest_sha256": sha(root/"manifest.json"), "samples": [
        {"id": s["id"], "status": "pending", "reason": "", "events": []} for s in samples]})
    print(json.dumps({"root": str(root), "samples": len(samples), "window_proposals": tries}))


def prefixes(root):
    manifest = json.loads((root/"manifest.json").read_text())
    labels = {x["id"]: x for x in json.loads((root/"annotations.json").read_text())["samples"]}
    for sample in manifest["samples"]:
        for j, e in enumerate(labels[sample["id"]]["events"]):
            frame = pd.read_parquet(root/"bars"/f'{sample["id"]}.parquet').iloc[:e["confirmation"]+1]
            draw(frame, f'{sample["id"]}-E{j+1} prefix', root/"prefix"/f'{sample["id"]}-E{j+1}.png', sample["score_start"], e["confirmation"])


def verify(root):
    """Replay real sampled days independently of the saved candidate table."""
    m = json.loads((root/"manifest.json").read_text())
    c = load_config()
    if contract_hashes() != m["detector_contract"]:
        raise ValueError("Detector changed")
    data = Path(c["data_dir"])
    if sha(data/"candidates.parquet") != m["candidate_file_sha256"]:
        raise ValueError("Candidate population changed")
    if m.get("selection"):
        s = m["selection"]
        if sha(s["watchlist"]) != s["watchlist_sha256"] or sha(s["selection_json"]) != s["selection_json_sha256"]:
            raise ValueError("Daily selection changed")
        watch = pd.read_parquet(s["watchlist"])
        pairs = set(zip(watch.loc[watch.selected, "symbol"], watch.loc[watch.selected, "day"]))
        if not all((s["symbol"], s["day"]) in pairs for s in m["samples"]):
            raise ValueError("Sample outside the prior-day watchlist")
    cache, replayed, prefixes_checked = {}, 0, 0
    for path, expected in m["source_hashes"].items():
        if sha(path) != expected:
            raise ValueError(f"Minute source changed: {path}")
    for s in m["samples"]:
        symbol, day = s["symbol"], s["day"]
        if symbol not in cache:
            cache[symbol] = regular_bars(pd.read_parquet(data/"candles"/f"{symbol}-1m-raw.parquet"))
        bars = cache[symbol].query("day == @day").reset_index(drop=True)
        full = structural_features(bars, symbol, c)
        shown = pd.read_parquet(root/"bars"/f'{s["id"]}.parquet')
        expected = [pd.Timestamp(s["timestamps"][i]) for i in s["predictions"]]
        actual = full.loc[full.candidate & full.timestamp.between(shown.timestamp.iloc[0], shown.timestamp.iloc[-1]), "timestamp"].tolist()
        if actual != expected:
            raise ValueError(f'Saved candidate replay differs: {s["id"]}')
        replayed += 1
        for cut in (shown.timestamp.iloc[-1], shown.timestamp.iloc[s["score_start"]]):
            part = structural_features(bars.loc[bars.timestamp <= cut], symbol, c)
            cols = ["timestamp", "candidate", "pattern_id", "stop_price", "high_slope", "low_slope", "higher_low_atr"]
            pd.testing.assert_frame_equal(full.loc[full.timestamp <= cut, cols].reset_index(drop=True), part[cols].reset_index(drop=True))
            prefixes_checked += 1
    result = {"sample_replays": replayed, "candidate_replay_matches": replayed, "causal_prefix_checks": prefixes_checked,
              "causal_prefix_matches": prefixes_checked, "minute_source_hashes_checked": len(m["source_hashes"]),
              "candidate_file_sha256": m["candidate_file_sha256"], "detector_contract": contract_hashes(),
              "annotations_frozen_before_unblinding": sha(root/"annotations.json") == sha(root/"annotations-before-unblinding.json"),
              "note": "Technical consistency/causality checks, not pattern recognition accuracy"}
    atomic_json(root/"verification.json", result)
    print(json.dumps(result, indent=2))


def review_report(root, manifest, annotation, result):
    esc = html.escape
    ca, wa = result["candidate_audit"], result["random_window_audit"]
    pct = lambda x: f"{100*x:.1f}%" if x is not None else "산출 불가"
    interval = lambda v: "산출 불가" if v is None else f"{pct(v[0])}–{pct(v[1])}"
    selection = manifest.get("selection")
    scope_html, daily_rows = "", {}
    if selection:
        summary = selection["summary"]
        watch = pd.read_parquet(selection["watchlist"])
        daily_rows = {(r["symbol"], r["day"]): r for r in watch.loc[watch.selected].to_dict("records")}
        scope_html = f'''<h2>전일 규칙 선별 → 다음 거래일 적용</h2><p>D 마감 자료로 최신 기준봉·시크릿·20일선 유지 또는 20일선 의지 회복을 판정한 후 D+1에만 적용했습니다. 정책 {esc(summary['policy'])}, 선정 {summary['selected_stock_days']:,} 종목·거래일, {summary['symbols']}종목. 다음날 종가나 다음날 수익으로 선정 종목을 바꾸지 않았습니다.</p><p>의지는 기존 코드의 20일선 종가 회복 대리 조건이며, 강한 돌파의 폭·거래량 정의는 미확정입니다. 음봉 기준봉의 시크릿 미정의 경로는 따로 표시했습니다. 과거 시장 상승률 상위 100개·뉴스·당시 위험종목 필터를 재현한 결과는 아닙니다.</p><p>선정 종목의 후보 {summary['selected_candidate_count']:,}건에 기존 LightGBM도 적용했습니다. 기존 모델은 형태 확률이 아닌 net_R 회귀이며 policy_active={summary['model']['policy_active']}입니다. 형태 평가에는 수익 진입 임계값을 적용하지 않았습니다. 이전 무선별 표본의 13.5%와는 다른 표본이므로 수치 차이를 선별의 인과 효과로 단정하지 않습니다.</p>'''
        group_rows = "".join(f"<tr><td>{esc(v['label'])}</td><td>{v['positive']}/{v['clear']} · {pct(v['precision'])}</td><td>{v['sampled']}건 중 {v['uncertain']}건 보류</td></tr>"
                             for v in result.get("selection_breakdown", {}).values())
        scope_html += f'<table><tr><th>선별 경로 (서로 겹칠 수 있음)</th><th>형태 정밀도</th><th>표본</th></tr>{group_rows}</table><p>경로별 표본이 작으므로 어느 선별 규칙이 우수한지 순위를 정하는 결과가 아닙니다. 포인트 돌파 행은 D 마감의 최신 셋업 기준이며, D에 새 기준봉도 형성된 경우는 새 기준으로 갱신되어 이전 셋업의 종가베팅 신호를 집계하지 않습니다. 기존 종가베팅 신호 전체의 집계와 구분하며, 관찰 종목을 전일 매수했다고 가정하지 않습니다.</p><p>학습 모델은 {esc(summary["model"]["trained_through"])}까지 학습됐으므로 그 이전 점수는 학습 구간 재생이며 당시 배포 가능한 모델의 예측 성능이 아닙니다. 이 점수로 형태 라벨·표본·감지 임계값을 정하지 않았습니다. 이번 수치는 고정된 규칙 감지기의 형태 평가입니다.</p>'
    labels = {x["id"]: x for x in annotation["samples"]}
    detail = {x["id"]: x for x in result["details"]}
    categories = {}
    cards = []
    for s in manifest["samples"]:
        a = labels[s["id"]]
        daily_html = ""
        if daily_rows:
            d = daily_rows[(s["symbol"], s["day"])]
            source = Path(load_config()["data_dir"])/"candles"/f'{s["symbol"]}-1d-adjusted.parquet'
            f = pd.read_parquet(source)
            dates = pd.to_datetime(f.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
            f = f.loc[dates <= d["selection_day"]].tail(80).copy().reset_index(drop=True)
            f[["open", "high", "low", "close"]] *= d["daily_price_scale_factor"]
            draw(f, f'D close {s["symbol"]} {d["selection_day"]}', root/"daily"/f'{s["id"]}.png', len(f)-1, len(f)-1,
                bar_label="Daily bar index (1 bar = 1 trading day)", levels={"Reference": d["reference_price"], "Secret": d["secret_price"], "D MA20": d["ma20_price"]})
            daily_html = f'''<p>D 선정일 {esc(d['selection_day'])} → D+1 {esc(s['day'])} · 기준봉 {esc(str(d['reference_day']))} · {esc(str(d['reason']))}</p><details><summary>전일 일봉·기준봉·시크릿 근거</summary><img loading="lazy" src="daily/{s['id']}.png" alt="전일 일봉 근거"><pre>{esc(json.dumps(d, ensure_ascii=False, indent=2, default=str))}</pre></details>'''
        if s["kind"] == "candidate":
            categories[a["category"]] = categories.get(a["category"], 0) + 1
        status = "보류" if a["status"] == "uncertain" else "형태 있음" if a["events"] else "형태 없음"
        prefixes_links = " ".join(f'<a href="prefix/{s["id"]}-E{j+1}.png">사건 {j+1}의 확인 시점 차트</a>' for j in range(len(a["events"])))
        pic = "blind" if a["status"] == "uncertain" else "reviewed"
        match = detail.get(s["id"], {})
        cards.append(f'''<article data-category="{esc(a['category'])}" id="{s['id']}">
<h3>{s['id']} · {'신호 표본' if s['kind']=='candidate' else '무작위 구간'} · {status}</h3>
<p>{esc(s['symbol'])} / {esc(s['day'])} · {esc(a['category'])}</p>{daily_html}<p>{esc(a['reason'])}</p>
<a href="{pic}/{s['id']}.png"><img loading="lazy" src="{pic}/{s['id']}.png" alt="{s['id']} 판독 차트"></a>
<p><a href="blind/{s['id']}.png">신호를 숨긴 원본</a> {prefixes_links}</p>
<details><summary>판독·감지 시각과 집계</summary><pre>{esc(json.dumps({'annotation': a, 'evaluation': match}, ensure_ascii=False, indent=2))}</pre></details></article>''')
    n_events = sum(len(a["events"]) for a in labels.values())
    n_refs = wa["tp_reference"] + wa["fn"]
    timing = ", ".join(f"{d:+d}분" for d in wa["matched_timing_minutes"]["all_signed"]) or "측정 사건 없음"
    document = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>뒤틀림 형태 감지 평가 — 2026-10-30</title><style>
body{{font-family:system-ui,sans-serif;max-width:1150px;margin:30px auto;padding:0 20px;line-height:1.7;color:#162235}}h1{{line-height:1.35}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{text-align:left;border-bottom:1px solid #d7dfeb;padding:10px}}article{{border-top:2px solid #d7dfeb;margin-top:35px;padding-top:10px}}img{{width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6fa;padding:16px}}a{{color:#0064b3}}.notice{{padding:18px;background:#fff1d6}}button{{padding:9px;margin:4px;cursor:pointer}}small{{color:#475569}}</style>
<h1>{'전일 규칙 선별 종목의 다음날 뒤틀림 평가' if selection else '뒤틀림 형태 감지 평가'}</h1>
<p class="notice">강의 형태 기준으로 AI 한 명이 판독한 <b>잠정 평가</b>입니다. 사람·강사의 정답 검수와 판독자 간 일치도는 미측정입니다. 수익률은 라벨과 지표에 사용하지 않았습니다.</p>
{scope_html}
<table><tr><th>지표</th><th>실측</th><th>범위와 해석</th></tr>
<tr><td>신호 정밀도</td><td><b>{pct(ca['precision'])}</b> · {ca['positive']}/{ca['clear']}</td><td>40건 중 보류 {ca['uncertain']}건 제외. Wilson 95% 구간 {interval(ca['wilson95'])}</td></tr>
<tr><td>보류 포함 민감도 범위</td><td>{pct(ca['conservative_precision_all_uncertain_negative'])}–{pct(ca['optimistic_precision_all_uncertain_positive'])}</td><td>보류 {ca['uncertain']}건을 모두 오답 또는 모두 정답으로 가정한 범위. 신뢰구간 아님.</td></tr>
<tr><td>무작위 구간 재현율</td><td><b>{pct(wa['recall'])}</b> · {wa['tp_reference']}/{n_refs}</td><td>양성 사건 {n_refs}건. 구간 bootstrap 95% 범위 {interval(wa['bootstrap95']['recall'])}. 작은 사건 수의 불확실성에 유의.</td></tr>
<tr><td>무작위 구간 오탐</td><td>{wa['fp']}건 / {wa['stock_hours']:.2f} 종목·시간</td><td>{wa['false_alarms_per_stock_hour']:.3f}건/종목·시간. bootstrap 95% {wa['bootstrap95']['false_alarms_per_stock_hour'][0]:.3f}–{wa['bootstrap95']['false_alarms_per_stock_hour'][1]:.3f}</td></tr>
<tr><td>맞춘 사건의 시점 차이</td><td>{timing}</td><td>신호 시각 − 판독 시각. 맞춘 {wa['matched_timing_minutes']['n']}개 사건만 해당하며 전체 감지 지연 성능으로 일반화 불가.</td></tr></table>
<p>대상: 국내 반도체 현재 구성 206종목, 2025-10-29~2026-10-28. 후보 {manifest['candidate_count']:,}건에서 {ca['sampled']}건 단순 무작위 추출. 별도로 신호 유무와 무관한 180분 구간 {wa['sampled']}개를 추출하여 {wa['clear']}개를 판독했고, 경계 5분을 제외한 구간당 50분을 집계했습니다. 개장 초기 구간은 신호 정밀도 표본에는 포함되지만, 무작위 구간 재현율은 120분의 문맥이 있는 장중 구간만 대상으로 합니다.</p>
<p>파란 선은 잠정 시각 판독과 anchor, 주황 점선은 기존 감지기입니다. ±5분 안에서 일대일로 사건을 연결했습니다. 서로 다른 표본에서 구한 정밀도와 재현율을 합쳐 F1을 만들지 않았습니다. 연속 시계열에서 자의적인 음성 봉 수로 전체 정확도를 부풀리지 않았습니다.</p>
<h2>무엇을 평가했나</h2><p>상방 추세 전환형과 저점 상승 수렴형의 <b>돌파 → 눌림 → 재상승</b> 확인입니다. 하방 전환 감지, 일반 박스 돌파 전체, 이후 상승 성공 여부를 평가한 결과가 아닙니다. 구조가 최소 10분에 걸친다는 조건은 분봉 검수용 가설이며 강의의 확정 수치가 아닙니다. 과거 차트를 판독한 뒤 양성 {n_events}개 사건을 확인 시점까지 잘라 재검수했습니다. 신뢰구간은 표집 변동이며 판독자의 오류나 형태 정의 차이까지 포함하지 않습니다.</p>
<h2>어디서 틀렸나</h2><p>신호 표본: 형태 양성 {categories.get('positive',0)}건, 돌파·눌림·재상승 미완성 {categories.get('unconfirmed',0)}건, 일반 상승의 연장 {categories.get('ordinary_continuation',0)}건, 미세 박스 잡음 {categories.get('micro_noise',0)}건, 돌파 없음 {categories.get('no_breakout',0)}건, 보류 {categories.get('uncertain',0)}건입니다. 기존 규칙은 선행 하락 고점이나 상승 저점을 필수로 요구하지 않습니다. LightGBM은 후보의 net_R을 학습했으며 형태 정답을 학습한 분류기가 아닙니다.</p>
<h2>다음 학습의 기준</h2><p>이번 결과를 기존 감지기의 기준점으로 보존합니다. 보류 {ca['uncertain']+wa['uncertain']}건과 대표 오탐·놓침을 사용자와 검수해 형태 정의·시간 크기를 확정하고, 형태·단계·확인 시각을 지도학습 라벨로 축적해야 합니다. 형태 분류기와 수익 예측기를 구분하고, 다음 버전은 이번에 본 차트를 재시험 점수로 사용하지 않고 새로운 미열람 표본에서 비교합니다. 현재 {n_refs}개의 양성 사건으로 강화학습이나 재현율 최적화를 시작할 근거는 부족합니다.</p>
<p><a href="evaluation.json">전체 지표 JSON</a> · <a href="annotations.json">판독 라벨</a> · <a href="manifest.json">고정 표본·프로토콜</a> · <a href="verification.json">원자료 재생·인과성 검증</a></p>
<h2>64개 판독 근거</h2><p>종목·날짜는 판독 후 공개했습니다. 미래 수익과 주문 정보는 표시하지 않습니다. 보류 사례에는 감지기 표시를 덧그리지 않았습니다.</p>
<nav><button onclick="filter('all')">전체</button><button onclick="filter('positive')">양성</button><button onclick="filter('uncertain')">보류</button><button onclick="filter('ordinary_continuation')">상승 연장 오탐</button><button onclick="filter('unconfirmed')">미완성</button></nav>
{''.join(cards)}<script>function filter(c){{document.querySelectorAll('article').forEach(a=>a.hidden=c!=='all'&&a.dataset.category!==c)}}</script></html>'''
    (root/"REPORT.html").write_text(document)


def evaluate(root):
    manifest = json.loads((root/"manifest.json").read_text())
    annotation = json.loads((root/"annotations.json").read_text())
    if annotation.get("manifest_sha256") != sha(root/"manifest.json") or annotation.get("protocol") != manifest["protocol"]["version"]:
        raise ValueError("Annotation does not belong to this frozen sample/protocol")
    if manifest["protocol"] != audit_protocol(manifest.get("selection")):
        raise ValueError("Audit protocol changed; use the original evaluator or start a new audit")
    if manifest["detector_contract"] != contract_hashes():
        raise ValueError("The detector changed after the sample was frozen")
    labels = {x["id"]: x for x in annotation["samples"]}
    if len(labels) != len(manifest["samples"]) or set(labels) != {x["id"] for x in manifest["samples"]}:
        raise ValueError("Exactly one annotation per sampled chart is required")
    points, windows, details = [], [], []
    for s in manifest["samples"]:
        a = labels[s["id"]]
        if a["status"] not in ("clear", "uncertain"):
            raise ValueError("Complete annotations before unblinding")
        if sha(root/"bars"/f'{s["id"]}.parquet') != s["bars_sha256"]:
            raise ValueError("Frozen chart source changed")
        if a["status"] == "uncertain":
            continue
        refs = [e["confirmation"] for e in a["events"]]
        if len(refs) != len(set(refs)):
            raise ValueError("Duplicate reference event")
        for e in a["events"]:
            if type(e["confirmation"]) is not int or not e.get("prefix_verified") or not s["score_start"] <= e["confirmation"] <= s["score_end"]:
                raise ValueError("Every event needs a reviewed causal prefix inside the review interval")
            if e["type"] not in PROTOCOL["types"]:
                raise ValueError("Unknown positive type")
            for key in ("highs", "lows"):
                if any(i < 0 or i > e["confirmation"] for i in e.get(key, [])):
                    raise ValueError("Future or invalid anchors")
        if s["kind"] == "candidate":
            points.append(bool(refs))
            detail = {"id": s["id"], "kind": "candidate", "positive": bool(refs)}
        else:
            preds = [i for i in s["predictions"] if 120 <= i <= 179]
            pairs = match_events(refs, preds)
            ri, pi = {i for i, _ in pairs}, {j for _, j in pairs}
            core = lambda i: 125 <= i <= 174
            tp_ref = sum(core(x) and i in ri for i, x in enumerate(refs))
            fn = sum(core(x) and i not in ri for i, x in enumerate(refs))
            tp_pred = sum(core(x) and j in pi for j, x in enumerate(preds))
            fp = sum(core(x) and j not in pi for j, x in enumerate(preds))
            delays = [preds[j]-refs[i] for i, j in pairs if core(refs[i])]
            detail = {"id": s["id"], "kind": "window", "reference_events": refs, "predictions": preds,
                      "matched_pairs": [[refs[i], preds[j]] for i, j in pairs], "tp_reference": tp_ref, "fn": fn,
                      "tp_alarm": tp_pred, "fp": fp, "stock_hours": 50/60, "delay_minutes": delays,
                      "duplicate_alarms": sum(core(x) and j not in pi and any(abs(x-r) <= 5 for r in refs) for j, x in enumerate(preds))}
            windows.append(detail)
        details.append(detail)
        frame = pd.read_parquet(root/"bars"/f'{s["id"]}.parquet')
        draw(frame, s["id"], root/"reviewed"/f'{s["id"]}.png', s["score_start"], s["score_end"], a["events"], s["predictions"])
    def aggregate(rows):
        tr, fn, ta, fp = [sum(x[k] for x in rows) for k in ("tp_reference", "fn", "tp_alarm", "fp")]
        hours = sum(x["stock_hours"] for x in rows)
        return {"recall": tr/(tr+fn) if tr+fn else None, "alarm_precision": ta/(ta+fp) if ta+fp else None,
                "false_alarms_per_stock_hour": fp/hours if hours else None, "tp_reference": tr, "fn": fn,
                "tp_alarm": ta, "fp": fp, "stock_hours": hours}
    wm = aggregate(windows)
    wm["edge_note"] = "Recall counts reference confirmations in bars 125..174; alarm precision/false alarms count predictions in bars 125..174. Matching can use bars 120..179, so TP counts can differ at the boundary."
    # Cluster bootstrap: each complete chart/window is resampled as a unit.
    rng = np.random.default_rng(manifest["seed"])
    boot = [aggregate([windows[i] for i in rng.integers(0, len(windows), len(windows))]) for _ in range(5000)] if windows else []
    wm["bootstrap95"] = {k: np.quantile([x[k] for x in boot if x[k] is not None], [.025, .975]).tolist()
                         if any(x[k] is not None for x in boot) else None for k in ("recall", "alarm_precision", "false_alarms_per_stock_hour")}
    delays = [d for x in windows for d in x["delay_minutes"]]
    wm["matched_timing_minutes"] = {"n": len(delays), "median_signed": float(np.median(delays)) if delays else None,
                                    "median_absolute": float(np.median(np.abs(delays))) if delays else None,
                                    "all_signed": delays, "note": "Matched events only, +/-5-minute matching; not a bound on all detection delays"}
    total_hours = manifest["regular_stock_hours"]
    result = {"protocol": manifest["protocol"], "manifest_sha256": sha(root/"manifest.json"), "annotations_sha256": sha(root/"annotations.json"),
              "evaluator_sha256": sha(__file__), "python": sys.version.split()[0], "uv_lock_sha256": sha(Path(__file__).parents[1]/"uv.lock"),
              "candidate_audit": {"sampled": 40, "clear": len(points), "uncertain": 40-len(points), "positive": sum(points),
                  "precision": sum(points)/len(points) if points else None, "wilson95": wilson(sum(points), len(points)),
                  "conservative_precision_all_uncertain_negative": sum(points)/40,
                  "optimistic_precision_all_uncertain_positive": (sum(points)+40-len(points))/40},
              "random_window_audit": {"sampled": 24, "clear": len(windows), "uncertain": 24-len(windows), **wm},
              "population_activity_not_accuracy": {"candidates": manifest["candidate_count"], "stock_hours": total_hours,
                  "alarms_per_stock_hour": manifest["candidate_count"]/total_hours, **manifest["diagnostics_not_accuracy"]},
              "details": details,
              "limits": ["Provisional single AI reviewer; instructor/human agreement not measured", "Small random-window sample; estimates have substantial uncertainty",
                         "Candidate precision and independent-window recall have different sampling frames: do not combine them into F1",
                         "No true negatives/overall accuracy defined for continuous event detection", "No claims about trading profit", "No changes to the frozen detector or outcome model"]}
    if manifest.get("selection"):
        watch = pd.read_parquet(manifest["selection"]["watchlist"]).set_index(["symbol", "day"])
        groups = {"reference_secret": ("최신 양봉 기준봉·시크릿 유지", lambda d: d.referenceValid and d.secretDefined),
                  "will_recovery": ("의지: 20일선 회복 대리 조건", lambda d: d.willRecovery),
                  "both": ("기준봉 유지와 의지 회복 동시", lambda d: d.intersection),
                  "prior_point_entry": ("전일 최신 셋업 포인트 돌파(동시 새 기준봉 제외)", lambda d: d.pointEntry),
                  "bearish_secret_undefined": ("음봉 기준봉·시크릿 미정의", lambda d: d.referenceValid and not d.secretDefined)}
        result["selection_breakdown"] = {}
        for key, (label, predicate) in groups.items():
            rows = [labels[s["id"]] for s in manifest["samples"] if s["kind"] == "candidate" and predicate(watch.loc[(s["symbol"], s["day"])])]
            clear = [a for a in rows if a["status"] == "clear"]
            positive = sum(bool(a["events"]) for a in clear)
            result["selection_breakdown"][key] = {"label": label, "sampled": len(rows), "clear": len(clear), "uncertain": len(rows)-len(clear),
                "positive": positive, "precision": positive/len(clear) if clear else None, "wilson95": wilson(positive, len(clear))}
    atomic_json(root/"evaluation.json", result)
    review_report(root, manifest, annotation, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("protocol", "details")}, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "prefixes", "evaluate", "verify"])
    p.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--seed", type=int, default=20261030, help="Sampling seed for prepare; existing audits keep their manifest seed")
    p.add_argument("--watchlist", type=Path, help="Frozen D-close selection for a separate D+1 audit")
    args = p.parse_args()
    root = args.output.resolve()
    if args.action == "prepare":
        prepare(root, args.seed, args.watchlist)
    else:
        {"prefixes": prefixes, "evaluate": evaluate, "verify": verify}[args.action](root)


if __name__ == "__main__":
    main()
