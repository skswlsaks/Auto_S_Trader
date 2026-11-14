"""Local sector expansion and selected-day minute collection for daytrade research."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from .config import atomic_json, load_config
from .data import export_series, fetch_series, normalized_candle, open_store
from .liquid_trend import daily_context
from .nextday_eval import daily_frame, prior_sessions, replay_daily
from .pattern_eval import sha
from .shape_experiment import ROOT
from .toss import TossAPI

SOURCE = ROOT/"outputs/model-research/data"
FOCUS = ROOT/"outputs/model-research/liquid-trend/20261030-v1/watchlist.parquet"
OUTPUT = ROOT/"outputs/model-research/daytrade/20261030-v1"
START, END = "2025-10-29", "2026-10-28"


def daily_path(output, symbol, adjusted):
    name = f"{symbol}-1d-{'adjusted' if adjusted else 'raw'}.parquet"
    old = SOURCE/"candles"/name
    return old if old.exists() else output/"data/candles"/name


def scan(output):
    output.mkdir(parents=True,exist_ok=True)
    c = load_config()
    api = TossAPI(c["credentials_file"],c["api_min_interval_seconds"])
    snapshot = output/"sectors-snapshot.json"
    if not snapshot.exists():
        sectors = api.get("/api/v1/sectors")["sectors"]
        groups = []
        for sector in sectors:
            if sector["depth"] != 0:
                continue
            members = api.get(f'/api/v1/sectors/{sector["sectorId"]}/stocks',marketCountry="KR")["stocks"]
            leaders = sorted(members,key=lambda x:float(x.get("marketCapitalization",0)),reverse=True)[:5]
            groups.append(dict(**sector,member_count=len(members),leaders=leaders))
        atomic_json(snapshot,dict(as_of=pd.Timestamp.now(tz="UTC").isoformat(),sectors=groups,
            limitation="Current membership and current top-5 market-cap leaders: retrospective proxy, NOT historical sector index or point-in-time investable universe"))
    groups = json.loads(snapshot.read_text())["sectors"]
    symbols = sorted({v["symbol"] for g in groups for v in g["leaders"]})
    (output/"data").mkdir(exist_ok=True)
    with open_store(output/"data/raw.sqlite"):
        pass
    def one(symbol):
        for adjusted in (False,True):
            path = daily_path(output,symbol,adjusted)
            if path.exists():
                continue
            begin,end = "2024-09-04T00:00:00+09:00",END+"T23:59:59+09:00"
            with open_store(output/"data/raw.sqlite") as db:
                fetch_series(api,db,symbol,"1d",adjusted,begin,end,progress=lambda _:None)
                export_series(db,{"data_dir":str(output/"data")},symbol,"1d",adjusted,begin,end)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(one,s) for s in symbols]
        for n,future in enumerate(as_completed(futures),1):
            future.result()
            if n%30==0:print(f"Daily sector leaders {n}/{len(symbols)}",flush=True)
    results,hashes = [],{}
    for g in groups:
        paths = [(v,daily_path(output,v["symbol"],True),daily_path(output,v["symbol"],False)) for v in g["leaders"]]
        returns,turnovers,leaders = [],[],[]
        for v,adj,raw in paths:
            hashes.update({str(adj):sha(adj),str(raw):sha(raw)})
            a,r = daily_frame(adj),daily_frame(raw)
            begin = a.loc[a.date.le("2025-10-28")]
            end = a.loc[a.date.le(END)]
            if begin.empty or end.empty or begin.date.iloc[-1]!="2025-10-28" or end.date.iloc[-1]!=END:
                continue
            ret = float(end.close.iloc[-1]/begin.close.iloc[-1]-1)
            turnover = float((r.loc[r.date.between(START,END),"close"]*r.loc[r.date.between(START,END),"volume"]).median())
            returns.append(ret);turnovers.append(turnover)
            leaders.append(dict(symbol=v["symbol"],name=v["name"],return_1y=ret,median_turnover=turnover))
        results.append(dict(sector_id=g["sectorId"],sector=g["name"],members=g["member_count"],coverage=len(returns),
            return_1y=float(np.mean(returns)) if returns else None,median_member_return=float(np.median(returns)) if returns else None,
            basket_median_turnover=float(np.median(turnovers)) if turnovers else None,leaders=leaders))
    ranked = sorted([r for r in results if r["coverage"]>=3 and r["basket_median_turnover"]>=10_000_000_000],
                    key=lambda r:r["return_1y"],reverse=True)
    added = [r for r in ranked if r["sector"]!="반도체" and r["return_1y"]>0][:4]
    atomic_json(output/"sector-ranking.json",dict(start=START,end=END,ranking=ranked,all_sectors=results,expanded_sectors=added,
        proxy="Equal initial weight, current top-5 capitalization leaders with full-year prices; adjusted price return excluding dividends",
        eligibility="At least 3 full-year leaders, median leader daily turnover >=10bn KRW; 4 strongest non-semiconductor baskets",
        limitations=["Ex-post sector and current-cap leader selection; exploratory only, no independent performance claim",
                     "Current members, no delisted names or historical classifications; not a sector-index ranking"],source_hashes=hashes))
    print(json.dumps(dict(sectors=len(groups),leaders=len(symbols),expanded=[(r["sector"],r["return_1y"]) for r in added]),ensure_ascii=False),flush=True)


def watchlist(output):
    if (output/"watchlist.parquet").exists():
        return pd.read_parquet(output/"watchlist.parquet")
    rank = json.loads((output/"sector-ranking.json").read_text())
    old = pd.read_parquet(FOCUS)
    old = old.loc[old.focus_selected].copy()
    old["sector"] = "반도체"
    symbols = {}
    for group in rank["expanded_sectors"]:
        for v in group["leaders"]:
            if v["symbol"] not in set(old.symbol):
                symbols.setdefault(v["symbol"],group["sector"])
    inputs,contexts,prices = [],[],{}
    for symbol in symbols:
        a,r = [daily_frame(daily_path(output,symbol,adj)) for adj in (True,False)]
        inputs.append(dict(symbol=symbol,bars=a[["date","open","high","low","close","volume"]].to_dict("records")))
        contexts.append(daily_context(a,r).assign(symbol=symbol))
        prices[symbol] = r[["date","close"]].merge(a[["date","close"]],on="date",suffixes=("_raw","_adj"),validate="one_to_one").set_index("date")
    days = json.loads((SOURCE/"dataset.json").read_text())["days"]
    previous = prior_sessions(days,[b["date"] for v in inputs for b in v["bars"]])
    next_day = {v:k for k,v in previous.items()}
    records = []
    for row in replay_daily(inputs,sorted(next_day))["rows"]:
        selection = row.pop("asOf")
        ref = row.pop("reference",None)
        row.pop("point",None);row.pop("broken",None)
        d = prices[row["symbol"]].loc[selection]
        factor = d.close_raw/d.close_adj
        records.append(dict(**row,selection_day=selection,day=next_day[selection],sector=symbols[row["symbol"]],
            known_at=selection+"T23:59:59+09:00",secret_price=ref["secret"]*factor if ref and ref["secret"] is not None else None,
            ma20_price=row.get("ma20",np.nan)*factor))
    new = pd.DataFrame(records).merge(pd.concat(contexts,ignore_index=True),on=["symbol","selection_day"],validate="many_to_one")
    new["focus_selected"] = new.selected & new.daily_focus.fillna(False)
    selected = pd.concat([old,new.loc[new.focus_selected]],ignore_index=True).sort_values(["day","symbol"]).reset_index(drop=True)
    if selected.duplicated(["symbol","day"]).any() or not selected.selection_day.lt(selected.day).all():
        raise ValueError("Invalid D-to-next-session watchlist")
    selected.to_parquet(output/"watchlist.parquet",index=False)
    atomic_json(output/"watchlist-summary.json",dict(selected_stockdays=len(selected),symbols=int(selected.symbol.nunique()),
        by_sector=selected.groupby("sector").agg(stockdays=("day","size"),symbols=("symbol","nunique")).to_dict("index"),
        source_watchlist_sha256=sha(FOCUS),ranking_sha256=sha(output/"sector-ranking.json")))
    return selected


def collect(output):
    watch = watchlist(output)
    c = load_config()
    api = TossAPI(c["credentials_file"],c["api_min_interval_seconds"])
    day_root = output/"minutes"
    day_root.mkdir(exist_ok=True)
    jobs = [(s,d) for s,d in watch[["symbol","day"]].itertuples(index=False,name=None)]
    # Existing regular data is immutable. Missing premarket is fetched separately.
    existing = {}
    for symbol in sorted(watch.symbol.unique()):
        path = SOURCE/"candles"/f"{symbol}-1m-raw.parquet"
        if path.exists():
            f = pd.read_parquet(path)
            dates = f.timestamp.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
            wanted = set(watch.loc[watch.symbol.eq(symbol),"day"])
            existing[symbol] = {d:g for d,g in f.groupby(dates) if d in wanted}
    def one(symbol,day):
        path = day_root/f"{symbol}-{day}.parquet"
        if path.exists():return
        records = []
        before = day+"T15:20:00+09:00"
        if symbol not in existing or day not in existing[symbol]:
            for _ in range(3):
                result = api.get("/api/v1/candles",symbol=symbol,interval="1m",adjusted="false",count=200,before=before)
                page = [normalized_candle(v) for v in result["candles"]]
                records.extend(page)
                if not page or pd.Timestamp(page[-1]["timestamp"])<=pd.Timestamp(day+"T09:01:00+09:00"):
                    break
                before = (pd.Timestamp(page[-1]["timestamp"])-pd.Timedelta(microseconds=1)).isoformat()
            else:raise ValueError("Regular day pagination budget exhausted")
        result = api.get("/api/v1/candles",symbol=symbol,interval="1m",adjusted="false",count=200,before=day+"T08:50:00+09:00")
        records.extend(normalized_candle(v) for v in result["candles"])
        f = pd.DataFrame(records)
        f.timestamp = pd.to_datetime(f.timestamp,utc=True)
        local = f.timestamp.dt.tz_convert("Asia/Seoul")
        minute = local.dt.hour*60+local.dt.minute
        f = f.loc[local.dt.strftime("%Y-%m-%d").eq(day) & (minute.between(481,530)|minute.between(541,920))]
        if symbol in existing and day in existing[symbol]:
            old = existing[symbol][day]
            clock = old.timestamp.dt.tz_convert("Asia/Seoul")
            old = old.loc[(clock.dt.hour*60+clock.dt.minute).between(541,920)]
            f = pd.concat([old,f],ignore_index=True)
        for _,group in f.loc[f.timestamp.duplicated(False)].groupby("timestamp"):
            if len(group[["open","high","low","close","volume"]].drop_duplicates())!=1:
                raise ValueError("Revised overlapping candle; do not overwrite frozen source")
        f = f.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        f.to_parquet(path.with_suffix(".tmp"),index=False)
        path.with_suffix(".tmp").replace(path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(one,*v) for v in jobs]
        for n,future in enumerate(as_completed(futures),1):
            future.result()
            if n%100==0:
                atomic_json(output/"collection-progress.json",dict(finished=n,total=len(jobs)))
                print(f"Selected-day minute collection {n}/{len(jobs)}",flush=True)
    atomic_json(output/"collection-progress.json",dict(finished=len(jobs),total=len(jobs),complete=True))


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["scan","collect","all"])
    parser.add_argument("--output",type=Path,default=OUTPUT)
    args = parser.parse_args()
    if args.command in ("scan","all"):scan(args.output)
    if args.command in ("collect","all"):collect(args.output)
