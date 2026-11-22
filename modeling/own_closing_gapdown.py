"""When to sell a closing bet that opens below the entry (gap-down D)?

Research only. Same trades/minutes as own-closing-exit-v1. Only trades whose D first 1m open < entry.
Exits fixed in PROTOCOL before results. Bar i = 09:01 + i minutes (regular session, 380 bars to 15:20).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import own_closing_exit as ex
from . import own_closing_study as oc
from . import picks_runner_study as runner
from .config import atomic_json
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-gapdown-v1"
COST = oc.COST
TIMES = {"T0901": 0, "T0930": 29, "T1000": 59, "T1100": 119, "T1300": 239, "T1420": 319}
PROTOCOL = dict(
    version="own-closing-gapdown-v1",
    sample="own-closing-exit-v1 trades with D first 1m open < entry (R1..R4 reported separately)",
    exits=dict(
        **{k: f"sell at bar {v} open" for k, v in TIMES.items()}, CLOSE="sell at the 15:20 bar close",
        BE="limit at entry*(1+cost) (breakeven), else 15:20 close",
        BE_T1100="limit at breakeven until 11:00, else sell at the 11:00 bar open",
        OPEN2="limit at D open*1.02, else close",
        ORH="after 09:30, limit at the 09:01-09:30 high, else close",
        ORL="after 09:30, sell when price breaks the 09:01-09:30 low (fill min(bar open, low)), else close",
        ORH_ORL="after 09:30, whichever of ORH / ORL comes first, else close",
        P50="current rule: 50% at +3%, 50% at +5%, rest at close (1 day)"),
    limit_fill="bar open >= limit fills at open, else at the limit when high >= limit (touch = fill)",
    split=dict(gap=["0~-2%", "-2~-4%", "<-4%"], periods=oc.PERIODS),
    cost=COST,
)


def exits(entry, o, h, l, c):
    n = len(o)
    r = {k: o[min(v, n-1)] for k, v in TIMES.items()}
    r["CLOSE"] = c[-1]

    def limit(lim, start=0, end=n, fallback=None):
        for i in range(start, end):
            if h[i] >= lim:
                return o[i] if o[i] >= lim else lim
        return c[-1] if fallback is None else fallback

    be = entry*(1+COST)
    r["BE"] = limit(be)
    r["BE_T1100"] = limit(be, 0, 119, o[min(119, n-1)])
    r["OPEN2"] = limit(o[0]*1.02)
    orh, orl = h[:30].max(), l[:30].min()
    r["ORH"] = limit(orh, 30)
    px = c[-1]
    for i in range(30, n):
        if l[i] < orl:
            px = min(o[i], orl)
            break
    r["ORL"] = px
    px = c[-1]
    for i in range(30, n):
        if l[i] < orl:
            px = min(o[i], orl); break
        if h[i] >= orh:
            px = o[i] if o[i] >= orh else orh; break
    r["ORH_ORL"] = px
    out = {k: v/entry-1-COST for k, v in r.items()}
    # P50: 50% at +3%, 50% at +5%, rest close
    rem, pnl = 1., 0.
    for g in (.03, .05):
        lim = entry*(1+g)
        hit = np.flatnonzero(h >= lim)
        if len(hit):
            i = hit[0]
            pnl += .5*((o[i] if o[i] >= lim else lim)/entry-1); rem -= .5
    out["P50"] = pnl+rem*(c[-1]/entry-1)-COST
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if not (out/"protocol.json").exists():
        atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    trades = pd.read_parquet(ex.OUT/"trades-in.parquet")
    gate.DAYDIRS = gate.DAYDIRS+[oc.MR/"holdout-picks/20261029-20261113/minutes", ex.OUT/"minutes"]
    runner._init()
    rows, paths = [], []
    for r in trades.itertuples():
        a = ex.day_arrays(r.code, r.D)
        if a is None:
            continue
        o, h, l, c, _ = a
        ratio = o[0]/r.Do if r.Do > 0 else np.nan
        entry = r.close*ratio if np.isfinite(ratio) and abs(ratio-1) > .02 else r.close
        if o[0] >= entry:
            continue
        gap = o[0]/entry-1
        e = exits(entry, o, h, l, c)
        low_i = int(np.argmin(l)); high_i = int(np.argmax(h))
        rows.append(dict(set=r.set, code=r.code, S=r.S, period=r.period, gap=gap, low_min=low_i, high_min=high_i,
                         day_low=l.min()/entry-1, day_high=h.max()/entry-1, **e))
        if len(c) >= 380:
            paths.append(dict(set=r.set, **{f"m{i}": c[i]/entry-1 for i in range(0, 380, 10)}))
    t = pd.DataFrame(rows)
    t.to_parquet(out/"gapdown.parquet", index=False)
    pd.DataFrame(paths).to_parquet(out/"paths.parquet", index=False)
    cols = list(TIMES)+["CLOSE", "BE", "BE_T1100", "OPEN2", "ORH", "ORL", "ORH_ORL", "P50"]
    t["gapb"] = pd.cut(t.gap, [-1, -.04, -.02, 0], labels=["<-4%", "-2~-4%", "0~-2%"])
    summ = []
    for keys, g in [(("set",), t), (("set", "period"), t), (("set", "gapb"), t)]:
        for k, gg in g.groupby(list(keys), observed=True):
            row = dict(zip(keys, k if isinstance(k, tuple) else (k,)), n=int(len(gg)))
            row |= {c_: float(gg[c_].mean()) for c_ in cols}
            row |= {f"ci_{c_}": oc.boot_ci(gg[c_].to_numpy(), gg.S.to_numpy()) for c_ in ("T0901", "CLOSE", "BE", "P50", "ORH_ORL")}
            row |= dict(low_min_med=float(gg.low_min.median()), high_min_med=float(gg.high_min.median()),
                        day_high=float(gg.day_high.mean()), day_low=float(gg.day_low.mean()))
            summ.append(row)
    atomic_json(out/"summary.json", dict(protocol_sha256=sha(out/"protocol.json"), rows=[
        {k: (None if isinstance(v, float) and v != v else (str(v) if not isinstance(v, (int, float, list, type(None))) else v))
         for k, v in r_.items()} for r_ in summ]))
    print("gap-down trades", len(t))


if __name__ == "__main__":
    main()
