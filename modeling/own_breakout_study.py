"""Symbolic breakouts (trendline, diagonal line, prior high, 60-day high, box top) AND the instructor chart rule.

Research only. User 2026-11-15: picks look like "trend breaks out"; can we detect symbolic breakouts and AND them
with the instructor rule, buying at S close and selling on D? Detectors and sets are fixed in PROTOCOL before results.
Daily bars: Naver KRX split-adjusted (closing-narrow-v1). Panel, universe, periods and exits: own-closing-v1/rank-v1.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from . import own_closing_rank as rk
from . import own_closing_study as oc
from .config import atomic_json
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-breakout-v1"
LEFT = RIGHT = 2
RECENT = 3
DETECTORS = ["TLDN", "DIAG", "PH", "HI60", "BOX"]
EXITS = ["TP5", "TP3", "E_open", "E_close", "O2"]
PROTOCOL = dict(
    version="own-breakout-v1",
    pivots=f"confirmed swing high j: high[j] > max(high[j-{LEFT}:j]) and high[j] >= max(high[j+1:j+{RIGHT}+1]); "
           f"usable from day j+{RIGHT}+1 (no look-ahead)",
    detectors=dict(
        TLDN="descending trendline: last two confirmed pivots (a<b) within 60 days, b-a>=5, high_b < high_a; closes since b "
             "stayed <= line; close[t-1] <= line(t-1) and close[t] > line(t)",
        DIAG="diagonal (빗각): last two confirmed pivots within 120 days, b-a>=5, any slope; same respect/cross rule",
        PH="prior high: highest confirmed pivot high within 120 days; close[t-1] <= level < close[t]",
        HI60="60-day high: close[t] > max(high[t-60:t]) and close[t-1] <= max(high[t-61:t-1])",
        BOX="box top: max(high[t-20:t]) / min(low[t-20:t]) - 1 <= 15% and close[t-1] <= box top < close[t]"),
    variants=dict(today="breakout on S", recent3=f"breakout on S or the {RECENT-1} previous sessions and S close >= the "
                                                    "breakout level (line value at S for TLDN/DIAG)", ANY="any detector"),
    rule="instructor tree core leaf (own-closing-v1 profile): close/20d low - 1 > 17.9%, close/MA5 - 1 > 0.9%, "
         "reference candle within 3 days, 20d median turnover >= 10^9.997 KRW",
    sets="U (universe), RULE, RULE & breakout, U & breakout, picks (reference), picks & breakout (diagnostic)",
    selection="all signals and top 3 per day by S-day turnover",
    exits=dict(primary="TP5 (+5% limit, else D close)", reported=EXITS),
    periods=oc.PERIODS,
    caveat="rule was fitted on 2026-03-10..06-30 picks; detectors are new hypotheses not looked at before this run",
)


def detect(code, a, start="20250401"):
    d = [x[0] for x in a]
    O, H, L, C = (np.array([x[i] for x in a], float) for i in range(1, 5))
    n = len(C)
    piv = []
    ev = {k: None for k in DETECTORS}  # last event: (t, kind-specific level fn data)
    rows = []

    def line_cross(p1, p2, t, window, need_desc):
        (a_, ha), (b_, hb) = p1, p2
        if t-a_ > window or b_-a_ < 5 or (need_desc and not hb < ha):
            return None
        m = (hb-ha)/(b_-a_)
        lv = lambda k: hb+m*(k-b_)  # noqa: E731
        xs = np.arange(b_, t)
        if len(xs) and not (C[b_:t] <= lv(xs)+1e-9).all():
            return None
        if lv(t) <= 0:
            return None
        return (C[t-1] <= lv(t-1) and C[t] > lv(t)), (b_, hb, m)

    for t in range(n):
        out = {}
        if t >= 61:
            cand = [p for p in piv if t-p[0] <= 120]
            # TLDN / DIAG
            for k, window, desc in (("TLDN", 60, True), ("DIAG", 120, False)):
                hit = False
                if len(cand) >= 2:
                    r = line_cross(cand[-2], cand[-1], t, window, desc)
                    if r and r[0]:
                        hit = True
                        ev[k] = (t, "line", r[1])
                out[k] = hit
            # PH
            hit = False
            if cand:
                lvl = max(p[1] for p in cand)
                if C[t-1] <= lvl < C[t]:
                    hit = True
                    ev["PH"] = (t, "lvl", lvl)
            out["PH"] = hit
            # HI60
            lvl, lvl1 = H[t-60:t].max(), H[t-61:t-1].max()
            out["HI60"] = bool(C[t] > lvl and C[t-1] <= lvl1)
            if out["HI60"]:
                ev["HI60"] = (t, "lvl", lvl)
            # BOX
            top, bot = H[t-20:t].max(), L[t-20:t].min()
            out["BOX"] = bool(top/bot-1 <= .15 and C[t-1] <= top < C[t])
            if out["BOX"]:
                ev["BOX"] = (t, "lvl", top)
        if d[t] >= start and t+1 < n:
            row = dict(code=code, S=d[t])
            for k in DETECTORS:
                row[f"{k}_today"] = bool(out.get(k, False))
                e = ev[k]
                rec = False
                if e is not None and t-e[0] < RECENT:
                    if e[1] == "lvl":
                        rec = C[t] >= e[2]
                    else:
                        b_, hb, m = e[2]
                        rec = C[t] >= hb+m*(t-b_)
                row[f"{k}_recent3"] = bool(rec)
            rows.append(row)
        j = t-RIGHT
        if j >= LEFT and H[j] > H[j-LEFT:j].max() and H[j] >= H[j+1:t+1].max():
            piv.append((j, H[j]))
        piv = [p for p in piv if t-p[0] <= 125]
    return rows


def one(item):
    return detect(*item)


def prefix_check(bars, n=6, seed=5):
    rng = np.random.default_rng(seed)
    bad = 0
    for c in rng.choice(sorted(bars), n, replace=False):
        a = bars[c]
        full = pd.DataFrame(detect(c, a)).set_index("S")
        part = pd.DataFrame(detect(c, a[:len(a)-30])).set_index("S")
        common = part.index.intersection(full.index)
        bad += int((full.loc[common] != part.loc[common]).to_numpy().sum())
    return dict(mismatches=bad)


def stats(g, e):
    x = g[e].to_numpy(float)
    if not len(x):
        return dict(n=0)
    w, lo = x[x > 0], x[x < 0]
    return dict(n=int(len(x)), mean=float(x.mean()), median=float(np.median(x)), win=float((x > 0).mean()),
                pf=float(w.sum()/-lo.sum()) if len(lo) else None, ci=oc.boot_ci(x, g.S.to_numpy()))


def evaluate(g):
    res = {}
    for e in EXITS:
        r = {"all": stats(g, e)}
        for p in oc.PERIODS:
            r[p] = stats(g[g.period == p], e)
        res[e] = r
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.output)
    if (out/"results.json").exists():
        raise SystemExit(f"{out} is frozen; choose a new version")
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    bars = cn.load_bars(oc.SRC)
    pc = prefix_check(bars)
    print("prefix", pc, flush=True)
    with cf.ProcessPoolExecutor(8) as ex:
        parts = list(ex.map(one, bars.items(), chunksize=20))
    B = pd.DataFrame([r for p in parts for r in p])
    f = pd.read_parquet(oc.MR/"own-closing/20261115-v1/panel.parquet")
    f = rk.add_exits(f).merge(B, on=["code", "S"], how="left")
    bcols = [c for c in B.columns if c.endswith(("_today", "_recent3"))]
    f[bcols] = f[bcols].fillna(False).astype(bool)
    f["ANY_today"] = f[[f"{k}_today" for k in DETECTORS]].any(axis=1)
    f["ANY_recent3"] = f[[f"{k}_recent3" for k in DETECTORS]].any(axis=1)
    f["RULE"] = (f.ld20 > .179) & (f.d5 > .009) & (f.ref_age.clip(upper=12) <= 3) & (np.log10(f.tvmed) > 9.997)
    f = f[f.U & f.period.notna()].copy()
    f.to_parquet(out/"panel.parquet", index=False)
    flags = [f"{k}_{v}" for k in DETECTORS+["ANY"] for v in ("today", "recent3")]

    # profile: breakout share among picks vs universe vs RULE non-picks (S >= 2026-03-10)
    P = f[f.S >= "20260310"]
    prof = []
    for fl in flags:
        prof.append(dict(flag=fl, universe=float(P[fl].mean()), picks=float(P[P.pick][fl].mean()),
                         rule_nonpick=float(P[P.RULE & ~P.pick][fl].mean()), rule_pick=float(P[P.RULE & P.pick][fl].mean()),
                         lift=float(P[P.pick][fl].mean()/max(P[fl].mean(), 1e-9))))

    sets = {"U": f, "RULE": f[f.RULE], "PICKS": f[f.pick]}
    for fl in flags:
        sets[f"RULE&{fl}"] = f[f.RULE & f[fl]]
        sets[f"U&{fl}"] = f[f[fl]]
        sets[f"PICKS&{fl}"] = f[f.pick & f[fl]]
    res = []
    for name, g in sets.items():
        res.append(dict(set=name, sel="all", per_day=float(len(g)/f.S.nunique()), **evaluate(g)))
        if not name.startswith("PICKS"):
            t3 = g.sort_values("tv", ascending=False).groupby("S").head(oc.TOP_K)
            res.append(dict(set=name, sel="top3", per_day=float(len(t3)/f.S.nunique()), **evaluate(t3)))
    atomic_json(out/"results.json", dict(prefix_check=pc, profile=prof, results=res))
    print("done", len(res))


if __name__ == "__main__":
    main()
