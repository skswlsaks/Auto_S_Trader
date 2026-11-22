"""Exit study for our own closing-bet combos: fixed +3/+5% vs bearish-twist model exits, and holding extra days
while the daily trend is up.

Research only. Trades: top-4 combos of own-closing-rank-v1, top 3 per day (S close buy). D..D+3 1m bars from
existing files or the Toss public candle API (regular session). Policies fixed in PROTOCOL before results.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from . import entry_gate_study as gate
from . import own_closing_study as oc
from . import picks_runner_study as runner
from .config import atomic_json, load_config
from .data import KST, fetch_series, normalized_candle, open_store
from .pattern_eval import sha

OUT = oc.MR/"own-closing/20261115-exit-v1"
COST = oc.COST
MAX_SESSIONS = 4
SETS = {
    "R1": ("mom20=0-10 & day=7-15 & volume=3+", "거래대금 급증 돌파형"),
    "R2": ("shape=upper_wick & scan=waiting & size=large", "대형주 윗꼬리 대기형"),
    "R3": ("mom20=10-30 & day=15+ & scan=invalid", "급등 지속형"),
    "R4": ("day=7-15 & shape=upper_wick & market=breadth_hi", "강세장 윗꼬리형"),
}
POLICIES = {
    "TP3": dict(tp=[(.03, 1.)], stop=None, bear=None, hold=1),
    "TP5": dict(tp=[(.05, 1.)], stop=None, bear=None, hold=1),
    "X3": dict(tp=[(.03, .35)], stop=-.03, be_after_tp=True, bear="after_tp", hold=1),
    "BEAR": dict(tp=[], stop=-.03, bear="any", hold=1),
    "BEAR_NS": dict(tp=[], stop=None, bear="any", hold=1),
    "TP5_H": dict(tp=[(.05, 1.)], stop=None, bear=None, hold=MAX_SESSIONS),
    "X3_H": dict(tp=[(.03, .35)], stop=-.03, be_after_tp=True, bear="after_tp", hold=MAX_SESSIONS),
    "BEAR_H": dict(tp=[], stop=-.03, bear="any", hold=MAX_SESSIONS),
    "BEAR_NS_H": dict(tp=[], stop=None, bear="any", hold=MAX_SESSIONS),
}
PROTOCOL = dict(
    version="own-closing-exit-v1",
    question="User 2026-11-15: instead of always selling at +3~5%, let the bearish-twist model decide; if the trend "
             "is already up, consider holding a few more days.",
    trades={k: v[0] for k, v in SETS.items()} | dict(rule="own-closing-rank-v1 combos, top 3 per day by S-day turnover, "
                                                         "S 2025-04-01..2026-11-12, S close buy"),
    minutes="existing per-day files first, else Toss /api/v1/candles 1m raw regular session; entry rescaled by "
            "(D 09:01 1m open / Naver D open) when they differ > 2% (Naver is split-adjusted)",
    policies=POLICIES,
    rules=dict(
        same_bar="stop checked before targets; gap below stop fills at bar open",
        tp="limit at entry*(1+g): a bar whose open >= limit fills at open, else at the limit when high >= limit",
        be_after_tp="after the first target, stop -> entry*(1+cost)",
        bear="bearish transfer model alarm at bar i -> sell the remainder at bar i+1 open (last bar: its close); "
             "'after_tp' only after the first target, 'any' from the first bar",
        carry=f"_H only: at a session's last bar, carry the remainder to the next session if Naver daily close > MA5, "
              f"MA5 > MA20 (that day) and the 1m close > entry; max {MAX_SESSIONS} sessions (D..D+3); otherwise sell "
              f"at the session's last 1m close (15:20 bar)",
        cost=f"{COST} round trip, once per trade"),
    comparison="paired difference vs TP3 and TP5 on the same trades, day-clustered bootstrap",
    periods=oc.PERIODS,
)

# ---------------- trades and calendars ----------------

def build_trades():
    f = pd.read_parquet(oc.MR/"own-closing/20261115-v1/panel.parquet")
    F = oc.families(f)
    rows = []
    for k, (cell, _) in SETS.items():
        g = oc.select(f, F, cell, oc.TOP_K)
        rows.append(g[g.period.notna()].assign(set=k))
    return pd.concat(rows, ignore_index=True)[["set", "code", "S", "D", "period", "close", "Do", "E_open", "pick"]]


def calendars(codes):
    bars = cn.load_bars(cn.Path(oc.SRC))
    cal = {}
    for c in codes:
        a = bars.get(c)
        if not a:
            continue
        d = pd.DataFrame(a, columns=["day", "o", "h", "l", "c", "v"])
        d["ma5"], d["ma20"] = d.c.rolling(5).mean(), d.c.rolling(20).mean()
        d["up"] = (d.c > d.ma5) & (d.ma5 > d.ma20)
        cal[c] = d.set_index("day")
    return cal


def sessions(cal, code, D):
    d = cal[code]
    idx = list(d.index)
    if D not in d.index:
        return []
    i = idx.index(D)
    return idx[i:i+MAX_SESSIONS]


def iso(day):
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"

# ---------------- collection ----------------

def collect(out, trades, cal):
    gate.DAYDIRS = gate.DAYDIRS+[oc.MR/"holdout-picks/20261029-20261113/minutes", out/"minutes"]
    need = []
    for r in trades.drop_duplicates(["code", "D"]).itertuples():
        days = sessions(cal, r.code, r.D)
        miss = [x for x in days if gate.minute_day(r.code, iso(x)) is None]
        if miss:
            need.append((r.code, miss[0], miss[-1]))
    print("stock-days needing minutes", len(need), flush=True)
    c = load_config()
    from .toss import TossAPI
    api = TossAPI(c["credentials_file"], c["api_min_interval_seconds"])
    (out/"minutes").mkdir(parents=True, exist_ok=True)
    log = []
    with open_store(out/"raw.sqlite") as db:
        for n, (code, a, b) in enumerate(need, 1):
            s = datetime.fromisoformat(iso(a)+"T08:00:00").replace(tzinfo=KST)
            e = datetime.fromisoformat(iso(b)+"T20:00:00").replace(tzinfo=KST)
            try:
                info = fetch_series(api, db, code, "1m", False, s.isoformat(), e.isoformat(), 200, progress=lambda *_: None)
            except Exception as ex:  # keep going; a missing day stays missing and is reported
                log.append(dict(code=code, start=a, end=b, error=str(ex)[:200]))
                continue
            rows = [normalized_candle(json.loads(x[0])) for x in db.execute(
                "SELECT raw FROM candles WHERE symbol=? AND interval='1m' AND adjusted=0 AND timestamp>=? AND timestamp<=? "
                "ORDER BY timestamp", (code, s.astimezone(timezone.utc).isoformat(), e.astimezone(timezone.utc).isoformat()))]
            fr = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            if len(fr):
                fr["timestamp"] = pd.to_datetime(fr.timestamp, utc=True)
                day = fr.timestamp.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
                for d, g in fr.groupby(day):
                    p = out/"minutes"/f"{code}-{d}.parquet"
                    if not p.exists():
                        g.to_parquet(p, index=False)
            log.append(dict(code=code, start=a, end=b, rows=len(fr), pages=info.get("pages")))
            if n % 50 == 0:
                print(f"collected {n}/{len(need)}", flush=True)
    atomic_json(out/"collection.json", dict(source="toss_api 1m raw", items=log))

# ---------------- simulation ----------------

def simulate(entry, days, carry_ok, p):
    """days: [(o,h,l,c,bear)] per session. Returns net, reason, sessions held."""
    rem, pnl, done, sl = 1., 0., set(), (entry*(1+p["stop"]) if p.get("stop") is not None else -np.inf)
    for k, (o, h, l, c, bear) in enumerate(days):
        n = len(o)
        for i in range(n):
            if l[i] <= sl:
                pnl += rem*(min(o[i], sl)/entry-1)
                return pnl-COST, ("breakeven" if done else "stop"), k+1
            for j, (g, frac) in enumerate(p["tp"]):
                lim = entry*(1+g)
                if j not in done and h[i] >= lim:
                    px = o[i] if o[i] >= lim else lim
                    take = min(frac, rem)
                    pnl += take*(px/entry-1); rem -= take; done.add(j)
                    if p.get("be_after_tp"):
                        sl = max(sl, entry*(1+COST))
            if rem <= 1e-9:
                return pnl-COST, "target", k+1
            b = p.get("bear")
            if b and bear is not None and bear[i] and (b == "any" or done):
                px = o[i+1] if i+1 < n else c[i]
                pnl += rem*(px/entry-1)
                return pnl-COST, "bear", k+1
        last = k == len(days)-1 or k+1 >= p["hold"]
        if not last and carry_ok[k] and c[-1] > entry:
            continue
        pnl += rem*(c[-1]/entry-1)
        return pnl-COST, ("close" if k == 0 else "close_carry"), k+1
    raise AssertionError("unreachable")


def day_arrays(code, day):
    raw = gate.minute_day(code, iso(day))
    if raw is None:
        return None
    frame, bear = runner.day_frames(raw, code, None, None, entry_model=False)
    if frame is None or bear is None or len(frame) < 300:
        return None
    o, h, l, c = gate.arrays(frame)
    return o, h, l, c, bear


def run_sim(out, trades, cal):
    gate.DAYDIRS = gate.DAYDIRS+[oc.MR/"holdout-picks/20261029-20261113/minutes", out/"minutes"]
    runner._init()
    cache, res, miss = {}, [], []
    for r in trades.itertuples():
        days = sessions(cal, r.code, r.D)
        arr = []
        for d in days:
            key = (r.code, d)
            if key not in cache:
                cache[key] = day_arrays(r.code, d)
            if cache[key] is None:
                break
            arr.append(cache[key])
        if not arr:
            miss.append(dict(set=r.set, code=r.code, D=r.D))
            continue
        ratio = arr[0][0][0]/r.Do if r.Do > 0 else np.nan
        entry = r.close*ratio if np.isfinite(ratio) and abs(ratio-1) > .02 else r.close
        carry = [bool(cal[r.code].loc[d, "up"]) for d in days[:len(arr)]]
        for name, p in POLICIES.items():
            net, reason, held = simulate(entry, arr, carry, p)
            res.append(dict(set=r.set, code=r.code, S=r.S, D=r.D, period=r.period, policy=name, net=net, reason=reason,
                            held=held, sessions_available=len(arr), rescaled=bool(entry != r.close)))
    return pd.DataFrame(res), pd.DataFrame(miss)


def paired(a, b, days, draws=2000, seed=7):
    d = pd.DataFrame(dict(x=a-b, d=days)).groupby("d").x.agg(["sum", "count"])
    idx = np.random.default_rng(seed).integers(0, len(d), (draws, len(d)))
    bs = d["sum"].to_numpy()[idx].sum(1)/d["count"].to_numpy()[idx].sum(1)
    return dict(diff=float((a-b).mean()), ci=[float(np.quantile(bs, .025)), float(np.quantile(bs, .975))])


def summarize(t):
    out = []
    for (s, pol), g in t.groupby(["set", "policy"]):
        for per in ["train", "valid", "eval", "final", "all"]:
            gg = g if per == "all" else g[g.period == per]
            if not len(gg):
                continue
            x = gg.net.to_numpy()
            w, lo = x[x > 0], x[x < 0]
            row = dict(set=s, policy=pol, period=per, n=int(len(gg)), mean=float(x.mean()), median=float(np.median(x)),
                       win=float((x > 0).mean()), pf=float(w.sum()/-lo.sum()) if len(lo) else None,
                       ci=oc.boot_ci(x, gg.S.to_numpy()), worst=float(x.min()), held=float(gg.held.mean()),
                       reasons=gg.reason.value_counts(normalize=True).round(3).to_dict())
            for ref in ("TP3", "TP5"):
                base = t[(t.set == s) & (t.policy == ref)].set_index(["code", "S"]).net
                k = gg.set_index(["code", "S"]).net
                j = k.index.intersection(base.index)
                if pol != ref and len(j) > 5:
                    row[f"vs_{ref}"] = paired(k.loc[j].to_numpy(), base.loc[j].to_numpy(), np.array([i[1] for i in j]))
            out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    trades = build_trades()
    cal = calendars(sorted(trades.code.unique()))
    if args.collect:
        if not (out/"protocol.json").exists():
            atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
        trades.to_parquet(out/"trades-in.parquet", index=False)
        return collect(out, trades, cal)
    if args.simulate:
        t, miss = run_sim(out, trades, cal)
        t.to_parquet(out/"results.parquet", index=False)
        miss.to_csv(out/"missing.csv", index=False)
        atomic_json(out/"summary.json", dict(protocol_sha256=sha(out/"protocol.json"), source_sha256=sha(Path(__file__)),
                                             trades_in=int(len(trades)), simulated=int(t.drop_duplicates(["set", "code", "S"]).shape[0]),
                                             missing=int(len(miss)), rows=summarize(t)))
        print("simulated", len(t), "missing", len(miss))


if __name__ == "__main__":
    main()
