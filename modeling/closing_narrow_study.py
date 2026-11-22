"""Plan 2: can our closing scan narrow candidates to s_trader91-like names by S close?

Research only. Uses Naver KRX daily (public) for the scan and daily-bar outcome bounds,
and existing/holdout 1m data (no new account API calls) for an H2-exit spot check.
Scan logic is the cowork port of trading/jongga_scan.mjs evaluate()/nextDayWatch()
(verified 400/400 against JS in outputs/research/s_trader91_picks_20261114).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import entry_gate_study as gate
from . import picks_runner_study as runner
from .config import atomic_json
from .pattern_eval import sha

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
OUT = MR/"closing-narrow/20261114-v1"
PICKS = ROOT/"outputs/research/s_trader91_picks_20261114"
COST = .0025
FEATS = ["ref_age_c", "d5", "d20", "d240", "r5", "r20", "log_tvratio", "hd60", "ma20slope", "nref20",
         "log_tvmed", "chg1", "wick", "body"]
PROTOCOL = dict(
    version="closing-narrow-v1",
    data="Naver KRX daily siseJson 2025-01-01..2026-11-13, universe.json STOCK (no SPAC) + pick codes",
    scan="cowork Python port of jongga_scan evaluate()/nextDayWatch() (build.py), unchanged thresholds",
    timing_caveat="Uses S final daily close; a real 15:00 scan sees a provisional bar",
    liquidity="20-day median turnover (prior 20 days) >= 3e9 KRW",
    C0="status in {entry, point_today, waiting_point, waiting_breakout, above_point} OR new reference today OR nextDayWatch valid",
    R1="C0 minus risk flags: 0.10 < S-day return <= 0.20, S-day is a reference candle (ref_age==0), status invalidated",
    R2="R1 and KOSDAQ gate (S close > MA20 and S return > -1.5%)",
    rankers=dict(S1="logistic 'is s_trader91 pick' on FEATS, trained S in [2026-03-10, 2026-06-30]",
                 S2="tvratio (S turnover / 20d median) descending", S3="r20 descending"),
    top_k=[3, 5],
    outcomes=dict(gap="D open / S close - 1",
                  O1="daily split, no stop: 35% at +3%, 35% at +5% if D high reaches, rest D close, cost .25% (upper bound)",
                  O2="as O1 but if D low <= S*0.97 the whole position exits at min(D open, S*0.97) first (lower bound)",
                  M_X3="1m spot check where minutes exist: S close buy, picks-runner X3 (35% at +3%, rest bearish model)",
                  M_X0="1m spot check, current rule X0"),
    periods=dict(train="S <= 2026-06-30 (S1 fit; rule-only rows still reported)",
                 eval="2026-07-01 <= S <= 2026-10-27", late="S >= 2026-10-28 (holdout already used once for H2)"),
)


# ---------------- data ----------------
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
PAT = re.compile(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)')


def fetch_daily(out, codes):
    d = out/"naver-daily"
    d.mkdir(parents=True, exist_ok=True)

    def one(c):
        p = d/f"{c}.txt"
        if p.exists():
            return True
        url = (f"https://api.finance.naver.com/siseJson.naver?symbol={c}&requestType=1"
               "&startTime=20250101&endTime=20261113&timeframe=day")
        for a in range(3):
            try:
                t = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=15).read().decode("utf-8", "replace")
                tmp = d/f"{c}.tmp"
                tmp.write_text(t)
                os.replace(tmp, p)
                return True
            except Exception:
                time.sleep(.5*(a+1))
        return False

    with cf.ThreadPoolExecutor(8) as ex:
        ok = list(ex.map(one, codes))
    return sum(ok), len(codes)


def load_bars(out):
    bars = {}
    for p in (out/"naver-daily").glob("*.txt"):
        a = [(m[1], float(m[2]), float(m[3]), float(m[4]), float(m[5]), float(m[6])) for m in PAT.finditer(p.read_text())]
        a = [x for x in a if x[1] > 0 and x[4] > 0]
        if len(a) > 30:
            bars[p.stem] = a
    return bars


# ---------------- scan port (from cowork build.py, thresholds unchanged) ----------------
def ref_of(o, h, l, c, pc):
    if pc <= 0:
        return None
    if c > o and c/pc-1 >= .058-1e-9:
        return ("양봉", o, c, h, c, c-(c-o)/3)
    if c < o and h/pc-1 >= .12-1e-9:
        return ("음봉", o, c, h, o, None)
    return None


def features(code, a, start="20260302", end="20261113"):
    n = len(a)
    d = [x[0] for x in a]
    O, Hh, Lw, C, V = (np.array([x[i] for x in a]) for i in range(1, 6))
    cs = np.concatenate([[0], np.cumsum(C)])

    def ma(i, k):
        return (cs[i+1]-cs[i+1-k])/k if i+1 >= k and i >= 0 else np.nan

    refs = [None]+[ref_of(O[i], Hh[i], Lw[i], C[i], C[i-1]) for i in range(1, n)]
    TV = C*V
    out = []
    for t in range(1, n):
        if d[t] < start or d[t] > end:
            continue
        ma20, pma20 = ma(t, 20), ma(t-1, 20)
        status, r, ref = "no_setup", -1, None
        for k in range(t-1, max(1, t-60)-1, -1):
            if refs[k]:
                r, ref = k, refs[k]
                break
        if r >= 0:
            status = None
            for k in range(r+1, t):
                m = ma(k, 20)
                if (ref[5] is not None and C[k] < ref[5]) or (not np.isnan(m) and C[k] < m):
                    status = "invalidated"
                    break
            if status is None:
                p = -1
                for k in range(r+1, t):
                    if C[k] > ref[4]:
                        p = k
                        break
                if ref[5] is not None and C[t] < ref[5]:
                    status = "exit_today"
                elif not np.isnan(ma20) and C[t] < ma20:
                    status = "exit_today"
                elif p < 0:
                    status = "point_today" if C[t] > ref[4] else "waiting_point"
                elif C[t-1] <= C[p] and C[t] > C[p]:
                    status = "entry"
                elif C[t] > C[p]:
                    status = "above_point"
                else:
                    status = "waiting_breakout"
        rr = -1
        for k in range(t, max(1, t-59)-1, -1):
            if refs[k]:
                rr = k
                break
        valid = False
        if rr >= 0:
            rf, ok = refs[rr], True
            for k in range(rr+1, t+1):
                m = ma(k, 20)
                if (rf[5] is not None and C[k] < rf[5]) or (not np.isnan(m) and C[k] < m):
                    ok = False
                    break
            valid = ok and not np.isnan(ma20) and C[t] >= ma20
        h60 = Hh[max(0, t-59):t+1].max()
        tvmed = np.median(TV[max(0, t-20):t]) if t >= 5 else np.nan
        rng = Hh[t]-Lw[t]
        nx = t+1 < n
        out.append(dict(
            code=code, S=d[t], D=d[t+1] if nx else None, close=C[t], status=status, ndw_valid=valid,
            todayref=refs[t] is not None, ref_age=t-rr if rr >= 0 else 99,
            chg1=C[t]/C[t-1]-1, r5=C[t]/C[t-5]-1 if t >= 5 else np.nan, r20=C[t]/C[t-20]-1 if t >= 20 else np.nan,
            d5=C[t]/ma(t, 5)-1, d20=C[t]/ma20-1, d240=C[t]/ma(t, 240)-1,
            ma20slope=ma20/ma(t-5, 20)-1 if t >= 25 else np.nan, hd60=C[t]/h60-1,
            tv=TV[t], tvmed=tvmed, tvratio=TV[t]/tvmed if tvmed and tvmed > 0 else np.nan,
            nref20=sum(1 for k in range(max(1, t-19), t+1) if refs[k]),
            wick=(Hh[t]-max(O[t], C[t]))/rng if rng > 0 else 0, body=(C[t]-O[t])/C[t-1],
            Do=O[t+1] if nx else np.nan, Dh=Hh[t+1] if nx else np.nan, Dl=Lw[t+1] if nx else np.nan,
            Dc=C[t+1] if nx else np.nan))
    return out


# ---------------- outcomes ----------------
def daily_outcomes(f):
    S, o, h, l, c = f.close, f.Do, f.Dh, f.Dl, f.Dc
    part = np.where(h >= S*1.03, .35*.03, 0)+np.where(h >= S*1.05, .35*.05, 0)
    rem = 1-np.where(h >= S*1.03, .35, 0)-np.where(h >= S*1.05, .35, 0)
    o1 = part+rem*(c/S-1)-COST
    stop_px = np.minimum(o, S*.97)
    o2 = np.where(l <= S*.97, stop_px/S-1-COST, o1)
    return o/S-1, o1, o2


def label_picks(f):
    a = pd.read_csv(PICKS/"picks_features.csv", dtype={"code": str})
    b = pd.read_csv(PICKS/"picks_new_features.csv", dtype={"code": str})
    p = pd.concat([a, b])[["code", "S일_직전종가"]].dropna()
    p["code"] = p.code.str.zfill(6)
    p["S"] = p["S일_직전종가"].astype(int).astype(str)
    keys = set(zip(p.code, p.S))
    f["pick"] = [(c, s) in keys for c, s in zip(f.code, f.S)]
    return p.drop_duplicates(["code", "S"])


def stats(x, days):
    x = np.asarray(x, float)
    x, days = x[np.isfinite(x)], np.asarray(days)[np.isfinite(x)]
    if not len(x):
        return dict(n=0)
    w, lo = x[x > .001], x[x < -1e-9]
    out = dict(n=int(len(x)), mean=float(x.mean()), win=float((x > .001).mean()),
               pf=float(w.sum()/-lo.sum()) if len(lo) else None)
    g = pd.DataFrame(dict(d=days, x=x)).groupby("d").x.agg(["sum", "count"])
    if len(g) >= 5:
        idx = np.random.default_rng(7).integers(0, len(g), (1000, len(g)))
        b = g["sum"].to_numpy()[idx].sum(1)/g["count"].to_numpy()[idx].sum(1)
        out["ci"] = [float(np.quantile(b, .025)), float(np.quantile(b, .975))]
    return out


def minute_check(rows, out):
    """H2/X0 on 1m bars where minutes already exist (no new collection)."""
    gate.DAYDIRS = gate.DAYDIRS+[MR/"holdout-picks/20261029-20261113/minutes"]
    runner._init()
    res = []
    for r in rows.sort_values("code").itertuples(index=False):
        D = f"{r.D[:4]}-{r.D[4:6]}-{r.D[6:]}"
        raw = gate.minute_day(r.code, D)
        if raw is None:
            continue
        frame, bear = runner.day_frames(raw, r.code, None, None, entry_model=False)
        if frame is None or bear is None:
            continue
        o, h, l, c = gate.arrays(frame)
        # Naver daily is split-adjusted, Toss 1m is raw: rescale S close when the D-open scale differs (>2%).
        ratio = o[0]/r.Do if r.Do > 0 else np.nan
        entry = r.close*ratio if np.isfinite(ratio) and abs(ratio-1) > .02 else r.close
        seg = [(o, h, l, c, bear, 0)]
        for name in ("X0_base", "X3_runner_bear"):
            net, reason, mfe, _, _ = runner.simulate(entry, entry*.97, seg, runner.POLICIES[name])
            res.append(dict(code=r.code, S=r.S, D=r.D, set=r.set, policy=name, net=net, reason=reason, mfe=mfe,
                            scale_ratio=ratio, rescaled=entry != r.close))
    return pd.DataFrame(res)


def summarize_minutes(m):
    msum = []
    for (name, pol), g in m.groupby(["set", "policy"]):
        per = np.where(g.S <= "20260630", "train", np.where(g.S <= "20261027", "eval", "late"))
        for pp in ("train", "eval", "late"):
            gg = g[per == pp]
            msum.append(dict(set=name, policy=pol, period=pp, **stats(gg.net, gg.S)))
    return msum


def minute_only(out):
    S = pd.read_parquet(out/"selections.parquet")
    focus = S[S.set.isin(["PICKS_F", "R1", "R2", "R1-S1-top3", "R1-S1-top5", "R2-S1-top3", "R1-S2-top3", "R1-S3-top3"])]
    m = minute_check(focus, out)
    m.to_parquet(out/"minute-check-v2.parquet", index=False)
    atomic_json(out/"minute-check-v2-summary.json", dict(
        fix="Entry rescaled by (1m 09:01 open / Naver D open) when |ratio-1|>2% (split-adjusted vs raw)",
        rescaled_rows=int(m.drop_duplicates(["code", "S"]).rescaled.sum()), source_sha256=sha(Path(__file__)),
        results=summarize_minutes(m)))
    print("minute v2 done", len(m))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    ap.add_argument("--minute-only", action="store_true",
                    help="recompute only the 1m spot check (v2: adjusted/raw scale fix) from frozen selections")
    args = ap.parse_args()
    out = Path(args.output)
    if args.minute_only:
        return minute_only(out)
    if (out/"selections.parquet").exists():
        raise SystemExit(f"{out} is frozen; choose a new version")
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    u = json.loads((MR/"whole-market/20261030-v1/universe.json").read_text())["symbols"]
    codes = [s["symbol"] for s in u if s.get("securityType") == "STOCK" and "스팩" not in s.get("name", "")]
    p0 = pd.concat([pd.read_csv(PICKS/f, dtype={"code": str}) for f in ("picks_features.csv", "picks_new_features.csv")])
    codes = sorted(set(codes) | set(p0.code.dropna().str.zfill(6)))
    got, total = fetch_daily(out, codes)
    print(f"naver daily {got}/{total}", flush=True)
    bars = load_bars(out)
    rows = []
    for c, a in bars.items():
        rows += features(c, a)
    f = pd.DataFrame(rows)
    f = f[f.D.notna() & (f.S >= "20260310")]
    picks = label_picks(f)
    k = gate.kosdaq(out).assign(S=lambda x: x.day.str.replace("-", ""))
    f = f.merge(k[["S", "gate_ok"]], on="S", how="left")
    f["gate_ok"] = f.gate_ok.fillna(False).astype(bool)
    f["gap"], f["O1"], f["O2"] = daily_outcomes(f)
    f["liquid"] = f.tvmed >= 3e9
    f["C0"] = f.liquid & (f.status.isin(["entry", "point_today", "waiting_point", "waiting_breakout", "above_point"])
                          | f.todayref | f.ndw_valid)
    f["risk"] = ((f.chg1 > .10) & (f.chg1 <= .20)) | (f.ref_age == 0) | (f.status == "invalidated")
    f["R1"] = f.C0 & ~f.risk
    f["R2"] = f.R1 & f.gate_ok
    f["period"] = np.where(f.S <= "20260630", "train", np.where(f.S <= "20261027", "eval", "late"))
    f["ref_age_c"] = f.ref_age.clip(upper=10)
    f["log_tvratio"] = np.log(f.tvratio.clip(lower=1e-3))
    f["log_tvmed"] = np.log1p(f.tvmed)
    tr = f[(f.period == "train") & f.liquid]
    model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(max_iter=2000, C=.5))
    model.fit(tr[FEATS], tr.pick)
    f["S1"] = model.predict_proba(f[FEATS])[:, 1]
    f["S2"], f["S3"] = f.tvratio, f.r20
    f.to_parquet(out/"features.parquet", index=False)

    # ---- selections ----
    sel = []
    for base in ("C0", "R1", "R2"):
        g = f[f[base]]
        sel.append(g.assign(set=base))
        for s in ("S1", "S2", "S3"):
            for kk in (3, 5):
                top = g.sort_values(s, ascending=False).groupby("S").head(kk)
                sel.append(top.assign(set=f"{base}-{s}-top{kk}"))
    sel.append(f[f.pick].assign(set="PICKS"))
    sel.append(f[f.pick & ~f.risk].assign(set="PICKS_F"))
    sel.append(f[f.liquid].assign(set="LIQUID_ALL"))
    S = pd.concat(sel, ignore_index=True)
    S.to_parquet(out/"selections.parquet", index=False)

    picks_in = f[f.pick]
    summary = []
    for (name, per), g in S.groupby(["set", "period"]):
        denom = picks_in[picks_in.period == per]
        summary.append(dict(set=name, period=per, rows=len(g), per_day=float(g.groupby("S").size().median()),
                            picks_in_set=int(g.pick.sum()), recall=float(g.pick.sum()/max(1, len(denom))),
                            precision=float(g.pick.mean()), gap=stats(g.gap, g.S), O1=stats(g.O1, g.S), O2=stats(g.O2, g.S)))
    atomic_json(out/"summary.json", summary)

    focus = S[S.set.isin(["PICKS_F", "R1", "R2", "R1-S1-top3", "R1-S1-top5", "R2-S1-top3", "R1-S2-top3", "R1-S3-top3"])]
    m = minute_check(focus[focus.S >= "20260310"], out)
    m.to_parquet(out/"minute-check.parquet", index=False)
    msum = []
    for (name, pol), g in m.groupby(["set", "policy"]):
        per = np.where(g.S <= "20260630", "train", np.where(g.S <= "20261027", "eval", "late"))
        for pp in ("train", "eval", "late"):
            gg = g[per == pp]
            msum.append(dict(set=name, policy=pol, period=pp, **stats(gg.net, gg.S)))
    atomic_json(out/"minute-check-summary.json", msum)
    print("done", out, "rows", len(f), "picks labeled", int(f.pick.sum()), "of", len(picks))


if __name__ == "__main__":
    main()
