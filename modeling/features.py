from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import VERSION
from .config import atomic_json, digest
from .execution import outcome

FEATURES = ["ret_1", "ret_5", "ret_15", "ret_60", "range_atr", "body_atr", "close_location", "atr_pct",
            "volume_ratio", "vwap_distance_atr", "ema5_distance_atr", "ema20_distance_atr", "compression",
            "high_slope", "low_slope", "higher_low_atr", "resistance_distance_atr", "support_distance_atr",
            "setup_age", "pullback_depth_atr", "recovery_atr", "pullback_volume_ratio", "rebound_volume_ratio",
            "minutes_since_open", "daily_momentum20", "daily_ma20_distance", "daily_ma60_distance", "daily_ma240_distance",
            "opening_gap", "signal_risk_pct", "estimated_cost_to_risk", "sector_ret15", "sector_breadth", "relative_strength15", "sector_daily_momentum20"]


def contract_hashes():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("features.py", "execution.py")}


def regular_bars(frame):
    f = frame.copy().sort_values("timestamp").reset_index(drop=True)
    f["timestamp"] = pd.to_datetime(f.timestamp, utc=True)
    if f.timestamp.duplicated().any():
        raise ValueError("Duplicate minute timestamps")
    local = f.timestamp.dt.tz_convert("Asia/Seoul")
    minute = local.dt.hour * 60 + local.dt.minute
    f = f.loc[(minute >= 541) & (minute <= 920)].copy()
    f["day"] = f.timestamp.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    f["minute"] = f.timestamp.dt.tz_convert("Asia/Seoul").dt.hour * 60 + f.timestamp.dt.tz_convert("Asia/Seoul").dt.minute
    f["segment"] = ((f.timestamp.diff() != pd.Timedelta(minutes=1)) | f.day.ne(f.day.shift())).cumsum()
    return f.reset_index(drop=True)


