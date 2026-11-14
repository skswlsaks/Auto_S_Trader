"""Whole KOSPI/KOSDAQ share universe and resumable daily collection."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path

import pandas as pd

from .config import atomic_json,load_config
from .data import export_series,fetch_series,open_store
from .pattern_eval import sha
from .liquid_trend import PARAMETERS
from .sector_study import OUTPUT as PRIOR,SOURCE
from .shape_experiment import ROOT
from .toss import TossAPI

OUTPUT=ROOT/"outputs/model-research/whole-market/20261030-v1"
SHARE_TYPES={"STOCK","FOREIGN_STOCK","DEPOSITARY_RECEIPT"}
BEGIN="2025-05-01T00:00:00+09:00"
END="2026-10-28T23:59:59+09:00"


def ever_liquid(raw):
    """Skip adjusted downloads only when no D can pass the unchanged liquidity rule."""
    f=raw.sort_values("timestamp")
    days=pd.to_datetime(f.timestamp,utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    turnover=(f.close*f.volume).rolling(20).median()
    return bool((days.between("2025-10-28","2026-10-27") & turnover.ge(PARAMETERS["daily_turnover20_min"])).any())


def share_universe(rows):
    selected={}
    for row in rows:
        if row["market"] not in ("KOSPI","KOSDAQ") or row["securityType"] not in SHARE_TYPES:continue
        symbol=row["symbol"]
        if symbol in selected and selected[symbol]["market"]!=row["market"]:
            raise ValueError("Same symbol on conflicting markets")
        selected[symbol]=row
    return sorted(selected.values(),key=lambda v:v["symbol"])


def discover(output,api):
    output.mkdir(parents=True,exist_ok=True)
    snapshot=output/"universe-snapshot.json"
    if not snapshot.exists():
        rows=[]
        for market in ("KOSPI","KOSDAQ"):
            values=api.get("/api/v1/stocks/all",market=market)
            if not isinstance(values,list):raise ValueError("Market universe must be a full unpaginated list")
            rows.extend(dict(**v,market=market) for v in values)
        atomic_json(snapshot,dict(rows=rows,current_snapshot=True,as_of=pd.Timestamp.now(tz="UTC").isoformat()))
    dead=output/"delisted-snapshot.json"
    if not dead.exists():
        rows=[]
        for market in ("KOSPI","KOSDAQ"):
            rows.extend(dict(**v,market=market,status="DELISTED") for v in api.get("/api/v1/stocks/all",market=market,status="DELISTED"))
        atomic_json(dead,dict(rows=rows))
    rows=json.loads(snapshot.read_text())["rows"]+json.loads(dead.read_text())["rows"]
    values=share_universe(rows)
    result=dict(symbols=values,total=len(values),by_market=dict(Counter(v["market"] for v in values)),
        security_types=dict(Counter(v["securityType"] for v in values)),
        common=int(sum(v.get("isCommonShare") is True for v in values)),
        preferred_or_other=int(sum(v.get("isCommonShare") is not True for v in values)),
        excluded_instrument_types=dict(Counter(v["securityType"] for v in rows if v["securityType"] not in SHARE_TYPES)),
        delisted_returned=len(json.loads(dead.read_text())["rows"]),
        scope="All API-listed KOSPI/KOSDAQ shares, including preferred/foreign shares and DRs. Funds/ETF/ETN/warrants excluded by instrument type; no sector/capitalization/top-N restriction",
        limitations="Current API snapshot; delisted endpoint may not provide historical names. Historical membership, risk flags and listing-state vintages incomplete",
        snapshot_sha256=sha(snapshot),delisted_sha256=sha(dead))
    target=output/"universe.json"
    if target.exists() and json.loads(target.read_text())!=result:raise ValueError("Frozen universe changed")
    if not target.exists():atomic_json(target,result)
    return result


def cached_daily(output,symbol,adjusted):
    name=f"{symbol}-1d-{'adjusted' if adjusted else 'raw'}.parquet"
    path=output/"daily"/name
    if path.exists():return path
    for root in (SOURCE/"candles",PRIOR/"data/candles"):
        source=root/name
        if source.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.symlink_to(source.resolve())
            return path
    return path


def collect_daily(output):
    c=load_config();api=TossAPI(c["credentials_file"],c["api_min_interval_seconds"])
    universe=discover(output,api)
    (output/"daily").mkdir(exist_ok=True)
    (output/"daily-status").mkdir(exist_ok=True)
    with open_store(output/"daily-raw.sqlite"):pass
    def one(stock):
        symbol=stock["symbol"];status_path=output/"daily-status"/f"{symbol}.json"
        if status_path.exists():
            saved=json.loads(status_path.read_text())
            if saved["status"]!="error":
                for path,expected in saved.get("sources",{}).items():
                    if sha(Path(path))!=expected:raise ValueError("Cached daily source changed")
                return saved
        try:
            paths=[]
            for adjusted in (False,True):
                path=cached_daily(output,symbol,adjusted)
                if not path.exists():
                    with open_store(output/"daily-raw.sqlite") as db:
                        fetch_series(api,db,symbol,"1d",adjusted,BEGIN,END,progress=lambda _:None)
                        export_series(db,{"data_dir":str(output/"export")},symbol,"1d",adjusted,BEGIN,END)
                    exported=output/"export/candles"/path.name
                    exported.replace(path)
                paths.append(path)
                if not adjusted:
                    raw=pd.read_parquet(path)
                    if raw.empty or raw.timestamp.max()<pd.Timestamp("2025-10-29T00:00:00+09:00"):
                        result=dict(symbol=symbol,market=stock["market"],status="no_period_history",sources={str(path):sha(path)})
                        atomic_json(status_path,result);return result
                    if not ever_liquid(raw):
                        result=dict(symbol=symbol,market=stock["market"],status="never_liquid",sources={str(path):sha(path)})
                        atomic_json(status_path,result);return result
            frames=[pd.read_parquet(path) for path in paths]
            if not frames[0].timestamp.equals(frames[1].timestamp):raise ValueError("Daily raw/adjusted timestamps differ")
            result=dict(symbol=symbol,market=stock["market"],status="ready",rows=len(frames[0]),
                sources={str(path):sha(path) for path in paths})
        except Exception as error:
            # API wrapper omits response bodies and credentials; keep failures visible.
            result=dict(symbol=symbol,market=stock["market"],status="error",error=f"{type(error).__name__}: {error}")
        atomic_json(status_path,result)
        return result
    results=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(one,v) for v in universe["symbols"]]
        for n,future in enumerate(as_completed(futures),1):
            results.append(future.result())
            if n%50==0 or n==len(futures):
                progress=dict(stage="daily",processed=n,total=len(futures),status_counts=dict(Counter(v["status"] for v in results)))
                atomic_json(output/"progress.json",progress)
                print(json.dumps(progress,ensure_ascii=False),flush=True)
    atomic_json(output/"daily-collection.json",dict(complete=True,total=len(results),
        status_counts=dict(Counter(v["status"] for v in results)),sources={k:v for row in results for k,v in row.get("sources",{}).items()},
        failures=[v for v in results if v["status"]=="error"],universe_sha256=sha(output/"universe.json")))
    return results


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=OUTPUT)
    args=parser.parse_args();collect_daily(args.output)
