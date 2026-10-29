from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import atomic_json, digest
from .toss import TossAPI

KST = timezone(timedelta(hours=9))


def regular_cursor(value):
    local = timestamp(value).astimezone(KST)
    minute = local.hour * 60 + local.minute
    if minute > 15 * 60 + 20:
        local = local.replace(hour=15, minute=20, second=0, microsecond=0)
    elif minute <= 9 * 60 + 1:
        local = (local - timedelta(days=1)).replace(hour=15, minute=20, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat()


def timestamp(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return t.astimezone(timezone.utc)


def normalized_candle(raw):
    if raw.get("currency") != "KRW":
        raise ValueError("Only KRW candles are supported")
    row = {k: float(raw[v]) for k, v in {"open": "openPrice", "high": "highPrice", "low": "lowPrice",
                                        "close": "closePrice", "volume": "volume"}.items()}
    if not all(math.isfinite(v) for v in row.values()) or min(row[k] for k in ("open", "high", "low", "close")) <= 0 or row["volume"] < 0:
        raise ValueError("Invalid candle numeric value")
    if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]:
        raise ValueError("Invalid OHLC order")
    return {"timestamp": timestamp(raw["timestamp"]).isoformat(), **row}


def open_store(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS candles(
        symbol TEXT, interval TEXT, adjusted INTEGER, timestamp TEXT, raw TEXT,
        PRIMARY KEY(symbol,interval,adjusted,timestamp));
      CREATE TABLE IF NOT EXISTS pages(
        id TEXT PRIMARY KEY, series TEXT, requested_before TEXT, fetched_at TEXT, payload TEXT);
      CREATE TABLE IF NOT EXISTS progress(
        series TEXT PRIMARY KEY, cursor TEXT, complete INTEGER, pages INTEGER, oldest TEXT);
    """)
    return db


def fetch_series(api, db, symbol, interval, adjusted, start, end, max_pages=5000, progress=print):
    """Commit raw page, immutable candles and pagination checkpoint together."""
    if not re.fullmatch(r"[0-9A-Z]{6}", symbol) or interval not in ("1m", "1d"):
        raise ValueError("Invalid symbol / interval")
    start, end = timestamp(start), timestamp(end)
    series = digest([symbol, interval, adjusted, start.isoformat(), end.isoformat()])
    saved = db.execute("SELECT cursor,complete,pages,oldest FROM progress WHERE series=?", (series,)).fetchone()
    if saved and saved[1]:
        return {"symbol": symbol, "interval": interval, "complete": True, "pages": saved[2], "oldest": saved[3]}
    cursor = saved[0] if saved else (regular_cursor(end.isoformat()) if interval == "1m" else end.isoformat())
    page_count = saved[2] if saved else 0
    oldest_seen = saved[3] if saved else None
    while page_count < max_pages:
        result = api.get("/api/v1/candles", symbol=symbol, interval=interval, adjusted=str(adjusted).lower(), count=200, before=cursor)
        raw = result.get("candles")
        if not isinstance(raw, list):
            raise ValueError("Invalid candle page")
        rows = [normalized_candle(c) for c in raw]
        times = [timestamp(r["timestamp"]) for r in rows]
        if times != sorted(times, reverse=True) or any(t > timestamp(cursor) for t in times):
            raise ValueError("Candle page order / upper bound violated")
        oldest = min(times).isoformat() if times else oldest_seen
        next_cursor = result.get("nextBefore")
        complete = not rows or min(times) <= start or next_cursor is None
        if next_cursor and timestamp(next_cursor) > timestamp(cursor):
            raise ValueError("Pagination cursor moved into the future")
        # Toss's inclusive cursor overlaps a boundary candle. A boundary-only page
        # needs an explicit microsecond step; never invent or skip a minute candle.
        if not complete and next_cursor and timestamp(next_cursor) == timestamp(cursor):
            if not times or min(times) < timestamp(cursor):
                raise ValueError("Pagination stalled despite older candles")
            next_cursor = (timestamp(cursor) - timedelta(microseconds=1)).isoformat()
        if not complete and interval == "1m" and next_cursor:
            next_cursor = regular_cursor(next_cursor)
        with db:
            for source, row in zip(raw, rows):
                old = db.execute("SELECT raw FROM candles WHERE symbol=? AND interval=? AND adjusted=? AND timestamp=?",
                                 (symbol, interval, adjusted, row["timestamp"])).fetchone()
                if old and normalized_candle(json.loads(old[0])) != row:
                    raise ValueError("Historical candle revision detected; use a new data directory for an auditable refresh")
                db.execute("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?)",
                           (symbol, interval, adjusted, row["timestamp"], json.dumps(source, ensure_ascii=False)))
            fetched = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?)",
                       (digest([series, cursor]), series, cursor, fetched, json.dumps(result, ensure_ascii=False)))
            page_count += 1
            oldest_seen = min(oldest_seen, oldest) if oldest_seen and oldest else oldest
            db.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?,?,?)", (series, next_cursor, complete, page_count, oldest_seen))
        if page_count % 25 == 0 or complete:
            progress(f"{symbol} {interval} pages={page_count} oldest={oldest_seen} complete={complete}")
        if complete:
            return {"symbol": symbol, "interval": interval, "complete": True, "pages": page_count, "oldest": oldest_seen}
        if not next_cursor:
            raise ValueError("Missing pagination cursor")
        cursor = next_cursor
    raise ValueError(f"Page budget exhausted for {symbol} {interval}; increase api_max_pages_per_series and resume")


def discover_universe(api, config):
    root = Path(config["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    target = root / "universe.json"
    if target.exists():
        saved = json.loads(target.read_text())
        if saved["selection"] != {k: config[k] for k in ("sector_name", "sector_id", "symbols")}:
            raise ValueError("Universe selection changed; use a new data_dir")
        return saved
    sectors = api.get("/api/v1/sectors")["sectors"]
    matches = [s for s in sectors if s["sectorId"] == config["sector_id"]] if config["sector_id"] else [s for s in sectors if s["name"] == config["sector_name"]]
    if len(matches) != 1:
        raise ValueError("Sector name must match exactly one API sector; set sector_id from discovery")
    members = api.get(f'/api/v1/sectors/{matches[0]["sectorId"]}/stocks', marketCountry="KR")["stocks"]
    by_symbol = {s["symbol"]: s for s in members}
    symbols = config["symbols"] or sorted(by_symbol)
    if not symbols or any(s not in by_symbol for s in symbols):
        raise ValueError("Empty sector or selected symbols outside the discovered sector")
    result = {"as_of": datetime.now(timezone.utc).isoformat(), "selection": {k: config[k] for k in ("sector_name", "sector_id", "symbols")},
              "sector": matches[0], "symbols": symbols, "members": [by_symbol[s] for s in symbols],
              "universe_scope": "current_sector_snapshot; historical membership and delisted members unavailable from this endpoint",
              "venue": "KRX+NXT integrated; exchange-specific candles unavailable", "sectors": sectors}
    atomic_json(target, result)
    return result


def export_series(db, config, symbol, interval, adjusted, start, end):
    rows = [normalized_candle(json.loads(r[0])) for r in db.execute(
        "SELECT raw FROM candles WHERE symbol=? AND interval=? AND adjusted=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp",
        (symbol, interval, adjusted, timestamp(start).isoformat(), timestamp(end).isoformat()))]
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    target = Path(config["data_dir"]) / "candles" / f"{symbol}-{interval}-{'adjusted' if adjusted else 'raw'}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target, index=False)
    return {"path": str(target), "rows": len(frame), "first": frame.timestamp.min().isoformat(), "last": frame.timestamp.max().isoformat()}


def collect(config, api=None):
    api = api or TossAPI(config["credentials_file"], config["api_min_interval_seconds"])
    universe = discover_universe(api, config)
    start = datetime.fromisoformat(config["start_date"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(config["end_date"]).replace(tzinfo=KST) + timedelta(days=1) - timedelta(microseconds=1)
    if end >= datetime.now(timezone.utc):
        raise ValueError("Collection end_date must be a completed KST day")
    daily_start = start - timedelta(days=420)
    reports = []
    with open_store(Path(config["data_dir"]) / "raw.sqlite") as db:
        for symbol in universe["symbols"]:
            for interval, adjusted, begin in (("1m", False, start), ("1d", True, daily_start), ("1d", False, daily_start)):
                info = fetch_series(api, db, symbol, interval, adjusted, begin.isoformat(), end.isoformat(), config["api_max_pages_per_series"])
                exported = export_series(db, config, symbol, interval, adjusted, begin.isoformat(), end.isoformat())
                reports.append({**info, "adjusted": adjusted, "export": exported})
            atomic_json(Path(config["data_dir"]) / "collection-progress.json", {"series": reports, "finished": False})
    result = {"source": "toss_api", "schema_version": "1.2.28", "start_date": config["start_date"], "end_date": config["end_date"],
              "minute_timestamp": "bar end, [timestamp-1 minute,timestamp)", "minute_scope": "09:01-15:20 KST; outside regular-clock hours skipped by cursor", "venue": universe["venue"],
              "universe_hash": digest(universe), "series": reports, "finished": True,
              "limitations": [universe["universe_scope"], "Minute bars have no exact trading amount; close*volume is only a proxy."]}
    atomic_json(Path(config["data_dir"]) / "collection.json", result)
    return result


def probe(config, api=None):
    api = api or TossAPI(config["credentials_file"], config["api_min_interval_seconds"])
    universe = discover_universe(api, config)
    result = {"universe_symbols": len(universe["symbols"]), "sector": universe["sector"]["name"], "samples": []}
    for symbol in universe["symbols"][:2]:
        for day in (config["start_date"], config["end_date"]):
            page = api.get("/api/v1/candles", symbol=symbol, interval="1m", count=200, adjusted="false", before=day+"T15:20:00+09:00")
            rows = [normalized_candle(c) for c in page["candles"]]
            result["samples"].append({"symbol": symbol, "requested_day": day, "rows": len(rows),
                                       "first": min((r["timestamp"] for r in rows), default=None),
                                       "last": max((r["timestamp"] for r in rows), default=None)})
    atomic_json(Path(config["data_dir"]) / "probe.json", result)
    return result
