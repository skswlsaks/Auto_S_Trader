"""Step 1c: hold momentum + sector-strength names for several days instead of one night.

Research only. Signals come from our own closing scan (no instructor input), so the S-close entry
is executable in principle (15:20 auction, provisional-bar caveat). Exits, sets and the pass bar are
fixed in PROTOCOL before results are read. Daily bars only (Naver, split-adjusted).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from . import momentum_trend_study as mt
from . import theme_momentum_study as tm
from .config import atomic_json
from .pattern_eval import sha

OUT = tm.MR/"swing-hold/20261115-v1"
COST = .0025
EXITS = {
    "E1_ma20": dict(stop=.07, ma20=True, trail=None, hold=20),
    "E2_trail10": dict(stop=.07, ma20=False, trail=.10, hold=20),
    "E3_hold5": dict(stop=.07, ma20=False, trail=None, hold=5),
    "E4_hold10": dict(stop=.07, ma20=False, trail=None, hold=10),
}
PROTOCOL = dict(
    version="swing-hold-v1",
    hypothesis="User 2026-11-15: an intact stock uptrend plus a strong sector (e.g. semis Sep-Oct) pays over days, not overnight",
    entry="Buy at S close when the stock is in the set on S. One open trade per stock (signals while holding are skipped)",
    sets=dict(LIQ="all liquid stocks (base rate)", R1="closing scan, no risk flag", SM="R1 + stock momentum (momentum-trend-v1)",
              SM_TT="SM + sector relative strength (momentum-trend-v1)", SM_TT_TOP="SM_TT top 3 per day by th20_rel"),
    exits={k: v for k, v in EXITS.items()},
    exit_rules=dict(stop="initial stop = entry x (1-7%); if a day's low <= stop, fill at min(open, stop) (checked first)",
                    ma20="E1: exit at the close of the first day whose close < MA20",
                    trail="E2: stop raised to 90% of the highest close since entry",
                    hold="exit at the close of the N-th session after entry",
                    end="trades still open at 2026-11-13 are marked at that close and kept (flag open=True)",
                    cost=COST),
    periods=dict(train="S <= 2026-06-30", eval="2026-07-01..2026-10-27 (decision)", late="S >= 2026-10-28 (report only)"),
    pass_bar="A set x exit passes if on eval: per-trade mean > 0 with day-clustered 95% CI low > 0, AND mean beats R1 with "
             "the same exit by >= 0.5%p, AND train mean > 0. 4 sets x 4 exits are tried: a marginal pass is exploratory.",
    portfolio="report only: max 5 positions, 20% each, new signals taken in th20_rel order while slots are free, exit E*",
)


def series(f):
    out = {}
    for code, g in f.sort_values("S").groupby("code"):
        out[code] = dict(S=g.S.to_numpy(), C=g.close.to_numpy(float), d20=g.d20.to_numpy(float),
                         O=g.Do.to_numpy(float), H=g.Dh.to_numpy(float), L=g.Dl.to_numpy(float), Cn=g.Dc.to_numpy(float),
                         D=g.D.to_numpy())
    return out


def simulate(s, i, p):
    """Entry at close of row i. Day k (k=1..) bar = next-day OHLC stored on row i+k-1."""
    entry = s["C"][i]
    stop = entry*(1-p["stop"])
    peak = entry
    n = len(s["C"])
    for k in range(1, p["hold"]+1):
        j = i+k-1
        if j >= n or not np.isfinite(s["O"][j]):
            if j >= n:
                last, day = (s["Cn"][n-1], s["D"][n-1]) if np.isfinite(s["Cn"][n-1]) else (s["C"][n-1], s["S"][n-1])
            else:
                last, day = s["C"][j], s["S"][j]
            return last/entry-1-COST, k-1, "open", day
        o, h, l, c = s["O"][j], s["H"][j], s["L"][j], s["Cn"][j]
        if l <= stop:
            return min(o, stop)/entry-1-COST, k, "stop", s["D"][j]
        peak = max(peak, c)
        if p["trail"]:
            stop = max(stop, peak*(1-p["trail"]))
        if p["ma20"] and j+1 < n and s["d20"][j+1] < 0:  # row j+1 is day k's close
            return c/entry-1-COST, k, "ma20", s["D"][j]
    return c/entry-1-COST, p["hold"], "time", s["D"][j]


def run(sig, ser, p):
    rows = []
    for code, g in sig.groupby("code"):
        s = ser[code]
        idx = {d: k for k, d in enumerate(s["S"])}
        free_after = ""
        for S in sorted(g.S):
            if S <= free_after:
                continue
            net, days, why, exit_day = simulate(s, idx[S], p)
            rows.append(dict(code=code, S=S, net=net, days=days, reason=why, exit_day=exit_day))
            free_after = exit_day
    return pd.DataFrame(rows)


def portfolio(trades, sig, slots=5):
    """Equal 1/slots weight, take signals in th20_rel order while slots are free. Returns compounded equity."""
    t = trades.merge(sig[["code", "S", "th20_rel"]], on=["code", "S"]).sort_values(["S", "th20_rel"], ascending=[True, False])
    open_, eq, taken, curve = [], 1., 0, []
    for S, g in t.groupby("S"):
        done = [x for x in open_ if x["exit_day"] <= S]
        for x in done:
            eq += x["w"]*x["net"]; open_.remove(x)
        for r in g.itertuples(index=False):
            if len(open_) >= slots:
                break
            open_.append(dict(exit_day=r.exit_day, net=r.net, w=eq/slots)); taken += 1
        curve.append((S, eq))
    for x in open_:
        eq += x["w"]*x["net"]
    c = pd.Series([v for _, v in curve], index=[k for k, _ in curve])
    return dict(final=eq-1, trades=taken, maxdd=float((c/c.cummax()-1).min()) if len(c) else None)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    f = pd.read_parquet(tm.SRC/"features.parquet")
    f = f.merge(mt.sector_scores(f, tm.load_themes()), on=["code", "S"], how="left")
    f["SM"] = (f.r20 > 0) & (f.d20 > 0) & (f.ma20slope > 0) & (f.hd60 >= -.10)
    f["TT"] = (f.th20_rel > 0) & (f.th_above >= .5)
    ser = series(f)
    r1 = f[f.R1]
    smtt = r1[r1.SM & r1.TT]
    sets = dict(LIQ=f[f.liquid], R1=r1, SM=r1[r1.SM], SM_TT=smtt, SM_TT_TOP=tm.topk(smtt, "th20_rel"))
    per = lambda S: np.where(S <= "20260630", "train", np.where(S <= "20261027", "eval", "late"))
    rows, allt, port = [], [], []
    for sname, sig in sets.items():
        for ename, p in EXITS.items():
            t = run(sig, ser, p).assign(set=sname, exit=ename)
            t["period"] = per(t.S)
            allt.append(t)
            for pp in ("train", "eval", "late"):
                g = t[t.period == pp]
                st = cn.stats(g.net, g.S)
                rows.append(dict(set=sname, exit=ename, period=pp, n=st.get("n"), mean=st.get("mean"), ci=st.get("ci"),
                                 win=st.get("win"), pf=st.get("pf"), days_held=g.days.mean(),
                                 stop_share=(g.reason == "stop").mean(), open=int((g.reason == "open").sum())))
            if sname in ("SM_TT", "SM_TT_TOP", "R1"):
                for pp in ("train", "eval"):
                    tt = t[t.period == pp]
                    port.append(dict(set=sname, exit=ename, period=pp, **portfolio(tt, sig)))
    res = pd.DataFrame(rows)
    pd.concat(allt, ignore_index=True).to_parquet(OUT/"trades.parquet", index=False)
    res.to_csv(OUT/"summary.csv", index=False)
    pt = pd.DataFrame(port)
    pt.to_csv(OUT/"portfolio.csv", index=False)
    # pass check
    ev = res[res.period == "eval"].set_index(["set", "exit"])
    tr = res[res.period == "train"].set_index(["set", "exit"])
    passed = [dict(set=s, exit=e) for (s, e), r in ev.iterrows()
              if s not in ("LIQ", "R1") and r["mean"] > 0 and r["ci"] and r["ci"][0] > 0
              and r["mean"] >= ev.loc[("R1", e), "mean"]+.005 and tr.loc[(s, e), "mean"] > 0]
    atomic_json(OUT/"summary.json", dict(results=json.loads(res.to_json(orient="records")),
                                         portfolio=json.loads(pt.to_json(orient="records")), passed=passed))
    show = res.copy()
    show["mean"] = (show["mean"]*100).round(2)
    show["ci"] = show.ci.map(lambda v: f"[{v[0]*100:+.2f},{v[1]*100:+.2f}]" if isinstance(v, list) else "")
    show[["win", "pf", "days_held", "stop_share"]] = show[["win", "pf", "days_held", "stop_share"]].astype(float).round(2)
    pd.set_option("display.width", 220)
    print(show.to_string(index=False))
    pt[["final", "maxdd"]] = (pt[["final", "maxdd"]]*100).round(1)
    print(pt.to_string(index=False))
    print("passed:", passed)


if __name__ == "__main__":
    main()
