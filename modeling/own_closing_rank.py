"""Rank our own condition combinations by next-day return (S close buy, sell on D).

Exploratory ranking requested by the user (2026-11-15): "combine our own conditions and rank by return".
Every period has now been seen (own-closing-v1 opened eval/final), so this is a descriptive ranking over
2025-04..2026-11-12, NOT an out-of-sample test. Rankings are shown per period so stability is visible, and a
permutation null (shuffled cell labels within day) estimates how high the top cells would be by luck.
The only untouched data is the forward holdout from 2026-11-16.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import own_closing_study as oc
from .config import atomic_json
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-rank-v1"
PERIODS = ["train", "valid", "eval", "final"]
EXITS = {
    "E_open": "D 시가 매도",
    "E_close": "D 종가 매도",
    "TP3": "+3% 지정가 익절(시가가 위면 시가), 안 닿으면 D 종가",
    "TP5": "+5% 지정가 익절(시가가 위면 시가), 안 닿으면 D 종가",
    "O2": "35%@+3%, 35%@+5%, 잔량 종가, −3% 손절 먼저(보수)",
}
MIN_N, MIN_DAYS, MIN_EVAL_N = 300, 80, 30


def add_exits(f):
    S, o, h, c = f.close, f.Do, f.Dh, f.Dc
    for k, tp in (("TP3", .03), ("TP5", .05)):
        px = np.where(o >= S*(1+tp), o, np.where(h >= S*(1+tp), S*(1+tp), c))
        f[k] = px/S-1-oc.COST
    return f


DROP_BEST = 2


def drop_best_mean(x, day_idx, nd, k=DROP_BEST):
    """Pooled mean after removing the cell's k best days (by day mean): one-day market events must not carry a rank."""
    s = np.bincount(day_idx, weights=x, minlength=nd)
    c = np.bincount(day_idx, minlength=nd)
    act = np.flatnonzero(c)
    if len(act) <= k+2:
        return np.nan
    best = act[np.argsort(s[act]/c[act])[-k:]]
    keep = np.setdiff1d(act, best)
    return float(s[keep].sum()/c[keep].sum())


def period_stats(x, day_idx, nd, per_mask):
    out = {}
    for p, m in per_mask.items():
        st = oc.clustered(x[m], day_idx[m], nd)
        st["robust"] = drop_best_mean(x[m], day_idx[m], nd)
        out[p] = st
    return out


def run(f, F):
    U = f.U.to_numpy()
    per = f.period.to_numpy()
    days, day_idx = np.unique(f.S.to_numpy(), return_inverse=True)
    nd = len(days)
    X = {e: f[e].to_numpy(float) for e in EXITS}
    rows = []
    for cell in oc.cells(F):
        m = oc.mask_of(F, cell) & U
        if m.sum() < MIN_N:
            continue
        pm = {p: m & (per == p) for p in PERIODS}
        pm["all"] = m & (per != None)  # noqa: E711
        if pm["eval"].sum() < MIN_EVAL_N:
            continue
        r = dict(cell=" & ".join(cell), k=len(cell))
        for e, x in X.items():
            st = period_stats(x, day_idx, nd, pm)
            if e == "E_open":
                r["n"] = st["all"]["n"]
                r["days"] = st["all"]["days"]
                for p in PERIODS:
                    r[f"n_{p}"] = st[p]["n"]
            if st["all"]["days"] < MIN_DAYS:
                break
            r[f"{e}_all"] = st["all"]["mean"]
            r[f"{e}_t"] = st["all"]["t"]
            r[f"{e}_all_robust"] = st["all"]["robust"]
            for p in PERIODS:
                r[f"{e}_{p}"] = st[p]["mean"]
                r[f"{e}_{p}_robust"] = st[p]["robust"]
        else:
            rows.append(r)
    R = pd.DataFrame(rows)
    for e in EXITS:
        cols = [f"{e}_{p}" for p in PERIODS]
        R[f"{e}_worst"] = R[cols].min(axis=1)
        R[f"{e}_npos"] = (R[cols] > 0).sum(axis=1)
        rc = [f"{e}_{p}_robust" for p in PERIODS[:3]]  # final has only 12 days: robust check on train/valid/eval
        R[f"{e}_robust_worst"] = R[rc].min(axis=1)
    return R


