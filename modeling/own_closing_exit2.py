"""Exit study v2: partial sells x open-based cut rules x trend carry, same trades and minutes as own-closing-exit-v1.

Research only. User 2026-11-15: "partial sells, and if it starts below at the open, dump it".
Two readings are tested and fixed in PROTOCOL before results:
  GO  = gap-out: if a session's first 1m open < entry, sell everything at that open.
  OS1 = open stop: stop at (session's first 1m open) x 0.99 from the first bar.
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import own_closing_exit as ex
from . import own_closing_study as oc
from . import picks_runner_study as runner
from .config import atomic_json
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-exit-v2"
COST = oc.COST
SPLITS = {"TP5": [(.05, 1.)], "P35": [(.03, .35), (.05, .35)], "P50": [(.03, .5), (.05, .5)]}
CUTS = {"none": (False, False), "GO": (True, False), "OS1": (False, True), "GO+OS1": (True, True)}
HOLDS = {"1d": 1, "H": ex.MAX_SESSIONS}
POLICIES = {f"{s}|{c}|{h}": dict(tp=SPLITS[s], go=CUTS[c][0], os1=CUTS[c][1], hold=HOLDS[h])
            for s, c, h in itertools.product(SPLITS, CUTS, HOLDS)}
PROTOCOL = dict(
    version="own-closing-exit-v2",
    trades="own-closing-exit-v1 trades-in.parquet (R1..R4, top 3 per day, S close buy)",
    minutes="own-closing-exit-v1 minutes + existing per-day files",
    splits={k: str(v) for k, v in SPLITS.items()},
    cuts=dict(none="no stop", GO="session first 1m open < entry -> sell all at that open",
              OS1="stop = session first 1m open x 0.99, fill at min(bar open, stop)",
              both="GO then OS1"),
    holds=dict(**{"1d": "sell remainder at D 15:20 close"},
               H=f"carry remainder if Naver daily close > MA5, MA5 > MA20 and 1m close > entry; max {ex.MAX_SESSIONS} "
                 "sessions; targets stay live; GO/OS1 re-applied at each session's open"),
    tp_fill="bar open >= limit fills at open, else at the limit when high >= limit (touch = fill, optimistic)",
    baseline="TP5|none|1d (= own-closing-exit-v1 TP5)",
    cost=COST,
    note="24 variants on trades already ranked on all periods: exploratory; read paired differences, not best-of.",
)


def simulate(entry, days, carry_ok, p):
    rem, pnl, done = 1., 0., set()
    for k, (o, h, l, c, _) in enumerate(days):
        if p["go"] and o[0] < entry:
            pnl += rem*(o[0]/entry-1)
            return pnl-COST, "gap_out", k+1
        sl = o[0]*.99 if p["os1"] else -np.inf
        for i in range(len(o)):
            if l[i] <= sl:
                pnl += rem*(min(o[i], sl)/entry-1)
                return pnl-COST, "open_stop", k+1
            for j, (g, frac) in enumerate(p["tp"]):
                lim = entry*(1+g)
                if j not in done and h[i] >= lim:
                    take = min(frac, rem)
                    pnl += take*((o[i] if o[i] >= lim else lim)/entry-1); rem -= take; done.add(j)
            if rem <= 1e-9:
                return pnl-COST, "target", k+1
        last = k == len(days)-1 or k+1 >= p["hold"]
        if not last and carry_ok[k] and c[-1] > entry:
            continue
        pnl += rem*(c[-1]/entry-1)
        return pnl-COST, ("close" if k == 0 else "close_carry"), k+1
    raise AssertionError("unreachable")


def run(out):
    v1 = ex.OUT
    trades = pd.read_parquet(v1/"trades-in.parquet")
    cal = ex.calendars(sorted(trades.code.unique()))
    gate.DAYDIRS = gate.DAYDIRS+[oc.MR/"holdout-picks/20261029-20261113/minutes", v1/"minutes"]
    runner._init()
    cache, res = {}, []
    for r in trades.itertuples():
        days = ex.sessions(cal, r.code, r.D)
        arr = []
        for d in days:
            if (r.code, d) not in cache:
                cache[(r.code, d)] = ex.day_arrays(r.code, d)
            if cache[(r.code, d)] is None:
                break
            arr.append(cache[(r.code, d)])
        if not arr:
            continue
        ratio = arr[0][0][0]/r.Do if r.Do > 0 else np.nan
        entry = r.close*ratio if np.isfinite(ratio) and abs(ratio-1) > .02 else r.close
        carry = [bool(cal[r.code].loc[d, "up"]) for d in days[:len(arr)]]
        for name, p in POLICIES.items():
            net, reason, held = simulate(entry, arr, carry, p)
            res.append(dict(set=r.set, code=r.code, S=r.S, period=r.period, policy=name, net=net, reason=reason, held=held))
    return pd.DataFrame(res)


def summarize(t):
    rows = []
    base = t[t.policy == "TP5|none|1d"].set_index(["set", "code", "S"]).net
    for (s, pol), g in t.groupby(["set", "policy"]):
        x = g.net.to_numpy()
        w, lo = x[x > 0], x[x < 0]
        k = g.set_index(["set", "code", "S"]).net
        row = dict(set=s, policy=pol, n=int(len(g)), mean=float(x.mean()), median=float(np.median(x)), win=float((x > 0).mean()),
                   pf=float(w.sum()/-lo.sum()) if len(lo) else None, worst=float(x.min()), p05=float(np.quantile(x, .05)),
                   held=float(g.held.mean()), ci=oc.boot_ci(x, g.S.to_numpy()),
                   reasons=g.reason.value_counts(normalize=True).round(3).to_dict(),
                   **{f"m_{p}": float(g[g.period == p].net.mean()) if (g.period == p).any() else None
                      for p in ["train", "valid", "eval", "final"]})
        if pol != "TP5|none|1d":
            row["vs_TP5"] = ex.paired(k.to_numpy(), base.loc[k.index].to_numpy(), np.array([i[2] for i in k.index]))
        # equal-weight daily compounding (unit curve) for drawdown
        d = g.groupby("S").net.mean().sort_index()
        eq = (1+d).cumprod()
        row["mdd"] = float((eq/eq.cummax()-1).min())
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if not (out/"protocol.json").exists():
        atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    t = run(out)
    t.to_parquet(out/"results.parquet", index=False)
    atomic_json(out/"summary.json", dict(protocol_sha256=sha(out/"protocol.json"), rows=summarize(t)))
    print("done", len(t))


if __name__ == "__main__":
    main()
