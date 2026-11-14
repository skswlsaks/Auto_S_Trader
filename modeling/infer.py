from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .features import FEATURES, symbol_features
from .train import load_bundle, read_dataset


def records_frame(records):
    f = pd.DataFrame(records)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(f.columns):
        raise ValueError("Bars require timestamp/open/high/low/close/volume")
    f["timestamp"] = pd.to_datetime(f.timestamp, utc=True)
    for k in required - {"timestamp"}:
        f[k] = pd.to_numeric(f[k], errors="raise")
    if f.timestamp.duplicated().any() or not np.isfinite(f[list(required - {"timestamp"})]).all().all():
        raise ValueError("Duplicate timestamps or nonfinite bar value")
    if ((f.low > f[["open", "close"]].min(axis=1)) | (f.high < f[["open", "close"]].max(axis=1)) | (f.low <= 0) | (f.volume < 0)).any():
        raise ValueError("Invalid OHLCV")
    return f.sort_values("timestamp").reset_index(drop=True)


def finite(value):
    return float(value) if np.isfinite(value) else None


class Predictor:
    def __init__(self, run):
        self.run = Path(run)
        self.bundle = load_bundle(run)
        self.config = self.bundle["config"]

    def predict(self, payload):
        started = time.perf_counter()
        symbol, as_of = payload["symbol"], pd.Timestamp(payload["as_of"])
        if as_of.tzinfo is None:
            raise ValueError("as_of must have timezone")
        as_of = as_of.tz_convert("UTC")
        base = {"mode": "research_no_orders", "symbol": symbol, "as_of": as_of.isoformat(),
                "model_version": self.bundle["version"], "trained_through": self.bundle["trained_through"],
                "diagnostic_model": self.bundle["diagnostic"], "policy_active": self.bundle["policy_active"],
                "historical_in_sample": as_of.tz_convert("Asia/Seoul").strftime("%Y-%m-%d") <= self.bundle["trained_through"]}
        bars = records_frame(payload["bars"])
        if bars.empty or (bars.timestamp > as_of).any():
            raise ValueError("Empty bars or bars beyond as_of")
        local = as_of.tz_convert("Asia/Seoul")
        minute = local.hour * 60 + local.minute
        if local.weekday() >= 5 or not 541 <= minute <= 920:
            return {**base, "status": "outside_regular_clock", "decision": "hold"}
        if (as_of - bars.timestamp.iloc[-1]).total_seconds() > 120:
            return {**base, "status": "stale_data", "decision": "hold"}
        adjusted, raw = records_frame(payload["daily_adjusted"]), records_frame(payload["daily_raw"])
        f = symbol_features(bars, adjusted, raw, symbol, self.config)
        if f.empty:
            return {**base, "status": "insufficient_history", "decision": "hold"}
        last = f.iloc[-1].copy()
        sector = payload.get("sector", {})
        for name in ("sector_ret15", "sector_breadth", "sector_daily_momentum20"):
            value = sector.get(name)
            if value is None or not np.isfinite(value):
                return {**base, "status": "missing_sector_context", "decision": "hold"}
            last[name] = value
        if sector.get("as_of") != last.timestamp.isoformat():
            return {**base, "status": "sector_timestamp_mismatch", "decision": "hold"}
        last["relative_strength15"] = last.ret_15 - last.sector_ret15
        if not last.candidate:
            return {**base, "status": "observing", "decision": "hold", "bar_at": last.timestamp.isoformat(),
                    "processing_ms": (time.perf_counter() - started) * 1000}
        if last.minute + self.config["execution"]["max_holding_minutes"] > 920:
            return {**base, "status": "insufficient_session_remaining", "decision": "hold"}
        x = pd.DataFrame([{k: last[k] for k in FEATURES}], columns=FEATURES).astype("float32")
        score = float(self.bundle["model"].predict(x)[0])
        decision = "candidate_accepted" if score >= self.bundle["threshold"] else "hold"
        return {**base, "status": "pattern_confirmed", "decision": decision, "bar_at": last.timestamp.isoformat(),
                "expected_net_R": score, "threshold": self.bundle["threshold"], "initial_stop": float(last.stop_price),
                "pattern_id": last.pattern_id, "processing_ms": (time.perf_counter() - started) * 1000,
                "evidence": {k: finite(last[k]) for k in ("high_slope", "low_slope", "higher_low_atr", "pullback_depth_atr",
                                                          "recovery_atr", "rebound_volume_ratio", "relative_strength15")},
                "note": "Candidate score only; portfolio cash, slots, actual fills and protection require the execution engine"}


