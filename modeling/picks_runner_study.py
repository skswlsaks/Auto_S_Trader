"""s_trader91 picks only: conditions + twist model at entry x 'let winners run' exits.

Research only. Exit variants X1-X5 deliberately depart from the confirmed +0.5% BE /
35-35-30 rule (X0) to test whether larger winners can outweigh a lower win rate.
"""
from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .config import atomic_json, load_config
from .daytrade import bearish_structure, session_bars
from .entry_gate_study import (CUTOFF, PROTOCOL as GATE_PROTOCOL, arrays, clamp, first_entries, kosdaq,
                               minute_day, pick_tasks, regular)
from .liquid_research import FEATURES, OUTPUT as LIQUID, validate
from .liquid_trend import alarms, intraday_context
from .pattern_eval import sha
from .shape_experiment import DEVELOP
from .shape_v2 import SHAPE_FEATURES, detect

MR = Path(__file__).resolve().parents[2]/"outputs/model-research"
OUT = MR/"picks-runner/20261114-v1"
COST = .0025

POLICIES = {
    "X0_base": dict(targets=[(.03, .35), (.05, .35)], be_trigger=.005, be_after_tp=True),
    "X1_be_after_tp3": dict(targets=[(.03, .35), (.05, .35)], be_after_tp=True),
    "X2_runner_trail": dict(targets=[(.03, .35)], be_after_tp=True, trail=.025, trail_start=.03),
    "X3_runner_bear": dict(targets=[(.03, .35)], be_after_tp=True, bear_after_tp=True),
    "X4_trail_overnight": dict(targets=[(.03, .35)], be_after_tp=True, trail=.025, trail_start=.03,
                               overnight_min=.03),
    "X5_stop_only": dict(targets=[]),
}
PROTOCOL = dict(
    version="picks-runner-v1",
    universe="s_trader91 picks with complete D regular 1m bars (same 337 as entry-gate-v1)",
    windows=GATE_PROTOCOL["windows"], entry_scope="first signal across W1-W3 (ALL)",
    entries=dict(A_close="S close (reference, not executable from posts)", B_open="D open",
                 G1="open break -0.5% then reclaim (event)", M1="frozen classifier", M1o="M1 & close>=open",
                 G1M1="M1 after G1 reclaim & close>=open",
                 suffix_F="drop risk flags: S-day +10~20%, S-day is reference candle (ref_age==0), invalidated"),
    stops=GATE_PROTOCOL["stops"], exits=POLICIES,
    exit_rules=dict(cost_round_trip=COST, same_bar="stop before targets; trail/BE updated after the bar",
                    bear="bearish transfer model alarm (mirrored shape features) -> next bar open",
                    overnight="X4 only: remainder held to D+1 if D 15:20 close >= entry*1.03; D+1 exit by 15:20",
                    gap="stop hit on a gap fills at the bar open"),
    periods=dict(pre=f"D <= {CUTOFF} (classifier in-sample)", post=f"D > {CUTOFF}"),
    note="Exit variants were chosen before reading results but several are compared; treat best-of as exploratory.",
)

_model = _bear = None


def _init():
    global _model, _bear
    _model = joblib.load(LIQUID/"model.joblib")
    _bear = joblib.load(DEVELOP/"confirmation-model.joblib")


def bear_signal(bars, symbol):
    down = bearish_structure(bars, symbol)
    mask = alarms(_bear.predict_proba(down[SHAPE_FEATURES])[:, 1], down, False)
    down = down.assign(bear=mask)
    r = regular(down)
    return None if r is None else r.bear.to_numpy(bool)


def day_frames(raw, symbol, daily, execution, entry_model=True):
    bars = session_bars(raw)
    bear = bear_signal(bars, symbol)
    if not entry_model:
        r = regular(bars)
        return r, bear
    f = intraday_context(detect(bars, symbol), daily, execution)
    up = alarms(_model.predict_proba(f[FEATURES])[:, 1], f, True)
    cand = f.candidate.to_numpy(bool)
    rule = cand & ~(np.r_[False, cand[:-1]] & f.segment.eq(f.segment.shift()).to_numpy())
    return regular(f.assign(up=up, rule=rule)), bear


def simulate(entry, stop, segs, p):
    """segs: [(o,h,l,c,bear,start)]. Returns net, reason, max_gain, tp3, overnight."""
    rem, pnl, done, armed, sl, peak = 1., 0., set(), False, stop, entry
    targets = p.get("targets", [])
    overnight = False
    for k, (o, h, l, c, bear, start) in enumerate(segs):
        n = len(o)
        for i in range(start, n):
            if l[i] <= sl:
                pnl += rem*(min(o[i], sl)/entry-1)
                reason = "trail" if p.get("trail") and sl > entry*(1+COST)+1e-9 else ("breakeven" if armed else "stop")
                return pnl-COST, reason, peak/entry-1, bool(done), overnight
            for j, (g, frac) in enumerate(targets):
                if j not in done and h[i] >= entry*(1+g):
                    pnl += frac*g; rem -= frac; done.add(j)
                    if p.get("be_after_tp"):
                        armed = True
            if p.get("be_trigger") and h[i] >= entry*(1+p["be_trigger"]):
                armed = True
            peak = max(peak, h[i])
            if armed:
                sl = max(sl, entry*(1+COST))
            if p.get("trail") and peak >= entry*(1+p["trail_start"]):
                sl = max(sl, peak*(1-p["trail"]))
            if p.get("bear_after_tp") and done and bear is not None and bear[i]:
                px = o[i+1] if i+1 < n else c[i]
                pnl += rem*(px/entry-1)
                return pnl-COST, "bear", peak/entry-1, True, overnight
        last = k == len(segs)-1
        if not last and p.get("overnight_min") is not None and c[-1] >= entry*(1+p["overnight_min"]):
            overnight = True
            continue
        pnl += rem*(c[-1]/entry-1)
        return pnl-COST, "close", peak/entry-1, bool(done), overnight
    raise AssertionError("unreachable")


