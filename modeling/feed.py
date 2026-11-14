from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .data import normalized_candle, timestamp
from .features import symbol_features
from .infer import Predictor
from .toss import TossAPI

BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def window(api, symbol, interval, adjusted, start, end):
    cursor, rows = pd.Timestamp(end).isoformat(), {}
    for _ in range(6):
        page = api.get("/api/v1/candles", symbol=symbol, interval=interval, adjusted=str(adjusted).lower(), count=200, before=cursor)
        candles = [normalized_candle(c) for c in page["candles"]]
        for candle in candles:
            at = pd.Timestamp(candle["timestamp"])
            if at > pd.Timestamp(end):
                raise ValueError("Candle page crossed the fixed inference timestamp")
            if pd.Timestamp(start) <= at <= pd.Timestamp(end):
                if candle["timestamp"] in rows and rows[candle["timestamp"]] != candle:
                    raise ValueError("Candle revised within an inference snapshot")
                rows[candle["timestamp"]] = candle
        next_before = page.get("nextBefore")
        if not candles or not next_before or min(pd.Timestamp(c["timestamp"]) for c in candles) <= pd.Timestamp(start):
            break
        if timestamp(next_before) > timestamp(cursor):
            raise ValueError("Inference pagination moved forward")
        if timestamp(next_before) == timestamp(cursor):
            next_before = (pd.Timestamp(cursor) - pd.Timedelta(microseconds=1)).isoformat()
        cursor = next_before
    else:
        raise ValueError("Inference window exceeded bounded pagination")
    frame = pd.DataFrame(rows.values(), columns=BAR_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
    for name in BAR_COLUMNS[1:]:
        frame[name] = frame[name].astype(float)
    return frame.sort_values("timestamp").reset_index(drop=True)


def as_records(frame):
    f = frame[BAR_COLUMNS].copy()
    f["timestamp"] = f.timestamp.map(lambda t: t.isoformat())
    return f.to_dict("records")


class TossInference:
    """Frozen local model fed by provider-completed candles; never sends orders."""

    def __init__(self, run, api=None):
        self.predictor = Predictor(run)
        self.config = self.predictor.config
        manifest = json.loads((Path(run) / "dataset.json").read_text())
        self.symbols = sorted({row["symbol"] for row in manifest["coverage"]})
        self.api = api or TossAPI(self.config["credentials_file"], self.config["api_min_interval_seconds"])
        self.day = None
        self.daily_cache = {}
        self.calendar = None

    def _daily(self, symbol, adjusted, start, historical):
        key = (symbol, adjusted)
        if key in self.daily_cache:
            return self.daily_cache[key]
        suffix = "adjusted" if adjusted else "raw"
        path = Path(self.config["data_dir"]) / "candles" / f"{symbol}-1d-{suffix}.parquet"
        existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=BAR_COLUMNS)
        existing["timestamp"] = pd.to_datetime(existing.timestamp, utc=True)
        existing = existing[existing.timestamp < start]
        if not historical:
            target = self.calendar["previousBusinessDay"]["date"]
            latest = existing.timestamp.max().tz_convert("Asia/Seoul").strftime("%Y-%m-%d") if len(existing) else ""
            if latest < target:
                fresh = window(self.api, symbol, "1d", adjusted, start-pd.Timedelta(days=420), start-pd.Timedelta(microseconds=1))
                existing = pd.concat([existing, fresh]).drop_duplicates("timestamp", keep="last").sort_values("timestamp")
        self.daily_cache[key] = existing.tail(300).reset_index(drop=True)
        return self.daily_cache[key]

    def snapshot(self, as_of=None):
        started = time.perf_counter()
        historical = as_of is not None
        now = pd.Timestamp.now(tz="UTC")
        at = pd.Timestamp(as_of) if historical else (now - pd.Timedelta(seconds=3)).floor("min")
        if at.tzinfo is None or at > now:
            raise ValueError("as_of must include timezone and cannot be in the future")
        at = at.tz_convert("UTC").floor("min")
        local = at.tz_convert("Asia/Seoul")
        day, minute = local.strftime("%Y-%m-%d"), local.hour*60 + local.minute
        base = {"mode": "historical_toss_inference" if historical else "live_toss_inference_no_orders",
                "as_of": at.isoformat(), "universe_symbols": len(self.symbols), "orders_enabled": False}
        if historical and day in self.predictor.bundle["holdout_days"] and not (self.predictor.run / "evaluation.json").exists():
            raise ValueError("Historical inference cannot open the locked final evaluation period")
        if local.weekday() >= 5 or not 541 <= minute <= 920:
            return {**base, "status": "outside_regular_clock", "predictions": []}
        if day != self.day:
            self.day, self.daily_cache, self.calendar = day, {}, None
        if not historical:
            self.calendar = self.calendar or self.api.get("/api/v1/market-calendar/KR")
            today = self.calendar.get("today", {})
            interval = (today.get("integrated") or {}).get("regularMarket")
            if today.get("date") != day or not interval or not pd.Timestamp(interval["startTime"]) <= at <= pd.Timestamp(interval.get("singlePriceAuctionStartTime", interval["endTime"])):
                return {**base, "status": "calendar_closed", "predictions": []}
        start = pd.Timestamp(day + "T09:01:00", tz="Asia/Seoul").tz_convert("UTC")

        def fetch_symbol(symbol):
            try:
                bars = window(self.api, symbol, "1m", False, start, at)
                adjusted, raw = self._daily(symbol, True, start, historical), self._daily(symbol, False, start, historical)
                if bars.empty or bars.timestamp.iloc[-1] != at or adjusted.empty or raw.empty:
                    return symbol, None
                f = symbol_features(bars, adjusted, raw, symbol, self.config)
                if f.empty:
                    return symbol, None
                return symbol, (bars, adjusted, raw, f.iloc[-1])
            except (ValueError, RuntimeError, KeyError):
                return symbol, None

        with ThreadPoolExecutor(max_workers=self.config.get("api_workers", 3)) as pool:
            snapshots = dict(pool.map(fetch_symbol, self.symbols))
        usable = [value for value in snapshots.values() if value is not None]
        valid_returns = [value[3].ret_15 for value in usable if np.isfinite(value[3].ret_15)]
        daily = [value[3].daily_momentum20 for value in usable if np.isfinite(value[3].daily_momentum20)]
        if len(usable) < max(1, int(np.ceil(.8*len(self.symbols)))) or not valid_returns or not daily:
            return {**base, "status": "insufficient_sector_context", "fresh_symbols": len(usable), "predictions": []}
        sector = {"as_of": at.isoformat(), "sector_ret15": float(np.mean(valid_returns)),
                  "sector_breadth": float(np.mean(np.array(valid_returns) > 0)), "sector_daily_momentum20": float(np.mean(daily))}
        predictions = []
        for symbol, value in snapshots.items():
            if value is None:
                predictions.append({"symbol": symbol, "status": "missing_or_stale_data", "decision": "hold"})
                continue
            bars, adjusted, raw, _ = value
            predictions.append(self.predictor.predict({"symbol": symbol, "as_of": at.isoformat(), "sector": sector,
                                                       "bars": as_records(bars), "daily_adjusted": as_records(adjusted), "daily_raw": as_records(raw)}))
        return {**base, "status": "scored", "fresh_symbols": len(usable), "sector": sector, "predictions": predictions,
                "processing_seconds": time.perf_counter() - started,
                "note": "Frozen research policy; data collection, normalization and local inference only"}


def watch(run, output, once=False, as_of=None):
    if as_of and not once:
        raise ValueError("Historical --as-of requires --once")
    feed = TossInference(run)
    while True:
        started = time.monotonic()
        output.write(json.dumps(feed.snapshot(as_of), ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()
        if once:
            return
        time.sleep(max(1., 60. - (time.monotonic() - started)))