def historical_predictions(run, count=10):
    bundle = load_bundle(run)
    frame, manifest = read_dataset(bundle["config"])
    if manifest["dataset_sha256"] != bundle["dataset_sha256"]:
        raise ValueError("Dataset changed since model training")
    evaluated = (Path(run) / "evaluation.json").exists()
    if not evaluated:
        frame = frame.loc[frame.day <= bundle["trained_through"]]
    local = frame.timestamp.dt.tz_convert("Asia/Seoul")
    minute = local.dt.hour * 60 + local.dt.minute
    frame = frame.loc[minute + bundle["config"]["execution"]["max_holding_minutes"] <= 920]
    sample = frame.tail(count).copy()
    scores = bundle["model"].predict(sample[FEATURES])
    result = []
    for (_, row), score in zip(sample.iterrows(), scores):
        result.append({"symbol": row.symbol, "at": row.timestamp.isoformat(), "expected_net_R": float(score),
                       "decision": "candidate_accepted" if score >= bundle["threshold"] else "hold",
                       "initial_stop": float(row.stop_price), "pattern_id": row.pattern_id})
    return {"mode": "historical_inference_not_live", "holdout_previously_evaluated": evaluated,
            "diagnostic_model": bundle["diagnostic"], "predictions": result}


def serve(run, input_stream, output_stream):
    predictor = Predictor(run)
    for line in input_stream:
        try:
            if len(line) > 10_000_000:
                raise ValueError("Input line exceeds 10 MB")
            result = predictor.predict(json.loads(line))
        except (ValueError, KeyError, TypeError) as error:
            result = {"status": "rejected_input", "decision": "hold", "error_type": type(error).__name__}
        output_stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
        output_stream.flush()


def benchmark(run, count=30):
    from .config import atomic_json
    predictor = Predictor(run)
    frame, _ = read_dataset(predictor.config)
    eligible = frame[(frame.day <= predictor.bundle["trained_through"]) & frame.filled]
    eligible = eligible[pd.to_datetime(eligible.timestamp).dt.tz_convert("Asia/Seoul").dt.hour * 60 +
                        pd.to_datetime(eligible.timestamp).dt.tz_convert("Asia/Seoul").dt.minute <=
                        920 - predictor.config["execution"]["max_holding_minutes"]]
    row = eligible.iloc[-1]
    root = Path(predictor.config["data_dir"]) / "candles"
    minute, adjusted, raw = [pd.read_parquet(root/f"{row.symbol}-{suffix}.parquet") for suffix in ("1m-raw", "1d-adjusted", "1d-raw")]
    day_start = pd.Timestamp(row.day, tz="Asia/Seoul").tz_convert("UTC")
    minute = minute[(minute.timestamp >= day_start) & (minute.timestamp <= row.timestamp)]
    adjusted, raw = adjusted[adjusted.timestamp < day_start].tail(300), raw[raw.timestamp < day_start].tail(300)
    def records(f):
        f = f.copy()
        f["timestamp"] = f.timestamp.map(lambda t: t.isoformat())
        return f.to_dict("records")
    payload = {"symbol": row.symbol, "as_of": row.timestamp.isoformat(), "bars": records(minute),
               "daily_adjusted": records(adjusted), "daily_raw": records(raw),
               "sector": {"as_of": row.timestamp.isoformat(), **{k: float(row[k]) for k in ("sector_ret15", "sector_breadth", "sector_daily_momentum20")}}}
    expected = float(predictor.bundle["model"].predict(pd.DataFrame([row[FEATURES]], columns=FEATURES).astype("float32"))[0])
    first = predictor.predict(payload)
    if first["status"] != "pattern_confirmed" or not np.isclose(first["expected_net_R"], expected, atol=1e-6, rtol=1e-6):
        raise ValueError("Batch vs streaming-prefix inference mismatch")
    timings = []
    for _ in range(max(1, count)):
        started = time.perf_counter()
        predictor.predict(payload)
        timings.append((time.perf_counter() - started) * 1000)
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {"mode": "local_cpu_historical_payload_benchmark", "samples": len(timings),
              "python_version": sys.version,
              "one_symbol_one_completed_bar": True, "batch_prefix_prediction_matches": True,
              "p50_ms": float(np.quantile(timings, .5)), "p95_ms": float(np.quantile(timings, .95)),
              "p99_ms": float(np.quantile(timings, .99)), "peak_rss_mb": usage / (1024**2 if sys.platform == "darwin" else 1024),
              "inference": first, "note": "Warm repeated single-symbol payload; excludes broker latency. Peak RSS is process-lifetime, including prior data/model loading or training. Not a sustained thermal benchmark"}
    atomic_json(Path(run) / "benchmark.json", result)
    atomic_json(Path(run) / "inference-example.json", payload)
    (Path(run) / "inference-example.jsonl").write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    return result