def daily_features(adjusted, raw):
    a = adjusted.copy().sort_values("timestamp")
    a["day"] = pd.to_datetime(a.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    prior = pd.DataFrame({"known_from": pd.to_datetime(a.day) + pd.Timedelta(days=1), "daily_momentum20": a.close.pct_change(20)})
    for n in (20, 60, 240):
        prior[f"daily_ma{n}_distance"] = a.close / a.close.rolling(n, min_periods=n).mean() - 1
    r = raw.copy().sort_values("timestamp")
    r["day"] = pd.to_datetime(r.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    r["known_from"] = pd.to_datetime(r.day) + pd.Timedelta(days=1)
    r["previous_close"] = r.close
    return prior.merge(r[["known_from", "previous_close"]], on="known_from", validate="one_to_one").sort_values("known_from")


def structural_features(bars, symbol, config):
    """Every value and state transition uses only completed bars up to that row."""
    p = config["pattern"]
    parts = []
    for _, frame in bars.groupby("segment", sort=False):
        f = frame.copy().reset_index(drop=True)
        previous = f.close.shift()
        tr = pd.concat([f.high - f.low, (f.high - previous).abs(), (f.low - previous).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=14).mean().replace(0, np.nan)
        for n in (1, 5, 15, 60):
            f[f"ret_{n}"] = f.close.pct_change(n, fill_method=None)
        f["atr_pct"] = atr / f.close
        f["range_atr"], f["body_atr"] = (f.high - f.low) / atr, (f.close - f.open) / atr
        f["close_location"] = (f.close - f.low) / (f.high - f.low).replace(0, np.nan)
        f["volume_ratio"] = f.volume / f.volume.shift(1).rolling(20, min_periods=10).mean().replace(0, np.nan)
        f["vwap_distance_atr"] = (f.close - (f.close * f.volume).cumsum() / f.volume.cumsum().replace(0, np.nan)) / atr
        for n in (5, 20):
            f[f"ema{n}_distance_atr"] = (f.close - f.close.ewm(span=n, adjust=False).mean()) / atr
        f["compression"] = (f.high.rolling(10).max() - f.low.rolling(10).min()) / (f.high.rolling(30).max() - f.low.rolling(30).min()).replace(0, np.nan)
        f["minutes_since_open"] = f.minute - 540
        columns = ("high_slope", "low_slope", "higher_low_atr", "resistance_distance_atr", "support_distance_atr",
                   "setup_age", "pullback_depth_atr", "recovery_atr", "pullback_volume_ratio", "rebound_volume_ratio")
        extra = {name: np.full(len(f), np.nan) for name in columns}
        candidates, stops, pattern_ids = np.zeros(len(f), dtype=bool), np.full(len(f), np.nan), np.full(len(f), "", dtype=object)
        highs, lows, setup, cooldown = [], [], None, -1
        hi, lo, close, volume, vol_unit = [f[x].to_numpy() for x in ("high", "low", "close", "volume")] + [atr.to_numpy()]
        right, window = p["pivot_right_bars"], p["lookback_bars"]
        for i in range(len(f)):
            if i >= 2 * right:
                j = i - right
                if hi[j] >= max(hi[j-right:j]) and hi[j] > max(hi[j+1:i+1]):
                    highs.append((j, hi[j]))
                if lo[j] <= min(lo[j-right:j]) and lo[j] < min(lo[j+1:i+1]):
                    lows.append((j, lo[j]))
            highs = [(j, v) for j, v in highs if i - j <= window]
            lows = [(j, v) for j, v in lows if i - j <= window]
            a = vol_unit[i]
            if i < 20 or not np.isfinite(a) or a <= 0:
                continue
            resistance, hs = float(max(hi[max(0, i-20):i])), 0.
            support, ls, higher = float(min(lo[max(0, i-20):i])), 0., 0.
            if len(highs) >= 2:
                (j1, v1), (j2, v2) = highs[-2:]
                hs = (v2 - v1) / (j2 - j1)
                if hs <= 0:
                    resistance = v2 + hs * (i - j2)
            if len(lows) >= 2:
                (j1, v1), (j2, v2) = lows[-2:]
                ls, higher = (v2 - v1) / (j2 - j1), (v2 - v1) / a
                support = v2 + ls * (i - j2)
            for name, value in zip(columns[:5], (hs/a, ls/a, higher, (close[i]-resistance)/a, (close[i]-support)/a)):
                extra[name][i] = value
            if setup and (i - setup["start"] > p["setup_max_bars"] or close[i] < setup["line"] - p["invalidation_atr"] * a):
                setup = None
            if setup is None:
                if i > cooldown and close[i] > resistance + p["breakout_atr"] * a and close[i-1] <= resistance + p["breakout_atr"] * a:
                    setup = {"start": i, "line": resistance, "peak": hi[i], "low": lo[i], "stage": "breakout", "break_volume": max(volume[i], 1.), "pull_volumes": []}
                continue
            setup["peak"] = max(setup["peak"], hi[i])
            if setup["stage"] == "breakout" and close[i] < close[i-1] and lo[i] <= setup["line"] + p["pullback_atr"] * a:
                setup["stage"], setup["low"] = "pullback", lo[i]
            if setup["stage"] == "pullback":
                setup["low"] = min(setup["low"], lo[i])
                prior_volume = np.mean(setup["pull_volumes"]) if setup["pull_volumes"] else max(volume[i], 1.)
                for name, value in zip(columns[5:], (i-setup["start"], (setup["peak"]-setup["low"])/a, (close[i]-setup["low"])/a, prior_volume/setup["break_volume"], volume[i]/max(prior_volume, 1.))):
                    extra[name][i] = value
                if close[i] > hi[i-1] and close[i] > setup["line"] and close[i] - setup["low"] >= p["rebound_atr"] * a and volume[i] > 0:
                    stop = setup["low"] - .1 * a
                    if 0 < stop < close[i]:
                        candidates[i], stops[i], pattern_ids[i] = True, stop, digest([symbol, f.timestamp.iloc[setup["start"]].isoformat()])[:20]
                    setup, cooldown = None, i + p["cooldown_bars"]
                elif setup:
                    setup["pull_volumes"].append(volume[i])
        for name in columns:
            f[name] = extra[name]
        f["candidate"], f["stop_price"], f["pattern_id"] = candidates, stops, pattern_ids
        parts.append(f)
    result = pd.concat(parts, ignore_index=True) if parts else bars.copy()
    result["symbol"] = symbol
    return result


def symbol_features(minutes, adjusted, raw, symbol, config):
    bars = regular_bars(minutes)
    f = structural_features(bars, symbol, config)
    if f.empty:
        return f
    f["known_from"] = pd.to_datetime(f.day)
    f = pd.merge_asof(f.sort_values("known_from"), daily_features(adjusted, raw), on="known_from", direction="backward").sort_values("timestamp").reset_index(drop=True)
    first_open = f.groupby("day").open.transform("first")
    f["opening_gap"] = first_open / f.previous_close - 1
    f["signal_risk_pct"] = (f.close - f.stop_price) / f.close
    e = config["execution"]
    tax = pd.Series(float(e["sell_tax_bps"]), index=f.index)
    for row in e.get("sell_tax_schedule", []):
        tax = tax.mask(f.day >= row["effective_from"], row["bps"])
    roundtrip_cost = (e["buy_fee_bps"] + e["sell_fee_bps"] + tax + 2 * (e["slippage_bps"] + e["half_spread_bps"])) / 10000
    f["estimated_cost_to_risk"] = roundtrip_cost / f.signal_risk_pct.where(f.signal_risk_pct > 0)
    return f.replace([np.inf, -np.inf], np.nan)


def build(config, allow_partial=False):
    implementation = contract_hashes()
    root = Path(config["data_dir"])
    collection_path = root / "collection.json"
    if not allow_partial and (not collection_path.exists() or not json.loads(collection_path.read_text())["finished"]):
        raise ValueError("Complete collection first, or explicitly use --allow-partial for a non-final diagnostic dataset")
    universe = json.loads((root / "universe.json").read_text())
    frames, source_files, coverage, accum = [], [], [], None
    for symbol in universe["symbols"]:
        files = [root/"candles"/f"{symbol}-{suffix}.parquet" for suffix in ("1m-raw", "1d-adjusted", "1d-raw")]
        if not all(p.exists() for p in files):
            if allow_partial:
                continue
            raise ValueError(f"Missing candle files for {symbol}")
        minutes, adjusted, raw = [pd.read_parquet(p) for p in files]
        for path in files:
            source_files.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        f = symbol_features(minutes, adjusted, raw, symbol, config)
        if f.empty:
            coverage.append({"symbol": symbol, "minute_rows": 0, "days": 0, "candidates": 0,
                             "exclusion_reason": "no_regular_clock_history_in_requested_period"})
            continue
        contribution = f.set_index("timestamp")[["ret_15", "daily_momentum20"]].copy()
        contribution["breadth_sum"] = (contribution.ret_15 > 0).astype(float).where(contribution.ret_15.notna())
        contribution["intraday_count"] = contribution.ret_15.notna().astype(int)
        contribution["daily_count"] = contribution.daily_momentum20.notna().astype(int)
        contribution = contribution.fillna(0)
        accum = contribution if accum is None else accum.add(contribution, fill_value=0)
        candidates = f.loc[f.candidate].copy()
        labeled = []
        label_bars = f[["timestamp", "open", "high", "low", "close", "volume"]]
        for row in candidates.to_dict("records"):
            label = outcome(label_bars, row["timestamp"], row["stop_price"], config)
            labeled.append({**row, **label})
        if labeled:
            frames.append(pd.DataFrame(labeled))
        byday = f.groupby("day").agg(bars=("timestamp", "size"), zero_volume=("volume", lambda x: int((x == 0).sum())), segments=("segment", "nunique"))
        coverage.append({"symbol": symbol, "minute_rows": len(f), "days": len(byday), "first_day": f.day.min(), "last_day": f.day.max(),
                         "days_with_gaps": int((byday.segments > 1).sum()), "short_days_under_300_bars": int((byday.bars < 300).sum()),
                         "candidates": len(candidates)})
        print(f"features {symbol} bars={len(f)} candidates={len(candidates)}", flush=True)
    if not frames:
        raise ValueError("No pattern candidates; inspect raw data and pattern rules instead of creating artificial labels")
    sector = pd.DataFrame({"timestamp": accum.index,
                           "sector_ret15": accum.ret_15 / accum.intraday_count.replace(0, np.nan),
                           "sector_breadth": accum.breadth_sum / accum.intraday_count.replace(0, np.nan),
                           "sector_daily_momentum20": accum.daily_momentum20 / accum.daily_count.replace(0, np.nan)}).reset_index(drop=True)
    data = pd.concat(frames, ignore_index=True).merge(sector, on="timestamp", how="left", validate="many_to_one")
    data["relative_strength15"] = data.ret_15 - data.sector_ret15
    data = data.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    for name in FEATURES:
        data[name] = data[name].astype("float32")
    data["regime"] = np.select([data.sector_daily_momentum20 > 0, data.sector_daily_momentum20 < 0], ["up", "down"], default="neutral")
    columns = ["timestamp", "symbol", "day", "pattern_id", "stop_price", *FEATURES, "regime", "filled", "label_status", "ambiguous",
               "entry_at", "exit_at", "valuation_at", "remaining_quantity", "entry_price", "quantity", "capital", "net_pnl", "net_R", "net_return", "exit_reason",
               "path_times", "path_pnl", "path_remaining", "path_ambiguous"]
    for col in columns:
        if col not in data:
            data[col] = None
    for col in ("path_times", "path_pnl", "path_remaining", "path_ambiguous"):
        data[col] = data[col].map(lambda value: list(value) if isinstance(value, (list, np.ndarray)) else [])
    data = data[columns]
    if contract_hashes() != implementation:
        raise ValueError("Feature/label source changed during dataset build; rerun with a stable implementation")
    output = root / "candidates.parquet"
    data.to_parquet(output, index=False)
    manifest = {"version": VERSION, "contract_hashes": implementation, "source": "toss_api", "partial": bool(allow_partial), "universe": universe["universe_scope"],
                "source_files": source_files, "feature_names": FEATURES, "pattern": config["pattern"], "execution": config["execution"],
                "coverage": coverage, "candidate_rows": len(data), "filled_rows": int(data.filled.sum()),
                "label_status": data.label_status.value_counts().to_dict(), "regimes": data.regime.value_counts().to_dict(),
                "first_day": data.day.min(), "last_day": data.day.max(),
                "days": sorted(set(pd.to_datetime(accum.index, utc=True).tz_convert("Asia/Seoul").strftime("%Y-%m-%d"))),
                "dataset_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "limitations": [universe["universe_scope"], universe["venue"], "OHLC-only conservative execution estimates; not actual fills",
                                "Lecture-derived quantitative hypotheses; human pattern labels not yet applied"]}
    atomic_json(root / "dataset.json", manifest)
    return manifest
