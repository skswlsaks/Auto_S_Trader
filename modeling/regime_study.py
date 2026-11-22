"""Does the S-close + no-risk-flag + X3 combo hold up in bull vs bear markets?

Research only. Re-reads frozen trade lists (picks-runner-v1 and the locked holdout) and splits
them by market regime. Regime rules are fixed below before any split is read, and use only
index data known at the S-day close (the entry time), except the explanatory D-day split.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from .config import atomic_json

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
OUT = MR/"regime/20261115-v1"
TRADES = [(MR/"picks-runner/20261114-v1/trades.parquet", None),
          (MR/"holdout-picks/20261029-20261113/trades.parquet", "holdout")]
PROTOCOL = dict(
    version="regime-v1",
    focus=dict(entry="A_close_F", policy="X3_runner_bear"),
    comparators=[("A_close", "X3_runner_bear"), ("A_close_F", "X0_base"), ("A_close_F", "X2_runner_trail"),
                 ("B_open_F", "X3_runner_bear")],
    regimes=dict(
        trend60="Index at S close vs MA60 and MA60 change over 20 sessions: up = close>MA60 and MA60 rising; "
                "down = close<MA60 and MA60 falling; else side",
        ret20="Index 20-session return at S close: up > +5%, down < -5%, else side",
        dday="(explanatory, not known at entry) index D close / S close - 1: up > +0.5%, down < -0.5%, else flat",
        dgap="(explanatory) index D open / S close - 1: up > +0.3%, down < -0.3%, else flat"),
    indexes=["KOSDAQ (primary)", "KOSPI (secondary, trend60 only)"],
    excess="net - index(S close -> D close); a rough beta check, X3 can hold past D close",
    ci="95% day-clustered bootstrap of mean (2000 draws)")


def index_daily(symbol):
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={symbol}&requestType=1"
           "&startTime=20250101&endTime=20261114&timeframe=day")
    raw = urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "replace")
    (OUT/f"{symbol.lower()}-daily.raw.txt").write_text(raw)
    rows = [json.loads(line.strip().rstrip(",").replace("'", '"')) for line in raw.splitlines()
            if line.strip().startswith('["2')]
    k = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "fr"])
    k["day"] = pd.to_datetime(k.date).dt.strftime("%Y-%m-%d")
    k["ma60"] = k.close.rolling(60).mean()
    slope = k.ma60 - k.ma60.shift(20)
    k["trend60"] = np.select([(k.close > k.ma60) & (slope > 0), (k.close < k.ma60) & (slope < 0)],
                             ["up", "down"], "side")
    r20 = k.close/k.close.shift(20)-1
    k["ret20"] = np.select([r20 > .05, r20 < -.05], ["up", "down"], "side")
    k["next_close_ret"] = k.close.shift(-1)/k.close-1
    k["next_gap"] = k.open.shift(-1)/k.close-1
    k["dday"] = np.select([k.next_close_ret > .005, k.next_close_ret < -.005], ["up", "down"], "flat")
    k["dgap"] = np.select([k.next_gap > .003, k.next_gap < -.003], ["up", "down"], "flat")
    k.loc[k.next_close_ret.isna(), ["dday", "dgap"]] = None
    return k.set_index("day")


def stats(g, rng):
    r, d = g.net.to_numpy(), g.S.to_numpy()
    w, l = r[r > 1e-9], r[r < -1e-9]
    days = np.unique(d)
    by = {x: r[d == x] for x in days}
    boots = []
    for _ in range(2000):
        pick = rng.choice(days, len(days))
        v = np.concatenate([by[x] for x in pick])
        boots.append(v.mean())
    return dict(n=len(r), days=len(days), mean=r.mean(), median=np.median(r),
                pf=w.sum()/-l.sum() if len(l) else np.inf, win=(r > 1e-9).mean(),
                lo=np.percentile(boots, 2.5), hi=np.percentile(boots, 97.5),
                excess=g.excess.mean(), mkt=g.mkt.mean())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", PROTOCOL)
    kq, ks = index_daily("KOSDAQ"), index_daily("KOSPI")
    frames = []
    for path, period in TRADES:
        t = pd.read_parquet(path)
        t = t[t.status == "ok"].copy()
        if period:
            t["period"] = period
        frames.append(t)
    t = pd.concat(frames, ignore_index=True)
    for col in ["trend60", "ret20", "dday", "dgap"]:
        t[col] = t.S.map(kq[col])
    t["kospi_trend60"] = t.S.map(ks["trend60"])
    t["mkt"] = t.S.map(kq.next_close_ret)
    t["excess"] = t.net - t.mkt
    rng = np.random.default_rng(7)
    rows = []
    for entry, policy in [tuple(PROTOCOL["focus"].values())] + PROTOCOL["comparators"]:
        m = t[(t.entry_variant == entry) & (t.policy == policy)]
        rows.append(dict(entry=entry, policy=policy, regime="all", value="all", period="all", **stats(m, rng)))
        for reg in ["trend60", "ret20", "kospi_trend60", "dgap", "dday"]:
            for val, g in m.groupby(reg):
                rows.append(dict(entry=entry, policy=policy, regime=reg, value=val, period="all", **stats(g, rng)))
                for per, gp in g.groupby("period"):
                    if len(gp) >= 5:
                        rows.append(dict(entry=entry, policy=policy, regime=reg, value=val, period=per,
                                         **stats(gp, rng)))
    res = pd.DataFrame(rows)
    res.to_csv(OUT/"summary.csv", index=False)
    t.to_parquet(OUT/"trades_with_regime.parquet")
    # market backdrop
    m = kq.loc["2026-03-01":"2026-11-14"]
    month = m.close.groupby(m.index.str[:7]).agg(["first", "last"])
    month["ret"] = month["last"]/month["first"]-1
    reg_days = m.groupby(m.index.str[:7]).trend60.agg(lambda s: s.value_counts().to_dict())
    print("KOSDAQ by month:\n", pd.concat([month.ret.round(4), reg_days], axis=1).to_string())
    pd.set_option("display.width", 250)
    show = res.copy()
    for c in ["mean", "median", "lo", "hi", "excess", "mkt"]:
        show[c] = (show[c]*100).round(2)
    show[["pf", "win"]] = show[["pf", "win"]].round(2)
    print(show.to_string(index=False))


if __name__ == "__main__":
    main()
