"""Plan 3+6 study: open-reclaim gate x frozen twist detectors x KOSDAQ regime gate.

Research only. Entry/exit rules below are fixed before results are read (protocol.json).
Exit is the same simple percent policy used in the 2026-11-14 s_trader91 entry study so
numbers are comparable with that table; it is NOT the volume-capped Codex executor.
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .config import atomic_json, load_config
from .daytrade import session_bars
from .liquid_research import FEATURES, OUTPUT as LIQUID, validate
from .liquid_trend import alarms, daily_context, intraday_context
from .pattern_eval import sha
from .shape_v2 import contract as shape_contract, detect

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
WM = MR/"whole-market/20261030-v1"
DAYDIRS = [WM/"minutes", MR/"daytrade/20261030-v1/minutes"]
DAILYDIRS = [WM/"daily", MR/"data/candles"]
CANDLES = MR/"data/candles"
PICKS = ROOT/"outputs/research/s_trader91_picks_20261114"
OUT = MR/"entry-gate/20261114-v1"
CUTOFF = "2026-08-26"

PROTOCOL = dict(
    version="entry-gate-v1",
    purpose="Plan 3 (open-reclaim gate x twist detector) + Plan 6 (KOSDAQ regime gate)",
    windows=dict(W1=[541, 630], W2=[781, 870], W3=[901, 919]),
    window_note="Signal bar END minute (KST) 09:01-10:30, 13:01-14:30, 15:01-15:19; fill at next bar open. "
                "15:20-15:30 closing auction and NXT after-market have no 1m bars in this dataset.",
    entries=dict(
        A_close="Buy S-day close (picks only; reference)",
        B_open="Buy D 09:01 bar open (reference)",
        G1="Low since open <= open*(1-0.5%), then a bar whose close crosses back above open "
           "(previous close <= open, or same-bar break and close above). Event, not state "
           "(fixed after smoke test showed state firing at every window start).",
        M1="Frozen liquid-trend classifier alarm (threshold .5, minute filter on), first in window",
        M2="shape_v2 rule candidate rising edge, first in window",
        M1o="M1 with signal close >= D open",
        G1M1="M1 at/after a G1 reclaim, signal close >= D open",
        G1M2="M2 at/after a G1 reclaim, signal close >= D open"),
    stops=dict(A_close="-3%", B_open="-3%", G1="lowest low since open",
               model="(pullback_low or recent_low5) - .1*ATR", clamp="stop in [entry*0.97, entry*0.997]"),
    exit=dict(cost_round_trip=.0025, breakeven="after high >= entry*1.005, stop -> entry*(1+cost)",
              targets="35% at +3%, 35% at +5% (gross)", same_bar="stop checked before targets",
              remainder="D 15:20 bar close; W3_overnight variant carries to D+1 15:20 close"),
    regime=dict(source="Naver KOSDAQ index daily", gate_ok="S close > MA20(S) and S-day return > -1.5%",
                known_at="S close, before D open"),
    populations=dict(picks="s_trader91 picks with complete D regular 1m bars",
                     watch="whole-market watchlist stock-days (our daily selection)"),
    periods=dict(pre=f"D <= {CUTOFF} (classifier in-sample, retrospective)", post=f"D > {CUTOFF}"),
    one_entry="First qualifying signal per stock-day per scope; scopes W1/W2/W3 independent, ALL = first across windows",
)
COST = .0025
VARIANTS = ["G1", "M1", "M2", "M1o", "G1M1", "G1M2"]


# ---------- data ----------
def day_path(code, day):
    for d in DAYDIRS:
        p = d/f"{code}-{day}.parquet"
        if p.exists():
            return p
    return None


_full = {}


def minute_day(code, day):
    """Raw 1m bars for one KST day, from per-day files or the 206-symbol full series."""
    p = day_path(code, day)
    if p is not None:
        return pd.read_parquet(p)
    full = CANDLES/f"{code}-1m-raw.parquet"
    if not full.exists():
        return None
    if code not in _full:
        f = pd.read_parquet(full)
        f["kst_day"] = pd.to_datetime(f.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
        _full.clear()
        _full[code] = f
    f = _full[code]
    x = f[f.kst_day == day].drop(columns="kst_day")
    return x if len(x) else None


def daily_frames(code):
    for d in DAILYDIRS:
        a, r = d/f"{code}-1d-adjusted.parquet", d/f"{code}-1d-raw.parquet"
        if a.exists() and r.exists():
            return pd.read_parquet(a), pd.read_parquet(r)
    return None


def kosdaq(output):
    import urllib.request
    url = ("https://api.finance.naver.com/siseJson.naver?symbol=KOSDAQ&requestType=1"
           "&startTime=20250101&endTime=20261114&timeframe=day")
    raw = urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "replace")
    (output/"kosdaq-daily.raw.txt").write_text(raw)
    rows = [json.loads(line.strip().rstrip(",").replace("'", '"')) for line in raw.splitlines()
            if line.strip().startswith('["2')]
    k = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "fr"])
    k["day"] = pd.to_datetime(k.date).dt.strftime("%Y-%m-%d")
    k["ma20"] = k.close.rolling(20).mean()
    k["ret"] = k.close.pct_change()
    k["gate_ok"] = (k.close > k.ma20) & (k.ret > -.015)
    return k[["day", "close", "ma20", "ret", "gate_ok"]]


# ---------- simulation ----------
def run_exit(entry, stop, segs):
    rem, pnl, t1, t2, armed, sl = 1., 0., False, False, False, stop
    for o, h, l, c, start in segs:
        for i in range(start, len(o)):
            if l[i] <= sl:
                pnl += rem*(min(o[i], sl)/entry-1)
                return pnl-COST, "breakeven" if armed else "stop", t1, t2
            if not t1 and h[i] >= entry*1.03:
                pnl += .35*.03; rem -= .35; t1 = armed = True
            if not t2 and h[i] >= entry*1.05:
                pnl += .35*.05; rem -= .35; t2 = True
            if not armed and h[i] >= entry*1.005:
                armed = True
            if armed:
                sl = max(sl, entry*(1+COST))
    o, h, l, c, _ = segs[-1]
    pnl += rem*(c[-1]/entry-1)
    return pnl-COST, "close", t1, t2


def regular(frame):
    r = frame[frame.minute.between(541, 920)].reset_index(drop=True)
    return r if len(r) == 380 and r.minute.iat[0] == 541 else None


def arrays(r):
    return r.open.to_numpy(float), r.high.to_numpy(float), r.low.to_numpy(float), r.close.to_numpy(float)


_model = None


def _init():
    global _model
    _model = joblib.load(LIQUID/"model.joblib")


def signals(raw, symbol, daily, execution):
    bars = session_bars(raw)
    f = intraday_context(detect(bars, symbol), daily, execution)
    up = alarms(_model.predict_proba(f[FEATURES])[:, 1], f, True)
    cand = f.candidate.to_numpy(bool)
    seg_same = f.segment.eq(f.segment.shift()).to_numpy()
    rule = cand & ~(np.r_[False, cand[:-1]] & seg_same)
    f = f.assign(up=up, rule=rule)
    return regular(f)


def first_entries(r):
    """Return {(variant, scope): (signal_index, stop)} for intraday variants."""
    o, h, l, c = arrays(r)
    m = r.minute.to_numpy()
    d0 = o[0]
    lo = np.minimum.accumulate(l)
    broke = np.maximum.accumulate(lo <= d0*(1-.005))
    prev_below = np.r_[True, c[:-1] <= d0]
    reclaim = broke & (c > d0) & (prev_below | (l <= d0*(1-.005)))  # cross back above open (event)
    reclaimed = np.maximum.accumulate(reclaim)
    support = np.where(r.pullback_low.notna(), r.pullback_low, r.recent_low5)
    mstop = support-.1*r.atr_pct.to_numpy(float)*c
    up, rule = r.up.to_numpy(bool), r.rule.to_numpy(bool)
    conds = dict(G1=(reclaim, lo), M1=(up, mstop), M2=(rule, mstop), M1o=(up & (c >= d0), mstop),
                 G1M1=(up & reclaimed & (c >= d0), mstop), G1M2=(rule & reclaimed & (c >= d0), mstop))
    out = {}
    for v, (mask, stops) in conds.items():
        for w, (a, b) in PROTOCOL["windows"].items():
            idx = np.flatnonzero(mask & (m >= a) & (m <= b) & (np.arange(len(m)) < len(m)-1))
            if len(idx):
                out[(v, w)] = (int(idx[0]), float(stops[idx[0]]))
        alls = [out[(v, w)] for w in PROTOCOL["windows"] if (v, w) in out]
        if alls:
            out[(v, "ALL")] = min(alls)
    return out


def clamp(stop, entry):
    if not np.isfinite(stop):
        stop = entry*.97
    return min(max(stop, entry*.97), entry*.997)


def process(task):
    pop, symbol, S, D, D1, daily, s_close, meta = task
    raw = minute_day(symbol, D)
    if raw is None:
        return [dict(pop=pop, symbol=symbol, D=D, status="no_minutes")]
    try:
        r = signals(raw, symbol, daily, load_config()["execution"])
    except Exception as e:  # keep failures visible
        return [dict(pop=pop, symbol=symbol, D=D, status=f"error:{type(e).__name__}:{e}")]
    if r is None:
        return [dict(pop=pop, symbol=symbol, D=D, status="incomplete_regular_session")]
    o, h, l, c = arrays(r)
    nxt = None
    if D1:
        raw1 = minute_day(symbol, D1)
        if raw1 is not None:
            b1 = session_bars(raw1)
            r1 = regular(b1)
            if r1 is not None:
                nxt = (*arrays(r1), 0)
    base = dict(pop=pop, symbol=symbol, S=S, D=D, status="ok", day_open=o[0], **meta)
    rows = []

    def add(variant, scope, idx, entry, stop, segs, minute):
        net, reason, t1, t2 = run_exit(entry, stop, segs)
        rows.append(dict(base, variant=variant, scope=scope, signal_index=idx, signal_minute=minute,
                         entry=entry, stop=stop, net=net, reason=reason, tp3=t1, tp5=t2))

    if s_close is not None and np.isfinite(s_close):
        add("A_close", "ALL", -1, s_close, s_close*.97, [(o, h, l, c, 0)], 540)
    add("B_open", "ALL", 0, o[0], o[0]*.97, [(o, h, l, c, 0)], 541)
    for (v, w), (i, st) in first_entries(r).items():
        entry = o[i+1]
        stop = clamp(st, entry)
        add(v, w, i, entry, stop, [(o, h, l, c, i+1)], int(r.minute.iat[i]))
        if w == "W3":
            if nxt is not None:
                add(v, "W3_overnight", i, entry, stop, [(o, h, l, c, i+1), nxt], int(r.minute.iat[i]))
            else:
                rows.append(dict(base, variant=v, scope="W3_overnight", status="no_next_day_minutes"))
    return rows


# ---------- tasks ----------
def trading_days(k):
    days = k.day.tolist()
    return {d: (days[i+1] if i+1 < len(days) else None) for i, d in enumerate(days)}


def pick_tasks(k):
    nxt = trading_days(k)
    a = pd.read_csv(PICKS/"picks_features.csv", dtype={"code": str})
    b = pd.read_csv(PICKS/"picks_new_features.csv", dtype={"code": str})
    a["src"], b["src"] = "old", "new"
    cols = ["code", "name", "S일_직전종가", "D일_게시후첫장", "status", "ref_age", "chg1", "src"]
    p = pd.concat([a[cols], b[cols]], ignore_index=True).dropna(subset=["code"])
    p["code"] = p.code.str.zfill(6)
    fmt = lambda x: f"{str(int(x))[:4]}-{str(int(x))[4:6]}-{str(int(x))[6:]}"
    p["S"], p["D"] = p["S일_직전종가"].map(fmt), p["D일_게시후첫장"].map(fmt)
    p = p.drop_duplicates(["code", "D"])
    tasks, missing = [], []
    cache = {}
    for row in p.itertuples(index=False):
        if row.code not in cache:
            fr = daily_frames(row.code)
            cache[row.code] = None if fr is None else (daily_context(*fr), fr[1])
        if cache[row.code] is None:
            missing.append(dict(symbol=row.code, D=row.D, status="no_daily")); continue
        ctx, rawd = cache[row.code]
        hit = ctx[ctx.selection_day == row.S]
        if hit.empty:
            missing.append(dict(symbol=row.code, D=row.D, status="no_daily_S")); continue
        rd = pd.to_datetime(rawd.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
        s_close = rawd.close[rd == row.S]
        daily = hit.iloc[0][["log_turnover20", "daily_close_ma20", "daily_ma20_ma60", "daily_slope5"]].to_dict()
        meta = dict(name=row.name, pick_status=row.status, ref_age=row.ref_age, chg1=row.chg1, src=row.src)
        tasks.append(("picks", row.code, row.S, row.D, nxt.get(row.D), daily,
                      float(s_close.iloc[0]) if len(s_close) else None, meta))
    return tasks, missing


def watch_tasks(k):
    nxt = trading_days(k)
    w = pd.read_parquet(WM/"watchlist.parquet")
    keep = ["log_turnover20", "daily_close_ma20", "daily_ma20_ma60", "daily_slope5"]
    return [("watch", r.symbol, r.selection_day, r.day, nxt.get(r.day), {c: getattr(r, c) for c in keep}, None,
             dict(stage=r.stage, market=r.market)) for r in w.itertuples(index=False)]


# ---------- summary ----------
def stats(x, days, reps=1000, seed=7):
    x = np.asarray(x, float)
    if len(x) == 0:
        return dict(n=0)
    wins, losses = x[x > 0].sum(), -x[x < 0].sum()
    out = dict(n=int(len(x)), mean=float(x.mean()), median=float(np.median(x)),
               accuracy=float((x >= -1e-9).mean()), profit_share=float((x > .001).mean()),
               sum=float(x.sum()), pf=float(wins/losses) if losses > 0 else None)
    g = pd.DataFrame(dict(d=days, x=x)).groupby("d").x.agg(["sum", "count"])
    if len(g) >= 5:
        rng = np.random.default_rng(seed)
        s, n = g["sum"].to_numpy(), g["count"].to_numpy()
        idx = rng.integers(0, len(g), (reps, len(g)))
        boot = s[idx].sum(1)/n[idx].sum(1)
        out["ci95"] = [float(np.quantile(boot, .025)), float(np.quantile(boot, .975))]
    out["days"] = int(len(g))
    return out


def summarize(t):
    out = []
    t = t[t.status.eq("ok")]
    for keys, g in t.groupby(["pop", "period", "variant", "scope"]):
        for gate, gg in (("all", g), ("on", g[g.gate_ok]), ("off", g[~g.gate_ok])):
            out.append(dict(zip(["pop", "period", "variant", "scope"], keys), gate=gate, **stats(gg.net, gg.D)))
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)-1))
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.output)
    if (out/"trades.parquet").exists():
        raise SystemExit(f"{out} is frozen; choose a new output version")
    out.mkdir(parents=True, exist_ok=True)
    m = validate(LIQUID)
    k = kosdaq(out)
    k.to_csv(out/"kosdaq-gate.csv", index=False)
    protocol = dict(PROTOCOL, source_sha256=sha(Path(__file__)), model_sha256=m["model_sha256"],
                    shape_contract=shape_contract(), kosdaq_sha256=sha(out/"kosdaq-daily.raw.txt"),
                    classifier_training_cutoff=CUTOFF)
    atomic_json(out/"protocol.json", protocol)
    pt, missing = pick_tasks(k)
    tasks = pt+watch_tasks(k)
    if args.limit:
        tasks = pt[:args.limit]+watch_tasks(k)[:args.limit]
    print(f"tasks: picks {len(pt)} (+{len(missing)} without daily), total {len(tasks)}", flush=True)
    rows = list(missing)
    with ProcessPoolExecutor(args.workers, initializer=_init) as ex:
        for n, res in enumerate(ex.map(process, tasks, chunksize=8), 1):
            rows.extend(res)
            if n % 1000 == 0:
                print(f"{n}/{len(tasks)}", flush=True)
    t = pd.DataFrame(rows)
    gate = k.set_index("day").gate_ok
    t["gate_ok"] = t.S.map(gate).fillna(False).astype(bool) if "S" in t else False
    t["period"] = np.where(t.D > CUTOFF, "post", "pre")
    t.to_parquet(out/"trades.parquet", index=False)
    s = summarize(t)
    s.to_csv(out/"summary.csv", index=False)
    cov = t.groupby(["pop", "status"]).size().rename("n").reset_index()
    cov.to_csv(out/"coverage.csv", index=False)
    print(cov.to_string())
    print("done", out)


if __name__ == "__main__":
    main()
