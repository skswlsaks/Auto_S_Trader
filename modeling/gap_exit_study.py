"""Gap-down early exit on top of S-close + no-risk-flag + X3.

Research only. Variants and the adoption rule are fixed in PROTOCOL before any result is read.
The bar-0 (09:00-09:01) management uses the frozen X3 rules; an early exit fills at the 09:01 bar
open (o[1]) because the KOSDAQ index open and the stock open are only known once the 09:00 auction prints.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import picks_runner_study as runner
from .config import atomic_json, load_config
from .pattern_eval import sha
from .regime_study import MR, index_daily

OUT = MR/"gap-exit/20261115-v1"
HOLD = MR/"holdout-picks/20261029-20261113"
PROTOCOL = dict(
    version="gap-exit-v1",
    base=dict(entry="A_close_F", policy="X3_runner_bear"),
    variants=dict(
        X3="base, recomputed; must equal frozen picks-runner/holdout net",
        K03="KOSDAQ D open / S close - 1 < -0.3% -> after bar 0 under X3 rules, sell rest at 09:01 bar open",
        K10="same with -1.0%",
        SG="stock's own D open < S close -> same early exit",
        K03_ref="K03 but filled at the 09:00 auction open (not executable; reference upper bound)"),
    adoption="A variant is adopted only if its mean beats X3 on pre AND on post. Ties/fails -> keep X3. "
             "Holdout is reported, never used to choose.",
    periods=dict(pre="D <= 2026-08-26", post="2026-08-27..2026-10-28", holdout="2026-10-29..2026-11-13"),
    cost=runner.COST)


def run_until(entry, stop, o, h, l, c, bear, cut, exit_px, p):
    """X3 on bars [0, cut), then sell the remainder at exit_px."""
    c2 = c[:cut].copy()
    c2[-1] = exit_px
    return runner.simulate(entry, stop, [(o[:cut], h[:cut], l[:cut], c2, bear[:cut], 0)], p)[0]


def process(task, kq):
    pop, symbol, S, D, D1, daily, s_close, meta = task
    if runner.risk_flag(meta) or s_close is None or not np.isfinite(s_close):
        return None
    raw = gate.minute_day(symbol, D)
    if raw is None:
        return None
    r, bear = runner.day_frames(raw, symbol, daily, load_config()["execution"], entry_model=False)
    if r is None or bear is None:
        return None
    o, h, l, c = gate.arrays(r)
    p = runner.POLICIES["X3_runner_bear"]
    entry, stop = s_close, s_close*.97
    base = runner.simulate(entry, stop, [(o, h, l, c, bear, 0)], p)[0]
    kgap = kq.at[D, "open"]/kq.at[S, "close"]-1 if D in kq.index and S in kq.index else np.nan
    early = run_until(entry, stop, o, h, l, c, bear, 1, o[1], p)
    ref = o[0]/entry-1-runner.COST
    out = dict(symbol=symbol, S=S, D=D, kgap=kgap, sgap=o[0]/entry-1, X3=base)
    out["K03"] = early if kgap < -.003 else base
    out["K10"] = early if kgap < -.010 else base
    out["SG"] = early if o[0] < entry else base
    out["K03_ref"] = ref if kgap < -.003 else base
    return out


def collect(kq, period_filter):
    k = gate.kosdaq(OUT)
    tasks, _ = gate.pick_tasks(k)
    rows = [x for t in tasks if period_filter(t[3]) and (x := process(t, kq)) is not None]
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(__import__("pathlib").Path(__file__))))
    kq = index_daily("KOSDAQ")
    runner._init()
    a = collect(kq, lambda d: d <= "2026-10-28")
    a["period"] = np.where(a.D > gate.CUTOFF, "post", "pre")
    gate.DAYDIRS, gate.DAILYDIRS, gate.CANDLES = [HOLD/"minutes"], [HOLD/"daily"], HOLD/"no-full-series"
    b = collect(kq, lambda d: "2026-10-29" <= d <= "2026-11-13")
    b["period"] = "holdout"
    t = pd.concat([a, b], ignore_index=True)
    # reproduce frozen X3 net
    frozen = pd.concat([pd.read_parquet(MR/"picks-runner/20261114-v1/trades.parquet"), pd.read_parquet(HOLD/"trades.parquet")])
    frozen = frozen[(frozen.entry_variant == "A_close_F") & (frozen.policy == "X3_runner_bear")][["symbol", "D", "net"]]
    chk = t.merge(frozen, on=["symbol", "D"], how="outer", indicator=True)
    mism = chk[(chk._merge != "both") | ~np.isclose(chk.X3, chk.net)]
    print("reproduce X3:", len(chk), "rows,", len(mism), "mismatch")
    t.to_parquet(OUT/"trades.parquet", index=False)
    rows = []
    for per in ["pre", "post", "holdout", "all"]:
        g = t if per == "all" else t[t.period == per]
        for v in PROTOCOL["variants"]:
            x = g[v].to_numpy()
            w, lo = x[x > 1e-9], x[x < -1e-9]
            rows.append(dict(period=per, variant=v, n=len(x), mean=x.mean()*100, median=np.median(x)*100,
                             win=(x > 1e-9).mean(), pf=w.sum()/-lo.sum() if len(lo) else np.inf,
                             worst=x.min()*100, changed=int((g[v] != g.X3).sum()),
                             changed_delta=(g[v]-g.X3).sum()*100))
    res = pd.DataFrame(rows).round(3)
    res.to_csv(OUT/"summary.csv", index=False)
    pd.set_option("display.width", 200)
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()