def risk_flag(meta):
    chg1 = meta.get("chg1")
    return bool((chg1 is not None and np.isfinite(chg1) and .10 < chg1 <= .20) or meta.get("ref_age") == 0
                or meta.get("pick_status") == "invalidated")


def process(task):
    pop, symbol, S, D, D1, daily, s_close, meta = task
    raw = minute_day(symbol, D)
    if raw is None:
        return [dict(symbol=symbol, D=D, status="no_minutes")]
    e = load_config()["execution"]
    try:
        r, bear = day_frames(raw, symbol, daily, e)
    except Exception as ex:
        return [dict(symbol=symbol, D=D, status=f"error:{type(ex).__name__}:{ex}")]
    if r is None or bear is None:
        return [dict(symbol=symbol, D=D, status="incomplete_regular_session")]
    o, h, l, c = arrays(r)
    nxt = None
    if D1 and (raw1 := minute_day(symbol, D1)) is not None:
        r1, bear1 = day_frames(raw1, symbol, daily, e, entry_model=False)
        if r1 is not None and bear1 is not None:
            nxt = (*arrays(r1), bear1, 0)
    flag = risk_flag(meta)
    base = dict(symbol=symbol, S=S, D=D, status="ok", risk_flag=flag, has_next_day=nxt is not None, **meta)
    entries = []
    if s_close is not None and np.isfinite(s_close):
        entries.append(("A_close", -1, s_close, s_close*.97, 0))
    entries.append(("B_open", 0, o[0], o[0]*.97, 0))
    for (v, w), (i, st) in first_entries(r).items():
        if w == "ALL" and v in ("G1", "M1", "M1o", "G1M1"):
            entries.append((v, i, o[i+1], clamp(st, o[i+1]), i+1))
    rows = []
    for v, i, entry, stop, start in entries:
        segs = [(o, h, l, c, bear, start)] + ([nxt] if nxt is not None else [])
        for name, p in POLICIES.items():
            use = segs if name == "X4_trail_overnight" else segs[:1]
            net, reason, mfe, tp3, on = simulate(entry, stop, use, p)
            for vv in ([v, v+"_F"] if not flag else [v]):
                rows.append(dict(base, entry_variant=vv, policy=name, signal_index=i,
                                 signal_minute=int(r.minute.iat[i]) if i >= 0 else 540, entry=entry, stop=stop,
                                 net=net, reason=reason, mfe=mfe, tp3=tp3, overnight=on))
    return rows


def stats(x, days, reps=1000, seed=7):
    x = np.asarray(x, float)
    if not len(x):
        return dict(n=0)
    w, lo = x[x > .001], x[x < -1e-9]
    out = dict(n=len(x), mean=x.mean(), median=np.median(x), win_rate=(x > .001).mean(),
               accuracy=(x >= -1e-9).mean(), avg_win=w.mean() if len(w) else np.nan,
               avg_loss=lo.mean() if len(lo) else np.nan, sum=x.sum(), best=x.max(), worst=x.min(),
               pf=w.sum()/-lo.sum() if len(lo) else np.nan)
    out["payoff"] = out["avg_win"]/-out["avg_loss"] if len(w) and len(lo) else np.nan
    srt = np.sort(x)[::-1]
    out["top10pct_share_of_gains"] = srt[:max(1, len(x)//10)].clip(0).sum()/x.clip(0).sum() if x.clip(0).sum() > 0 else np.nan
    g = pd.DataFrame(dict(d=days, x=x)).groupby("d").x.agg(["sum", "count"])
    if len(g) >= 5:
        idx = np.random.default_rng(seed).integers(0, len(g), (reps, len(g)))
        b = g["sum"].to_numpy()[idx].sum(1)/g["count"].to_numpy()[idx].sum(1)
        out["ci_lo"], out["ci_hi"] = np.quantile(b, .025), np.quantile(b, .975)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)-1))
    args = ap.parse_args()
    out = Path(args.output)
    if (out/"trades.parquet").exists():
        raise SystemExit(f"{out} is frozen; choose a new output version")
    out.mkdir(parents=True, exist_ok=True)
    m = validate(LIQUID)
    k = kosdaq(out)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__)),
                                          gate_source_sha256=sha(Path(__file__).with_name("entry_gate_study.py")),
                                          model_sha256=m["model_sha256"],
                                          bear_model_sha256=sha(DEVELOP/"confirmation-model.joblib")))
    tasks, missing = pick_tasks(k)
    rows = list(missing)
    with ProcessPoolExecutor(args.workers, initializer=_init) as ex:
        for res in ex.map(process, tasks, chunksize=4):
            rows.extend(res)
    t = pd.DataFrame(rows)
    t["period"] = np.where(t.D > CUTOFF, "post", "pre")
    t.to_parquet(out/"trades.parquet", index=False)
    ok = t[t.status.eq("ok")]
    summ = []
    for keys, g in ok.groupby(["period", "entry_variant", "policy"]):
        summ.append(dict(zip(["period", "entry_variant", "policy"], keys), **stats(g.net, g.D)))
    for keys, g in ok.groupby(["entry_variant", "policy"]):
        summ.append(dict(period="all", **dict(zip(["entry_variant", "policy"], keys)), **stats(g.net, g.D)))
    pd.DataFrame(summ).to_csv(out/"summary.csv", index=False)
    print(t.drop_duplicates(["symbol", "D"]).status.value_counts().to_string())
    print("done", out)


if __name__ == "__main__":
    main()
