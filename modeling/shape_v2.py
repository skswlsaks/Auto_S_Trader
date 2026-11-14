"""Causal structure tracking for shape research; separate from the frozen return model."""
from __future__ import annotations

from itertools import combinations
from pathlib import Path
import hashlib

import numpy as np
import pandas as pd

from .config import digest

PARAMETERS = {
    "version": "shape-v2-research-1",
    "pivot_right_bars": 2,
    "lookback_bars": 60,
    "minimum_anchor_gap": 6,
    "minimum_structure_bars": 10,
    "maximum_pivots": 8,
    "breakout_atr": .1,
    "flat_tolerance_atr": .25,
    "higher_low_atr": .05,
    "pullback_depth_atr": .5,
    "invalidation_atr": .25,
    "rebound_atr": .5,
    "setup_max_bars": 30,
}
SHAPE_FEATURES = ["high_slope_atr", "structure_span", "breakout_age", "higher_low_atr",
                  "pullback_depth_atr", "recovery_atr", "resistance_distance_atr",
                  "support_distance_atr", "previous_high_distance_atr", "volume_ratio",
                  "atr_pct", "active_structures", "in_breakout", "in_pullback",
                  "ret_1", "ret_5", "ret_15", "range_position20", "distance_low20_atr"]


def contract():
    return {"source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "parameters": PARAMETERS, "feature_names": SHAPE_FEATURES}


def pullback_started(low, prior_peak, atr, parameters=PARAMETERS):
    # A wick can be a pullback even when that bar closes above the previous close.
    return prior_peak - low >= parameters["pullback_depth_atr"] * atr


def structure_broken(close, structural_low, atr, parameters=PARAMETERS):
    return close < structural_low - parameters["invalidation_atr"] * atr


def detect(bars, symbol, parameters=None):
    """Input is regular_bars(); only confirmed pivots and current/past OHLCV are read.

    Rule states are features, never supervised stage labels. Each pivot pair is
    tracked independently; a new small pivot cannot erase an existing structure.
    """
    p = dict(PARAMETERS if parameters is None else parameters)
    if set(p) != set(PARAMETERS) or any(not isinstance(v, (int, float)) or not np.isfinite(v) or v <= 0
                                       for k, v in p.items() if k != "version"):
        raise ValueError("Invalid shape parameters")
    for k in ("pivot_right_bars", "lookback_bars", "minimum_anchor_gap", "minimum_structure_bars",
              "maximum_pivots", "setup_max_bars"):
        if type(p[k]) is not int:
            raise ValueError(f"{k} must be an integer")
    required = {"timestamp", "segment", "open", "high", "low", "close", "volume"}
    if not required.issubset(bars.columns) or bars.empty:
        raise ValueError("Nonempty regular OHLCV bars required")
    numeric = bars[["open", "high", "low", "close", "volume"]]
    if bars.timestamp.duplicated().any() or not bars.timestamp.is_monotonic_increasing or not np.isfinite(numeric).all().all():
        raise ValueError("Invalid or unsorted bars")
    if ((bars.low <= 0) | (bars.low > bars[["open", "close"]].min(axis=1)) |
        (bars.high < bars[["open", "close"]].max(axis=1)) | (bars.volume < 0)).any():
        raise ValueError("Invalid OHLCV")
    parts = []
    for _, frame in bars.groupby("segment", sort=False):
        f = frame.copy().reset_index(drop=True)
        prev = f.close.shift()
        tr = pd.concat([f.high-f.low, (f.high-prev).abs(), (f.low-prev).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=14).mean().to_numpy()
        volume_mean = f.volume.shift().rolling(20, min_periods=10).mean().to_numpy()
        hi, lo, close, volume = [f[k].to_numpy() for k in ("high", "low", "close", "volume")]
        highs, lows, setups, used_pairs, emitted_bases = [], [], {}, set(), set()
        rows = []
        right = p["pivot_right_bars"]
        for i in range(len(f)):
            row = {name: np.nan for name in SHAPE_FEATURES}
            row.update(candidate=False, rule_stage="observing", pattern_id="", structure_type="",
                       resistance_price=np.nan, structural_low=np.nan, pullback_low=np.nan,
                       anchor_high_1=-1, anchor_high_2=-1, breakout_index=-1, pullback_index=-1,
                       invalidated_count=0, expired_count=0)
            if i >= 2*right:
                j = i-right
                if hi[j] >= max(hi[j-right:j]) and hi[j] > max(hi[j+1:i+1]):
                    highs.append((j, hi[j]))
                if lo[j] <= min(lo[j-right:j]) and lo[j] < min(lo[j+1:i+1]):
                    lows.append((j, lo[j]))
            highs = [(j, v) for j, v in highs if i-j <= p["lookback_bars"]][-p["maximum_pivots"]:]
            lows = [(j, v) for j, v in lows if i-j <= p["lookback_bars"]]
            a = atr[i]
            if i < 20 or not np.isfinite(a) or a <= 0:
                rows.append(row)
                continue
            row.update(previous_high_distance_atr=(close[i]-hi[i-1])/a,
                       volume_ratio=volume[i]/volume_mean[i] if volume_mean[i] > 0 else np.nan,
                       atr_pct=a/close[i],
                       ret_1=close[i]/close[i-1]-1, ret_5=close[i]/close[i-5]-1,
                       ret_15=close[i]/close[i-15]-1,
                       range_position20=(close[i]-min(lo[i-19:i+1]))/max(max(hi[i-19:i+1])-min(lo[i-19:i+1]), a*.01),
                       distance_low20_atr=(close[i]-min(lo[i-19:i+1]))/a)
            for key, s in list(setups.items()):
                if structure_broken(close[i], s["base"], a, p):
                    del setups[key]
                    row["invalidated_count"] += 1
                elif i-s["start"] > p["setup_max_bars"]:
                    del setups[key]
                    row["expired_count"] += 1
            # Both sides of a crossing use the same anchored line at their own time.
            for (j1, v1), (j2, v2) in combinations(highs, 2):
                key = (j1, j2)
                if key in used_pairs or j2-j1 < p["minimum_anchor_gap"] or i-j1 < p["minimum_structure_bars"]:
                    continue
                if v2-v1 > p["flat_tolerance_atr"]*a:
                    continue
                slope = (v2-v1)/(j2-j1)
                current_line, prior_line = v2+slope*(i-j2), v2+slope*(i-1-j2)
                if not (close[i] > current_line+p["breakout_atr"]*a and
                        close[i-1] <= prior_line+p["breakout_atr"]*a and volume[i] > 0):
                    continue
                # A line substantially crossed between its anchors is not resistance.
                between = hi[j1+1:j2]
                projected = v1+slope*np.arange(1, j2-j1)
                if np.any(between > projected + p["flat_tolerance_atr"]*a):
                    continue
                since = np.arange(j2+1, i)
                if np.any(close[j2+1:i] > v2+slope*(since-j2)+p["flat_tolerance_atr"]*a):
                    continue
                kind = "reversal"
                if v1-v2 <= p["flat_tolerance_atr"]*a:
                    if len(lows) < 2 or lows[-1][1]-lows[-2][1] <= p["higher_low_atr"]*a:
                        continue
                    kind = "rising_low_convergence"
                base_index = j2+int(np.argmin(lo[j2:i+1]))
                if base_index in emitted_bases:
                    continue
                setups[key] = dict(start=i, j1=j1, j2=j2, v2=v2, slope=slope, base=lo[base_index],
                                   base_index=base_index, peak=hi[i], low=np.inf, stage="breakout",
                                   pullback=-1, kind=kind)
                used_pairs.add(key)
            confirmations = []
            for key, s in setups.items():
                if s["start"] == i:
                    continue
                previous_peak = s["peak"]
                s["peak"] = max(s["peak"], hi[i])
                if (s["stage"] == "breakout" and i-s["start"] >= 2 and lo[i] < lo[i-1] and
                    pullback_started(lo[i], previous_peak, a, p)):
                    s.update(stage="pullback", low=lo[i], pullback=i)
                if s["stage"] == "pullback":
                    s["low"] = min(s["low"], lo[i])
                    line = s["v2"] + s["slope"]*(i-s["j2"])
                    if (i > s["pullback"] and s["low"] > s["base"]+p["higher_low_atr"]*a and
                        close[i] > hi[i-1] and close[i] > line and
                        close[i]-s["low"] >= p["rebound_atr"]*a and volume[i] > 0 and
                        s["base_index"] not in emitted_bases):
                        confirmations.append((key, s))
            if confirmations:
                # One alert per structural base, without a global new-pattern cooldown.
                _, chosen = max(confirmations, key=lambda x: x[1]["j2"]-x[1]["j1"])
                row["candidate"] = True
                emitted_bases.update(s["base_index"] for _, s in confirmations)
                row["rule_stage"] = "confirmed"
            elif setups:
                chosen = max(setups.values(), key=lambda s: (s["stage"] == "pullback", s["start"]))
                row["rule_stage"] = chosen["stage"]
            else:
                chosen = None
                if row["invalidated_count"]:
                    row["rule_stage"] = "invalidated"
            row["active_structures"] = len(setups)
            row["in_breakout"] = float(row["rule_stage"] == "breakout")
            row["in_pullback"] = float(row["rule_stage"] == "pullback")
            if chosen:
                s = chosen
                line = s["v2"]+s["slope"]*(i-s["j2"])
                pull_low = s["low"] if np.isfinite(s["low"]) else np.nan
                row.update(high_slope_atr=s["slope"]/a, structure_span=i-s["j1"],
                           breakout_age=i-s["start"], higher_low_atr=(pull_low-s["base"])/a,
                           pullback_depth_atr=(s["peak"]-pull_low)/a, recovery_atr=(close[i]-pull_low)/a,
                           resistance_distance_atr=(close[i]-line)/a, support_distance_atr=(close[i]-s["base"])/a,
                           resistance_price=line, structural_low=s["base"], pullback_low=pull_low,
                           anchor_high_1=s["j1"], anchor_high_2=s["j2"], breakout_index=s["start"],
                           pullback_index=s["pullback"], structure_type=s["kind"],
                           pattern_id=digest([p["version"], symbol, f.timestamp.iloc[s["base_index"]].isoformat()])[:20])
            setups = {key: s for key, s in setups.items() if s["base_index"] not in emitted_bases}
            rows.append(row)
        parts.append(pd.concat([f, pd.DataFrame(rows)], axis=1))
    return pd.concat(parts, ignore_index=True)
