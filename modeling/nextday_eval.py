"""D close daily-rule screening -> D+1 minute detector/model replay, without PnL labels."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pandas as pd

from .config import atomic_json, load_config
from .features import FEATURES, contract_hashes, regular_bars
from .pattern_eval import sha
from .train import load_bundle

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO/"outputs/model-research/nextday/20261030-v1"
DEFAULT_RUN = REPO/"outputs/model-research/runs/20261029T161547021944Z"


def prior_sessions(trading_days, daily_calendar):
    calendar = sorted(set(daily_calendar) | set(trading_days))
    return {day: calendar[i-1] for i, day in enumerate(calendar) if day in set(trading_days) and i > 0}


def replay_daily(symbols, selection_days):
    reply = subprocess.run(["node", str(REPO/"trading/nextday_watch.mjs")],
        input=json.dumps({"symbols": symbols, "selectionDays": selection_days}, allow_nan=False),
        capture_output=True, text=True, check=True)
    return json.loads(reply.stdout)


def daily_frame(path):
    f = pd.read_parquet(path).sort_values("timestamp").reset_index(drop=True)
    f["date"] = pd.to_datetime(f.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    if f.date.duplicated().any():
        raise ValueError("Duplicate daily candle")
    return f


def build(output, run=DEFAULT_RUN, policy="union"):
    if (output/"selection.json").exists():
        raise ValueError("Selection is frozen; choose a new output directory")
    c = load_config()
    root = Path(c["data_dir"])
    dataset = json.loads((root/"dataset.json").read_text())
    if dataset["contract_hashes"] != contract_hashes() or sha(root/"candidates.parquet") != dataset["dataset_sha256"]:
        raise ValueError("Original frozen detector/dataset changed")
    symbols = json.loads((root/"universe.json").read_text())["symbols"]
    inputs, daily, hashes = [], {}, {}
    for symbol in symbols:
        adjusted_path, raw_path = [root/"candles"/f"{symbol}-1d-{x}.parquet" for x in ("adjusted", "raw")]
        adjusted, raw = daily_frame(adjusted_path), daily_frame(raw_path)
        if adjusted.date.tolist() != raw.date.tolist():
            raise ValueError("Adjusted/raw daily dates differ")
        inputs.append({"symbol": symbol, "bars": adjusted[["date", "open", "high", "low", "close", "volume"]].to_dict("records")})
        daily[symbol] = raw[["date", "close"]].rename(columns={"close": "raw_close"}).merge(
            adjusted[["date", "close"]].rename(columns={"close": "adjusted_close"}), on="date", validate="one_to_one").set_index("date")
        hashes.update({str(adjusted_path): sha(adjusted_path), str(raw_path): sha(raw_path)})
    previous = prior_sessions(dataset["days"], [b["date"] for x in inputs for b in x["bars"]])
    replay = replay_daily(inputs, sorted(set(previous.values())))
    by_day = {selection: day for day, selection in previous.items()}
    records = []
    for row in replay["rows"]:
        selection_day = row.pop("asOf")
        if selection_day not in by_day:
            continue
        d = daily[row["symbol"]].loc[selection_day]
        factor = float(d.raw_close / d.adjusted_close)
        ref, point, broken = row.pop("reference", None), row.pop("point", None), row.pop("broken", None)
        selected = row["selected"] if policy == "union" else row["intersection"]
        records.append({**row, "selected": bool(selected), "day": by_day[selection_day], "selection_day": selection_day,
            "known_at": selection_day+"T23:59:59+09:00", "reference_day": ref["date"] if ref else None,
            "reference_type": ref["type"] if ref else None, "reference_price": ref["level"]*factor if ref else None,
            "secret_price": ref["secret"]*factor if ref and ref["secret"] is not None else None,
            "ma20_price": row["ma20"]*factor if "ma20" in row else None,
            "point_day": point["date"] if point else None, "point_price": point["close"]*factor if point else None,
            "invalidation_day": broken["date"] if broken else None, "invalidation_line": broken["line"] if broken else None,
            "will_is_strong_breakout_confirmed": False, "daily_price_scale_factor": factor})
    watch = pd.DataFrame(records).sort_values(["day", "symbol"]).reset_index(drop=True)
    if watch.duplicated(["day", "symbol"]).any() or not (watch.selection_day < watch.day).all():
        raise ValueError("Daily selection must precede next market session exactly once")
    selected = watch.loc[watch.selected].copy()
    if selected.empty:
        raise ValueError("No selected next-day stock sessions")
    candidates = pd.read_parquet(root/"candidates.parquet", columns=["symbol", "day", "timestamp", "pattern_id", "stop_price", *FEATURES])
    chosen = candidates.merge(selected, on=["symbol", "day"], how="inner", validate="many_to_one")
    coverage, minute_hashes, closes = [], {}, []
    for symbol, days in selected.groupby("symbol"):
        path = root/"candles"/f"{symbol}-1m-raw.parquet"
        bars = regular_bars(pd.read_parquet(path))
        minute_hashes[str(path)] = sha(path)
        counts = bars.groupby("day").size()
        for day in days.day:
            coverage.append({"symbol": symbol, "day": day, "minute_rows": int(counts.get(day, 0))})
        wanted = chosen.loc[chosen.symbol.eq(symbol), "timestamp"]
        closes.append(bars.loc[bars.timestamp.isin(wanted), ["timestamp", "close"]].assign(symbol=symbol))
    chosen = chosen.merge(pd.concat(closes, ignore_index=True), on=["symbol", "timestamp"], validate="one_to_one")
    chosen["above_prior_secret_at_signal"] = chosen.close.ge(chosen.secret_price).where(chosen.secret_price.notna())
    chosen["above_prior_ma20_at_signal"] = chosen.close.ge(chosen.ma20_price)
    bundle = load_bundle(run)
    if bundle["dataset_sha256"] != dataset["dataset_sha256"]:
        raise ValueError("Model trained on a different dataset")
    chosen["expected_net_R"] = bundle["model"].predict(chosen[FEATURES])
    chosen["historical_in_sample"] = chosen.day.le(bundle["trained_through"])
    chosen["return_policy_passed"] = chosen.expected_net_R.ge(bundle["threshold"])
    # The profit gate remains recorded; it is not used as a shape detector.
    chosen["pattern_detected"] = True
    output.mkdir(parents=True, exist_ok=True)
    watch.to_parquet(output/"watchlist.parquet", index=False)
    chosen.to_parquet(output/"selected-candidates.parquet", index=False)
    pd.DataFrame(coverage).to_parquet(output/"coverage.parquet", index=False)
    routes = {"reference_valid": int(selected.referenceValid.sum()), "will_recovery_proxy": int(selected.willRecovery.sum()),
              "both": int(selected.intersection.sum()), "bearish_reference_secret_undefined": int((selected.referenceValid & ~selected.secretDefined).sum()),
              "prior_day_point_entry": int(selected.pointEntry.sum())}
    n_by_day = selected.groupby("day").size()
    summary = {"version": "nextday-daily-rule-v1", "policy": policy, "selection_rule": f"D close: latest valid reference/secret/MA20 {'OR' if policy == 'union' else 'AND'} same-day MA20 recovery proxy; D+1 only",
        "rules": replay["rules"], "selected_stock_days": len(selected), "screened_stock_days": len(watch), "symbols": selected.symbol.nunique(),
        "market_days": len(dataset["days"]), "days_with_selection": selected.day.nunique(), "first_trade_day": selected.day.min(), "last_trade_day": selected.day.max(),
        "daily_watchlist_size": {"median": float(n_by_day.median()), "min": int(n_by_day.min()), "max": int(n_by_day.max())}, "routes": routes,
        "selected_candidate_count": len(chosen), "all_candidate_count": len(candidates), "selected_regular_stock_hours": sum(x["minute_rows"] for x in coverage)/60,
        "all_regular_stock_hours": sum(x["minute_rows"] for x in dataset["coverage"])/60,
        "model": {"path": str(run), "sha256": sha(Path(run)/"model.joblib"), "trained_through": bundle["trained_through"],
                  "policy_active": bool(bundle["policy_active"]), "return_policy_passed": int(chosen.return_policy_passed.sum()),
                  "shape_probability": False, "note": "Frozen net_R regressor replayed without retraining; shape recognition is evaluated separately"},
        "source_hashes": {**hashes, **minute_hashes}, "rule_sources": {str(REPO/"trading"/name): sha(REPO/"trading"/name) for name in ("jongga_scan.mjs", "nextday_watch.mjs")},
        "dataset_sha256": dataset["dataset_sha256"], "watchlist_sha256": sha(output/"watchlist.parquet"), "detector_contract": contract_hashes(),
        "limits": ["Current 206-stock semiconductor membership; historical delisted/membership and historical risk flags unavailable",
                   "Chart-rule subset only: historical market top-100 ranks, news, industry catalysts not reconstructed",
                   "Will is a MA20 recovery proxy, not a verified strong-breakout volume/size rule",
                   "Bearish reference has no defined secret; shown separately with MA20-only screening",
                   "Adjusted daily ratios used for rule structure; levels rescaled to D raw prices; historical point-in-time adjustment vintages unavailable",
                   "No D+1 close or D+1 return used for D selection; intraday price vs D lines is diagnostic, not a new user-confirmed rejection rule",
                   "No profitability or learned shape-classifier claim"]}
    atomic_json(output/"selection.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("source_hashes", "rule_sources", "rules", "limits")}, ensure_ascii=False, indent=2))


def verify(output):
    import numpy as np
    s = json.loads((output/"selection.json").read_text())
    for path, expected in {**s["source_hashes"], **s["rule_sources"]}.items():
        if sha(path) != expected:
            raise ValueError(f"Selection source changed: {path}")
    if sha(output/"watchlist.parquet") != s["watchlist_sha256"]:
        raise ValueError("Watchlist changed")
    watch = pd.read_parquet(output/"watchlist.parquet")
    expected = watch.referenceValid.eq(True) | watch.willRecovery.eq(True) if s["policy"] == "union" else watch.intersection.eq(True)
    if not watch.selected.eq(expected).all():
        raise ValueError("Selected flag differs from the declared policy")
    data = Path(load_config()["data_dir"])
    dataset = json.loads((data/"dataset.json").read_text())
    days = [d for path in s["source_hashes"] if path.endswith("1d-adjusted.parquet") for d in daily_frame(path).date]
    previous = prior_sessions(dataset["days"], days)
    if not watch.selection_day.eq(watch.day.map(previous)).all():
        raise ValueError("D selection was not assigned to the next actual market session")
    chosen = pd.read_parquet(output/"selected-candidates.parquet")
    original = pd.read_parquet(data/"candidates.parquet", columns=["symbol", "day", "timestamp", "pattern_id"])
    expected_rows = original.merge(watch.loc[watch.selected, ["symbol", "day"]], on=["symbol", "day"], validate="many_to_one")
    keys = ["timestamp", "symbol", "pattern_id"]
    if set(map(tuple, chosen[keys].to_numpy())) != set(map(tuple, expected_rows[keys].to_numpy())):
        raise ValueError("Filtered candidates are missing or outside the watchlist")
    if not (pd.to_datetime(chosen.timestamp, utc=True) > pd.to_datetime(chosen.known_at, utc=True)).all():
        raise ValueError("Intraday signal precedes its daily selection")
    bundle = load_bundle(s["model"]["path"])
    np.testing.assert_array_equal(bundle["model"].predict(chosen[FEATURES]), chosen.expected_net_R.to_numpy())
    coverage = pd.read_parquet(output/"coverage.parquet")
    result = {"status": "passed", "daily_sources_checked": sum("1d-" in p for p in s["source_hashes"]),
        "minute_sources_checked": sum("1m-" in p for p in s["source_hashes"]), "rule_sources_checked": len(s["rule_sources"]),
        "next_session_assignments_checked": len(watch), "candidate_membership_checked": len(chosen), "model_scores_reproduced": len(chosen),
        "selected_days_without_minute_data": int(coverage.minute_rows.eq(0).sum()), "selected_days_under_300_minutes": int(coverage.minute_rows.lt(300).sum()),
        "selection_json_sha256": sha(output/"selection.json"), "note": "Causal-selection and replay consistency, not independent shape accuracy"}
    atomic_json(output/"verification.json", result)
    print(json.dumps(result, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--run", type=Path, default=DEFAULT_RUN)
    p.add_argument("--policy", choices=["union", "intersection"], default="union")
    p.add_argument("--verify", action="store_true")
    a = p.parse_args()
    if a.verify:
        verify(a.output.resolve())
    else:
        build(a.output.resolve(), a.run.resolve(), a.policy)


if __name__ == "__main__":
    main()
