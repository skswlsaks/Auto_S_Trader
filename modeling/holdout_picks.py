"""Locked holdout: s_trader91 picks with D in 2026-10-29..2026-11-13, never used for any decision.

Step 1 writes holdout-lock.json (combinations + success rule) BEFORE any data is collected.
Step 2 collects Toss 1m/1d candles into a separate store (existing data untouched).
Step 3 evaluates only the locked combinations with the frozen picks-runner-v1 code.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import picks_runner_study as runner
from .config import atomic_json, load_config
from .data import KST, fetch_series, normalized_candle, open_store
from .pattern_eval import sha
from .toss import TossAPI

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/"outputs/model-research/holdout-picks/20261029-20261113"
START, END = "2026-10-29", "2026-11-13"
LOCK = dict(
    version="holdout-picks-v1",
    created_at=None,
    period=dict(D_from=START, D_to=END, note="No result from this period was seen before this lock"),
    universe="s_trader91 picks (picks_new_features.csv) with D in period and complete D regular 1m bars",
    primary=[
        dict(id="H1", entry="M1o_F", policy="X2_runner_trail",
             meaning="classifier + above open + no risk flag; 35% at +3%, rest trailing 2.5% from peak"),
        dict(id="H2", entry="A_close_F", policy="X3_runner_bear",
             meaning="S close buy + no risk flag; 35% at +3%, rest exits on bearish transfer model"),
    ],
    comparators=[("M1o_F", "X0_base"), ("A_close_F", "X0_base"), ("B_open", "X0_base"), ("A_close", "X0_base"),
                 ("M1o_F", "X4_trail_overnight")],
    success_rule="Primary mean net return > 0 AND above its X0_base comparator. With n this small the result is "
                 "directional only; no threshold/rule change may be made from it.",
    frozen_code=dict(picks_runner=None, entry_gate=None),
)


def lock(out):
    p = out/"holdout-lock.json"
    if p.exists():
        return json.loads(p.read_text())
    out.mkdir(parents=True, exist_ok=True)
    picks = ROOT/"outputs/research/s_trader91_picks_20261114/picks_new_features.csv"
    body = dict(LOCK, created_at=datetime.now(timezone.utc).isoformat(), picks_csv_sha256=sha(picks),
                frozen_code=dict(picks_runner=sha(Path(runner.__file__)), entry_gate=sha(Path(gate.__file__))))
    atomic_json(p, body)
    return body


def holdout_picks():
    b = pd.read_csv(ROOT/"outputs/research/s_trader91_picks_20261114/picks_new_features.csv", dtype={"code": str})
    b["code"] = b.code.str.zfill(6)
    b["D"] = pd.to_datetime(b["D일_게시후첫장"].astype(int).astype(str)).dt.strftime("%Y-%m-%d")
    return b[(b.D >= START) & (b.D <= END)].drop_duplicates(["code", "D"])


def collect(out, symbols):
    c = load_config()
    api = TossAPI(c["credentials_file"], c["api_min_interval_seconds"])
    m_start = datetime.fromisoformat(START).replace(tzinfo=KST)
    end = datetime.fromisoformat(END).replace(tzinfo=KST)+timedelta(days=1)-timedelta(microseconds=1)
    d_start = m_start-timedelta(days=200)
    (out/"minutes").mkdir(exist_ok=True)
    (out/"daily").mkdir(exist_ok=True)
    report = []
    with open_store(out/"raw.sqlite") as db:
        for n, s in enumerate(symbols, 1):
            for interval, adjusted, begin in (("1m", False, m_start), ("1d", True, d_start), ("1d", False, d_start)):
                info = fetch_series(api, db, s, interval, adjusted, begin.isoformat(), end.isoformat(),
                                    c["api_max_pages_per_series"], progress=lambda *_: None)
                rows = [normalized_candle(json.loads(r[0])) for r in db.execute(
                    "SELECT raw FROM candles WHERE symbol=? AND interval=? AND adjusted=? AND timestamp>=? AND timestamp<=? "
                    "ORDER BY timestamp", (s, interval, adjusted, begin.astimezone(timezone.utc).isoformat(),
                                           end.astimezone(timezone.utc).isoformat()))]
                f = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                f["timestamp"] = pd.to_datetime(f.timestamp, utc=True)
                if interval == "1d":
                    f.to_parquet(out/"daily"/f"{s}-1d-{'adjusted' if adjusted else 'raw'}.parquet", index=False)
                else:
                    day = f.timestamp.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
                    for d, g in f.groupby(day):
                        g.to_parquet(out/"minutes"/f"{s}-{d}.parquet", index=False)
                report.append(dict(symbol=s, interval=interval, adjusted=adjusted, rows=len(f), pages=info["pages"],
                                   first=f.timestamp.min().isoformat() if len(f) else None,
                                   last=f.timestamp.max().isoformat() if len(f) else None))
            print(f"collected {n}/{len(symbols)} {s}", flush=True)
    atomic_json(out/"collection.json", dict(source="toss_api", start=START, end=END, series=report))
    return report


def evaluate(out, lk):
    if sha(Path(runner.__file__)) != lk["frozen_code"]["picks_runner"] or sha(Path(gate.__file__)) != lk["frozen_code"]["entry_gate"]:
        raise SystemExit("Frozen evaluation code changed after lock")
    gate.DAYDIRS, gate.DAILYDIRS, gate.CANDLES = [out/"minutes"], [out/"daily"], out/"no-full-series"
    k = gate.kosdaq(out)
    tasks, missing = gate.pick_tasks(k)
    tasks = [t for t in tasks if START <= t[3] <= END]
    missing = [m for m in missing if START <= m["D"] <= END]
    runner._init()
    rows = list(missing)
    for t in tasks:
        rows.extend(runner.process(t))
    t = pd.DataFrame(rows)
    t.to_parquet(out/"trades.parquet", index=False)
    ok = t[t.status.eq("ok")]
    combos = [(p["entry"], p["policy"], p["id"]) for p in lk["primary"]] + [(e, p, "cmp") for e, p in lk["comparators"]]
    res = []
    for e, p, tag in combos:
        g = ok[(ok.entry_variant == e) & (ok.policy == p)]
        st = {k: (None if isinstance(v, float) and not np.isfinite(v) else (float(v) if isinstance(v, (np.floating, float)) else v))
              for k, v in runner.stats(g.net, g.D).items()}
        res.append(dict(tag=tag, entry=e, policy=p, **st,
                        trades=g[["symbol", "name", "D", "net", "reason", "mfe"]].round(4).to_dict("records")))
    cov = t.drop_duplicates(["symbol", "D"]).status.value_counts().to_dict()
    atomic_json(out/"results.json", dict(lock=lk["version"], coverage=cov, results=res))
    for r in res:
        print(r["tag"], r["entry"], r["policy"], "n", r.get("n"), "mean", None if not r.get("n") else round(r["mean"]*100, 2))
    print("coverage", cov)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["lock", "collect", "evaluate"])
    a = ap.parse_args()
    lk = lock(OUT)
    if a.step == "collect":
        collect(OUT, sorted(holdout_picks().code.unique()))
    elif a.step == "evaluate":
        if (OUT/"results.json").exists():
            raise SystemExit("Holdout already evaluated; it cannot be reused")
        evaluate(OUT, lk)


if __name__ == "__main__":
    main()