def null_max(f, F, R, exit_col="E_open", draws=20, seed=11):
    """Luck check: shuffle E_open across stocks within each day (keeps day effects), recompute pooled means of the
    same cells, record the max. Top real cells should clearly exceed this."""
    keep = (f.U & f.period.notna()).to_numpy()
    sub = f[keep].reset_index(drop=True)
    per = sub.period.to_numpy()
    y = sub[exit_col].to_numpy(float)
    rng = np.random.default_rng(seed)
    order = np.argsort(sub.S.to_numpy(), kind="stable")
    sdays = sub.S.to_numpy()[order]
    cells = [tuple(c.split(" & ")) for c in R.cell]
    idxs = [np.flatnonzero(oc.mask_of(F, c)[keep]) for c in cells]
    pidx = [{p: ix[per[ix] == p] for p in PERIODS} for ix in idxs]
    mx, mx_worst = [], []
    for _ in range(draws):
        perm = order[np.lexsort((rng.random(len(order)), sdays))]  # same day blocks, random order inside each day
        x = np.empty_like(y)
        x[order] = y[perm]
        means = np.array([x[ix].mean() for ix in idxs])
        worst = np.array([min(x[d[p]].mean() if len(d[p]) else np.nan for p in PERIODS) for d in pidx])
        mx.append(float(np.nanmax(means)))
        mx_worst.append(float(np.nanmax(worst)))
    return dict(exit=exit_col, draws=draws, max_mean_p50=float(np.median(mx)), max_mean_p95=float(np.quantile(mx, .95)),
                max_worst_p50=float(np.median(mx_worst)), max_worst_p95=float(np.quantile(mx_worst, .95)))


def top3(f, F, cell, exit_col):
    g = oc.select(f, F, cell, oc.TOP_K)
    g = g[g.period.notna()]
    d = g.groupby("S")[exit_col].mean().sort_index()
    eq = (1+d).cumprod()
    mon = g.assign(m=g.S.str[:6]).groupby("m")[exit_col].mean()
    per = {p: float(g[g.period == p][exit_col].mean()) for p in PERIODS}
    return dict(cell=cell, exit=exit_col, trades=int(len(g)), days=int(len(d)), mean=float(g[exit_col].mean()),
                win=float((g[exit_col] > 0).mean()), **{f"m_{p}": v for p, v in per.items()},
                account=float(eq.iloc[-1]-1), mdd=float((eq/eq.cummax()-1).min()),
                pos_months=int((mon > 0).sum()), months=int(len(mon)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    f = pd.read_parquet(oc.MR/"own-closing/20261115-v1/panel.parquet")
    f = add_exits(f)
    F = oc.families(f)
    R = run(f, F)
    R.to_csv(out/"ranking-all.csv", index=False)
    print("cells ranked", len(R), flush=True)
    uni = {e: {p: float(f[f.U & (f.period == p)][e].mean()) for p in PERIODS} | {"all": float(f[f.U & f.period.notna()][e].mean())}
           for e in EXITS}
    tables = {}
    for e in EXITS:
        tables[f"{e}_by_mean"] = R.sort_values(f"{e}_all", ascending=False).head(20)
        tables[f"{e}_by_worst"] = R[R[f"{e}_npos"] == 4].sort_values(f"{e}_worst", ascending=False).head(20)
        tables[f"{e}_by_robust"] = R.sort_values(f"{e}_robust_worst", ascending=False).head(20)
    nulls = [null_max(f, F, R, e) for e in ("E_open", "TP3")]
    print("null", nulls, flush=True)
    picks = []
    for e in EXITS:
        for c in tables[f"{e}_by_robust"].cell.head(5):
            picks.append(top3(f, F, c, e))
        for c in tables[f"{e}_by_mean"].cell.head(3):
            picks.append(top3(f, F, c, e))
    rand = {}
    U = f[f.U & f.period.notna()]
    for e in EXITS:
        v = [U.sample(frac=1, random_state=s).groupby("S").head(oc.TOP_K)[e].mean() for s in range(50)]
        rand[e] = dict(mean=float(np.mean(v)), p95=float(np.quantile(v, .95)))
    atomic_json(out/"ranking.json", dict(
        note="Descriptive ranking over all periods (2025-04-01..2026-11-12); every period already seen. Not an out-of-sample test.",
        source_sha256=sha(Path(__file__)), exits=EXITS, min_n=MIN_N, min_days=MIN_DAYS, min_eval_n=MIN_EVAL_N,
        n_cells=int(len(R)), universe_mean=uni, random_top3=rand, null=nulls,
        tables={k: json.loads(v.to_json(orient="records")) for k, v in tables.items()},
        top3=json.loads(pd.DataFrame(picks).to_json(orient="records"))))
    print("done", out)


if __name__ == "__main__":
    main()
