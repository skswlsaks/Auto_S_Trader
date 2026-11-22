"""Buy s_trader91 picks in NXT pre-market (08:00-08:50) after the post; H2 exit (bearish twist model).

Research only. Combines: post-time entry rule, risk-flag filter, frozen up-twist classifier (P2),
fixed partial take-profit + break-even, and bearish transfer model for the remainder (X3).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import picks_runner_study as runner
from .config import atomic_json, load_config
from .daytrade import bearish_structure, session_bars
from .liquid_research import FEATURES
from .liquid_trend import alarms, intraday_context
from .pattern_eval import sha
from .shape_v2 import SHAPE_FEATURES, detect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/"outputs/model-research/premarket-entry/20261114-v1"
PICKS = ROOT/"outputs/research/s_trader91_picks_20261114"
PROTOCOL = dict(
    version="premarket-entry-v1",
    universe="s_trader91 picks (kind pre/after) whose D minute file has traded 08:01-08:50 bars and a complete 380-bar regular session",
    post_time="KST post timestamp from picks CSV; 'after' posts (previous evening) may enter from the 08:01 bar",
    entries=dict(
        A_close="S close (reference, not executable from posts); managed from 09:01",
        B_open="09:01 bar open",
        P0="first pre-market bar (bar end minute 481..530) with volume>0 whose bar end >= post minute + 2; fill at its open",
        P1="P0 but skipped if P0 price >= S close * 1.03",
        P2="first frozen up-classifier alarm (liquid-trend, thr .5, minute filter) in 481..529 with bar end >= post+2; fill next bar open",
        suffix_F="drop risk flags (S +10~20%, S is reference candle, invalidated)"),
    exits=dict(X0_base=runner.POLICIES["X0_base"], X3_runner_bear=runner.POLICIES["X3_runner_bear"]),
    management="Pre-market entries are managed from the entry bar through pre-market and regular session (08:50->09:01 gap filled at 09:01 open); stop -3% from entry; cost .25%",
    capacity="fillable = turnover (close*volume) of entry bar + next 4 pre-market bars >= 30,000,000 KRW (3M position <= 10%)",
    periods=dict(pre="D <= 2026-08-26 (classifier in-sample)", post="2026-08-27 .. 2026-10-27"),
    not_covered="10/29-11/13 Toss collection has no pre-market bars",
)


def bear_full(bars, code):
    """Bearish transfer-model alarms on the full session (pre-market + regular), same model as X3."""
    down = bearish_structure(bars, code)
    return alarms(runner._bear.predict_proba(down[SHAPE_FEATURES])[:, 1], down, False)


def post_times():
    a = pd.read_csv(PICKS/"picks_features.csv", dtype={"code": str})
    b = pd.read_csv(PICKS/"picks_new_features.csv", dtype={"code": str})
    p = pd.concat([a, b])[["code", "D일_게시후첫장", "post", "kind"]].dropna(subset=["code"])
    p["code"] = p.code.str.zfill(6)
    p["D"] = pd.to_datetime(p["D일_게시후첫장"].astype(int).astype(str)).dt.strftime("%Y-%m-%d")
    p["post_ts"] = pd.to_datetime(p.post)
    # several posts can name the same stock/day; the earliest post decides the entry time
    return p.sort_values("post_ts").drop_duplicates(["code", "D"]).set_index(["code", "D"])


def process(task, post):
    _, code, S, D, _, daily, s_close, meta = task
    if (code, D) not in post.index or s_close is None:
        return [dict(symbol=code, D=D, status="no_post_or_close")]
    pr = post.loc[(code, D)]
    if pr.kind not in ("pre", "after"):
        return [dict(symbol=code, D=D, status="intraday_post")]
    p = gate.day_path(code, D)
    if p is None:
        return [dict(symbol=code, D=D, status="no_day_file")]
    bars = session_bars(pd.read_parquet(p))
    m = bars.minute.to_numpy()
    if int(((m >= 541) & (m <= 920)).sum()) != 380:
        return [dict(symbol=code, D=D, status="incomplete_regular_session")]
    if not ((m >= 481) & (m <= 530) & (bars.volume.to_numpy() > 0)).any():
        return [dict(symbol=code, D=D, status="no_premarket_trades")]
    e = load_config()["execution"]
    f = intraday_context(detect(bars, code), daily, e)
    up = alarms(runner._model.predict_proba(f[FEATURES])[:, 1], f, True)
    bear = bear_full(bars, code)
    if not (len(f) == len(bars) == len(bear)):
        raise AssertionError("misaligned frames")
    o, h, l, c, v = (f[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    post_ts = pr.post_ts
    same_day = post_ts.strftime("%Y-%m-%d") == D
    earliest = post_ts.hour*60+post_ts.minute+2 if same_day else 0
    reg0 = int(np.flatnonzero(m >= 541)[0])
    flag = runner.risk_flag(meta)
    base = dict(symbol=code, S=S, D=D, status="ok", risk_flag=flag, post=str(post_ts), kind=pr.kind,
                s_close=s_close, d_open=o[reg0], **meta)
    entries = [("A_close", s_close, reg0), ("B_open", o[reg0], reg0)]
    pre_ok = np.flatnonzero((m >= 481) & (m <= 530) & (m >= earliest) & (v > 0))
    cap = None
    if len(pre_ok):
        i = int(pre_ok[0])
        win = (np.arange(len(m)) >= i) & (np.arange(len(m)) < i+5) & (m <= 530)
        cap = float((c[win]*v[win]).sum())
        base.update(p0_minute=int(m[i]), p0_price=o[i], p0_vs_close=o[i]/s_close-1, capacity_krw=cap,
                    fillable=cap >= 3e7)
        entries.append(("P0", o[i], i))
        if o[i] < s_close*1.03:
            entries.append(("P1", o[i], i))
    sig = np.flatnonzero(up & (m >= 481) & (m <= 529) & (m >= earliest))
    if len(sig) and sig[0]+1 < len(m):
        j = int(sig[0])
        entries.append(("P2", o[j+1], j+1))
        base.update(p2_minute=int(m[j]))
    rows = []
    for name, entry, start in entries:
        seg = [(o, h, l, c, bear, start)]
        for pol in ("X0_base", "X3_runner_bear"):
            net, reason, mfe, tp3, _ = runner.simulate(entry, entry*.97, seg, runner.POLICIES[pol])
            for vv in ([name, name+"_F"] if not flag else [name]):
                rows.append(dict(base, entry_variant=vv, policy=pol, entry=entry, net=net, reason=reason, mfe=mfe))
    return rows


def stats(x, days):
    x = np.asarray(x, float)
    if not len(x):
        return dict(n=0)
    w, lo = x[x > .001], x[x < -1e-9]
    out = dict(n=int(len(x)), mean=float(x.mean()), median=float(np.median(x)), win=float((x > .001).mean()),
               avg_win=float(w.mean()) if len(w) else None, avg_loss=float(lo.mean()) if len(lo) else None,
               pf=float(w.sum()/-lo.sum()) if len(lo) else None)
    g = pd.DataFrame(dict(d=days, x=x)).groupby("d").x.agg(["sum", "count"])
    if len(g) >= 5:
        idx = np.random.default_rng(7).integers(0, len(g), (1000, len(g)))
        b = g["sum"].to_numpy()[idx].sum(1)/g["count"].to_numpy()[idx].sum(1)
        out["ci"] = [float(np.quantile(b, .025)), float(np.quantile(b, .975))]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    a = ap.parse_args()
    out = Path(a.output)
    if (out/"trades.parquet").exists():
        raise SystemExit(f"{out} is frozen; choose a new version")
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__)),
                                          runner_sha256=sha(Path(runner.__file__))))
    k = gate.kosdaq(out)
    tasks, missing = gate.pick_tasks(k)
    tasks = [t for t in tasks if t[3] <= "2026-10-27"]
    post = post_times()
    runner._init()
    rows = list(missing)
    for t in tasks:
        rows.extend(process(t, post))
    t = pd.DataFrame(rows)
    t["period"] = np.where(t.D > "2026-08-26", "post", "pre")
    t.to_parquet(out/"trades.parquet", index=False)
    ok = t[t.status.eq("ok")]
    summ = []
    for scope, g0 in (("all", ok), ("fillable", ok[ok.fillable.fillna(False).astype(bool)])):
        for keys, g in g0.groupby(["period", "entry_variant", "policy"]):
            summ.append(dict(scope=scope, **dict(zip(["period", "entry_variant", "policy"], keys)), **stats(g.net, g.D)))
        for keys, g in g0.groupby(["entry_variant", "policy"]):
            summ.append(dict(scope=scope, period="all", **dict(zip(["entry_variant", "policy"], keys)), **stats(g.net, g.D)))
    atomic_json(out/"summary.json", summ)
    cov = t.drop_duplicates(["symbol", "D"]).status.value_counts().to_dict()
    atomic_json(out/"coverage.json", cov)
    print(cov)


if __name__ == "__main__":
    main()
