"""Long-horizon (3~5 year daily) symbolic breakouts AND the instructor rule; chart rendering for AI review.

Research only. User 2026-11-15: judge trend breakouts on daily bars over a bigger angle, 3~5 years.
Steps (each fixed in PROTOCOL before results):
  --fetch    Naver KRX daily 2020-01-01..2026-11-13 for the closing-narrow universe (public endpoint)
  --run      long detectors on S 2025-04-01..2026-11-12, evaluated with own-closing panel exits
  --render   5-year + 6-month chart PNGs (bars up to S only) for a sample, for review / AI judging
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from . import own_breakout_study as ob
from . import own_closing_rank as rk
from . import own_closing_study as oc
from .config import atomic_json
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-long-breakout-v1"
DATA = oc.MR/"long-daily/20261115-v1"
START, END = "20200101", "20261113"
K = 10                     # major pivot: max of +-10 sessions
Y3, Y5 = 750, 1250         # sessions
RECENT = 5
DETECTORS = ["LTL", "LDIAG", "LPH", "HI3Y", "HI5Y"]
PROTOCOL = dict(
    version="own-long-breakout-v1",
    data=f"Naver KRX daily (split-adjusted) {START}..{END}, closing-narrow-v1 codes",
    pivots=f"major swing high j: high[j] > max(high[j-{K}:j]) and high[j] >= max(high[j+1:j+{K}+1]); usable from j+{K}+1",
    detectors=dict(
        LTL=f"long descending trendline: A = highest major pivot within {Y5} sessions (older than 20); B = later major pivot "
            f"maximizing slope from A (all later pivots on/below the line), high_B < high_A, B-A >= 40, t-B >= 5; "
            f"close[t-1] <= line(t-1) and close[t] > line(t)",
        LDIAG=f"long diagonal (빗각): line through the two most recent major pivots within {Y3} sessions, B-A >= 40, any "
              f"slope; closes since B stayed <= line; same cross rule",
        LPH=f"multi-year prior high: highest major pivot within {Y3} sessions excluding the last 20; close[t-1] <= level < close[t]",
        HI3Y=f"3-year closing high: close[t] > max(high[t-{Y3}:t]) and close[t-1] <= max(high[t-{Y3}-1:t-1]); needs {Y3}+1 sessions",
        HI5Y=f"5-year closing high: same with {Y5}; needs {Y5}+1 sessions"),
    variants=dict(today="breakout on S", recent5=f"breakout within the last {RECENT} sessions incl. S and S close >= level"),
    evaluation="same sets/exits/periods as own-breakout-v1 (RULE, RULE & flag, U & flag, picks & flag; TP5 primary)",
    render="PNG: top = up to 5 years of daily candles (log scale) + MA60/MA120; bottom = last 120 sessions candles + "
           "MA5/20/60 + turnover; vertical line at S; nothing after S is drawn",
)

H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}


def fetch():
    d = DATA/"naver-daily"
    d.mkdir(parents=True, exist_ok=True)
    codes = sorted(p.stem for p in (oc.SRC/"naver-daily").glob("*.txt"))

    def one(c):
        p = d/f"{c}.txt"
        if p.exists():
            return True
        url = (f"https://api.finance.naver.com/siseJson.naver?symbol={c}&requestType=1"
               f"&startTime={START}&endTime={END}&timeframe=day")
        for a in range(3):
            try:
                t = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20).read().decode("utf-8", "replace")
                tmp = d/f"{c}.tmp"
                tmp.write_text(t)
                os.replace(tmp, p)
                return True
            except Exception:
                time.sleep(.5*(a+1))
        return False

    with cf.ThreadPoolExecutor(8) as ex:
        ok = list(ex.map(one, codes))
    atomic_json(DATA/"collection.json", dict(source="api.finance.naver.com siseJson requestType=1", start=START, end=END,
                                             codes=len(codes), ok=int(sum(ok))))
    print("fetched", sum(ok), "/", len(codes))


def detect(code, a, start="20250401"):
    d = [x[0] for x in a]
    Hh, C = (np.array([x[i] for x in a], float) for i in (2, 4))
    n = len(C)
    piv, ev, rows = [], {k: None for k in DETECTORS}, []

    def respected(b, hb, m, t):
        xs = np.arange(b, t)
        return not len(xs) or bool((C[b:t] <= hb+m*(xs-b)+1e-9).all())

    for t in range(n):
        hit = dict.fromkeys(DETECTORS, False)
        if t >= 1:
            p5 = [p for p in piv if t-p[0] <= Y5]
            p3 = [p for p in p5 if t-p[0] <= Y3]
            # LTL
            old = [p for p in p5 if t-p[0] > 20]
            if old:
                ia = int(np.argmax([p[1] for p in old]))
                A = old[ia]
                later = [p for p in p5 if p[0] > A[0] and p[1] < A[1] and p[0]-A[0] >= 40 and t-p[0] >= 5]
                if later:
                    B = max(later, key=lambda p: (p[1]-A[1])/(p[0]-A[0]))
                    m = (B[1]-A[1])/(B[0]-A[0])
                    lv = lambda k: B[1]+m*(k-B[0])  # noqa: E731
                    if lv(t) > 0 and respected(B[0], B[1], m, t) and C[t-1] <= lv(t-1) and C[t] > lv(t):
                        hit["LTL"] = True
                        ev["LTL"] = (t, "line", (B[0], B[1], m))
            # LDIAG
            if len(p3) >= 2:
                (a_, ha), (b_, hb) = p3[-2], p3[-1]
                if b_-a_ >= 40:
                    m = (hb-ha)/(b_-a_)
                    lv = lambda k: hb+m*(k-b_)  # noqa: E731
                    if lv(t) > 0 and respected(b_, hb, m, t) and C[t-1] <= lv(t-1) and C[t] > lv(t):
                        hit["LDIAG"] = True
                        ev["LDIAG"] = (t, "line", (b_, hb, m))
            # LPH
            old3 = [p for p in p3 if t-p[0] > 20]
            if old3:
                lvl = max(p[1] for p in old3)
                if C[t-1] <= lvl < C[t]:
                    hit["LPH"] = True
                    ev["LPH"] = (t, "lvl", lvl)
            # HI3Y / HI5Y
            for k_, w in (("HI3Y", Y3), ("HI5Y", Y5)):
                if t >= w+1:
                    lvl, lvl1 = Hh[t-w:t].max(), Hh[t-w-1:t-1].max()
                    if C[t] > lvl and C[t-1] <= lvl1:
                        hit[k_] = True
                        ev[k_] = (t, "lvl", lvl)
        if d[t] >= start and t+1 < n:
            row = dict(code=code, S=d[t], hist=t+1)
            for k_ in DETECTORS:
                row[f"{k_}_today"] = hit[k_]
                e = ev[k_]
                rec = False
                if e is not None and t-e[0] < RECENT:
                    rec = C[t] >= (e[2] if e[1] == "lvl" else e[2][1]+e[2][2]*(t-e[2][0]))
                row[f"{k_}_recent5"] = bool(rec)
            rows.append(row)
        j = t-K
        if j >= K and Hh[j] > Hh[j-K:j].max() and Hh[j] >= Hh[j+1:t+1].max():
            piv.append((j, Hh[j]))
        piv = [p for p in piv if t-p[0] <= Y5+5]
    return rows


def one(item):
    return detect(*item)


def prefix_check(bars, n=6, seed=9):
    rng = np.random.default_rng(seed)
    bad = 0
    for c in rng.choice(sorted(bars), n, replace=False):
        a = bars[c]
        full = pd.DataFrame(detect(c, a)).set_index("S")
        part = pd.DataFrame(detect(c, a[:len(a)-30])).set_index("S")
        common = part.index.intersection(full.index)
        bad += int((full.loc[common] != part.loc[common]).to_numpy().sum())
    return dict(mismatches=bad)


def run(out):
    if (out/"results.json").exists():
        raise SystemExit(f"{out} is frozen")
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    bars = cn.load_bars(DATA)
    pc = prefix_check(bars)
    print("prefix", pc, flush=True)
    with cf.ProcessPoolExecutor(8) as ex:
        parts = list(ex.map(one, bars.items(), chunksize=20))
    B = pd.DataFrame([r for p in parts for r in p])
    f = pd.read_parquet(oc.MR/"own-closing/20261115-v1/panel.parquet")
    f = rk.add_exits(f).merge(B, on=["code", "S"], how="left")
    flags = [f"{k}_{v}" for k in DETECTORS for v in ("today", "recent5")]
    f[flags] = f[flags].fillna(False).astype(bool)
    f["ANY_today"] = f[[f"{k}_today" for k in DETECTORS]].any(axis=1)
    f["ANY_recent5"] = f[[f"{k}_recent5" for k in DETECTORS]].any(axis=1)
    flags += ["ANY_today", "ANY_recent5"]
    f["RULE"] = (f.ld20 > .179) & (f.d5 > .009) & (f.ref_age.clip(upper=12) <= 3) & (np.log10(f.tvmed) > 9.997)
    f = f[f.U & f.period.notna()].copy()
    f.to_parquet(out/"panel.parquet", index=False)
    P = f[f.S >= "20260310"]
    prof = [dict(flag=fl, universe=float(P[fl].mean()), picks=float(P[P.pick][fl].mean()),
                 rule_nonpick=float(P[P.RULE & ~P.pick][fl].mean()), rule_pick=float(P[P.RULE & P.pick][fl].mean()),
                 lift=float(P[P.pick][fl].mean()/max(P[fl].mean(), 1e-9))) for fl in flags]
    sets = {"U": f, "RULE": f[f.RULE], "PICKS": f[f.pick]}
    for fl in flags:
        sets[f"RULE&{fl}"] = f[f.RULE & f[fl]]
        sets[f"U&{fl}"] = f[f[fl]]
        sets[f"PICKS&{fl}"] = f[f.pick & f[fl]]
    res = []
    for name, g in sets.items():
        res.append(dict(set=name, sel="all", per_day=float(len(g)/f.S.nunique()), **ob.evaluate(g)))
        if not name.startswith("PICKS"):
            t3 = g.sort_values("tv", ascending=False).groupby("S").head(oc.TOP_K)
            res.append(dict(set=name, sel="top3", per_day=float(len(t3)/f.S.nunique()), **ob.evaluate(t3)))
    hist = dict(median_sessions=float(f["hist"].median()), share_ge_3y=float((f["hist"] > Y3).mean()),
                share_ge_5y=float((f["hist"] > Y5).mean()))
    atomic_json(out/"results.json", dict(prefix_check=pc, history=hist, profile=prof, results=res))
    print("done", len(res))


def render(code, S, path, bars):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    a = bars[code]
    d = pd.DataFrame(a, columns=["day", "o", "h", "l", "c", "v"])
    d = d[d.day <= S].tail(Y5).reset_index(drop=True)
    d["tv"] = d.c*d.v
    for k in (5, 20, 60, 120):
        d[f"ma{k}"] = d.c.rolling(k).mean()
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(15, 11), gridspec_kw=dict(height_ratios=[5, 4, 1.2]), dpi=100)

    def candles(ax, g, w):
        x = np.arange(len(g))
        up = g.c >= g.o
        ax.vlines(x, g.l, g.h, color=np.where(up, "#d62728", "#1f5fbf"), linewidth=.6)
        ax.bar(x, (g.c-g.o).abs().clip(lower=g.c*1e-4), bottom=np.minimum(g.o, g.c), width=w,
               color=np.where(up, "#d62728", "#1f5fbf"), linewidth=0)
        return x
    x = candles(ax1, d, .8)
    ax1.plot(x, d.ma60, color="#2ca02c", lw=1, label="MA60")
    ax1.plot(x, d.ma120, color="#9467bd", lw=1, label="MA120")
    ax1.set_yscale("log")
    ticks = np.linspace(0, len(d)-1, 10).astype(int)
    ax1.set_xticks(ticks, [d.day[i][:6] for i in ticks], fontsize=8)
    ax1.set_title(f"{code}  daily, {len(d)} sessions up to {S} (log scale)", fontsize=11)
    ax1.legend(loc="upper left", fontsize=8)
    z = d.tail(120).reset_index(drop=True)
    x2 = candles(ax2, z, .7)
    for k, col in ((5, "#ff7f0e"), (20, "#e377c2"), (60, "#2ca02c")):
        ax2.plot(x2, z[f"ma{k}"], color=col, lw=1, label=f"MA{k}")
    t2 = np.linspace(0, len(z)-1, 8).astype(int)
    ax2.set_xticks(t2, [z.day[i][2:] for i in t2], fontsize=8)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.set_title("last 120 sessions", fontsize=10)
    ax3.bar(x2, z.tv/1e8, color="#7f7f7f")
    ax3.set_xticks(t2, [z.day[i][2:] for i in t2], fontsize=8)
    ax3.set_ylabel("turnover (100M KRW)", fontsize=8)
    for ax, xx in ((ax1, x), (ax2, x2), (ax3, x2)):
        ax.axvline(xx[-1], color="black", lw=.8, ls="--")
        ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def render_samples(out, n_each=3, seed=1):
    f = pd.read_parquet(out/"panel.parquet")
    bars = cn.load_bars(DATA)
    rng = np.random.default_rng(seed)
    (out/"charts").mkdir(exist_ok=True)
    picks = []
    groups = {"pick_LTL": f[f.pick & f.LTL_recent5], "pick_none": f[f.pick & ~f.ANY_recent5],
              "rule_nonpick_LTL": f[f.RULE & ~f.pick & f.LTL_recent5 & (f.S >= "20260310")]}
    for name, g in groups.items():
        if not len(g):
            continue
        for r in g.iloc[rng.choice(len(g), min(n_each, len(g)), replace=False)].itertuples():
            p = out/"charts"/f"{name}-{r.code}-{r.S}.png"
            render(r.code, r.S, p, bars)
            picks.append(dict(group=name, code=r.code, S=r.S, file=p.name))
    atomic_json(out/"charts"/"index.json", picks)
    print("rendered", len(picks))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    if args.fetch:
        fetch()
    if args.run:
        run(OUT)
    if args.render:
        render_samples(OUT)


if __name__ == "__main__":
    main()
