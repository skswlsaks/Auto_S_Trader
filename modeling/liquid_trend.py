"""Past-only liquidity/trend context and minute-close stop-exit research probe."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .execution import sell_tax_bps
from .pattern_eval import sha

PARAMETERS = {"version": "liquid-trend-v1", "daily_turnover20_min": 10_000_000_000,
              "minute_turnover5_min": 100_000_000, "active_bars5_min": 4,
              "max_stop_risk_pct": .01, "stop_atr_buffer": .1,
              "threshold": .5, "training_cutoff": "2026-08-26"}
CONTEXT_FEATURES = ["log_turnover20", "daily_close_ma20", "daily_ma20_ma60", "daily_slope5",
                    "log_turnover5", "volume_expansion5", "active_fraction5", "ema5_ema20",
                    "ema20_slope5", "vwap_distance", "recent_volume_capacity_ratio"]


def contract():
    return {"source_sha256": sha(__file__), "parameters": PARAMETERS, "features": CONTEXT_FEATURES}


def daily_context(adjusted, raw):
    a, r = [f.sort_values("timestamp").reset_index(drop=True) for f in (adjusted, raw)]
    if not a.timestamp.equals(r.timestamp):
        raise ValueError("Raw/adjusted daily timestamps differ")
    ma20, ma60 = a.close.rolling(20).mean(), a.close.rolling(60).mean()
    f = pd.DataFrame({"selection_day": pd.to_datetime(a.timestamp, utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d"),
                      "turnover20": (r.close*r.volume).rolling(20).median(),
                      "daily_close_ma20": a.close/ma20-1, "daily_ma20_ma60": ma20/ma60-1,
                      "daily_slope5": ma20/ma20.shift(5)-1})
    if f.selection_day.duplicated().any():
        raise ValueError("Duplicate daily date")
    f["daily_focus"] = (f.turnover20.ge(PARAMETERS["daily_turnover20_min"]) &
                        f.daily_close_ma20.gt(0) & f.daily_ma20_ma60.gt(0) & f.daily_slope5.gt(0))
    f["log_turnover20"] = np.log1p(f.turnover20)
    return f


def intraday_context(bars, daily, execution):
    """All rolling/EMA/VWAP calculations restart at a gap or a session boundary."""
    parts = []
    for _, frame in bars.groupby("segment", sort=False):
        f = frame.copy().reset_index(drop=True)
        turnover = f.close*f.volume
        f["turnover5"] = turnover.rolling(5, min_periods=5).sum()
        f["log_turnover5"] = np.log1p(f.turnover5)
        f["active_fraction5"] = f.volume.gt(0).rolling(5, min_periods=5).mean()
        baseline = f.volume.shift(5).rolling(20, min_periods=10).mean().replace(0, np.nan)
        recent = f.volume.rolling(5, min_periods=5).mean()
        f["volume_expansion5"] = recent/baseline
        ema5, ema20 = [f.close.ewm(span=n, adjust=False).mean() for n in (5, 20)]
        f["ema5_ema20"], f["ema20_slope5"] = ema5/ema20-1, ema20/ema20.shift(5)-1
        vwap = turnover.cumsum()/f.volume.cumsum().replace(0, np.nan)
        f["vwap_distance"] = f.close/vwap-1
        f["recent_volume_mean5"] = recent
        desired = np.floor(execution["max_position_krw"]/f.close).clip(lower=1)
        f["recent_volume_capacity_ratio"] = np.floor(recent*execution["participation"])/desired
        for name in ("log_turnover20", "daily_close_ma20", "daily_ma20_ma60", "daily_slope5"):
            f[name] = daily[name]
        f["minute_eligible"] = (f.turnover5.ge(PARAMETERS["minute_turnover5_min"]) &
                                 f.active_fraction5.ge(PARAMETERS["active_bars5_min"]/5) &
                                 f.close.gt(ema20) & f.ema5_ema20.gt(0) &
                                 f.ema20_slope5.gt(0) & f.vwap_distance.ge(0))
        f["recent_low5"] = f.low.rolling(5, min_periods=5).min()
        parts.append(f)
    return pd.concat(parts, ignore_index=True)


def alarms(scores, frame, filtered):
    eligible = frame.atr_pct.notna().to_numpy()
    if filtered:
        eligible &= frame.minute_eligible.to_numpy()
    above = (np.asarray(scores) >= PARAMETERS["threshold"]) & eligible
    previous = np.r_[False, above[:-1]] & frame.segment.eq(frame.segment.shift()).to_numpy()
    return above & ~previous


def stop_probe(bars, signal_index, config, reaction_bars=1):
    """Observe stop touches at a completed minute; sell from the NEXT bar onward.

    Capacity uses whole-minute traded volume, not executable depth. Thus an exit
    is a conditional OHLCV simulation, never evidence of an actual broker fill.
    """
    if type(reaction_bars) is not int or reaction_bars < 1:
        raise ValueError("Reaction must be at least one later bar")
    e, s = config["execution"], bars.iloc[signal_index]
    result = {"status": "not_entered", "filled": False, "stop_triggered": False,
              "stop_complete": False, "reaction_bars": reaction_bars,
              "signal_at": s.timestamp.isoformat()}
    horizon = e["max_holding_minutes"]
    if s.minute+horizon > 920:
        return {**result, "status": "insufficient_session_remaining"}
    future = bars.iloc[signal_index+1:signal_index+1+horizon].copy().reset_index(drop=True)
    if len(future) < horizon or not future.timestamp.diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all() or future.timestamp.iloc[0] != s.timestamp+pd.Timedelta(minutes=1):
        return {**result, "status": "future_gap"}
    a = float(s.atr_pct*s.close)
    support = s.pullback_low if pd.notna(s.pullback_low) else s.recent_low5
    stop = float(support-PARAMETERS["stop_atr_buffer"]*a)
    impact = (e["slippage_bps"]+e["half_spread_bps"])/10000
    buy_fee = e["buy_fee_bps"]/10000
    entry = float(future.open.iloc[0])*(1+impact)
    risk = (entry-stop)/entry
    if not np.isfinite(stop) or not 0 < stop < entry or risk > PARAMETERS["max_stop_risk_pct"]:
        return {**result, "status": "stop_risk_rejected", "stop_price": stop if np.isfinite(stop) else None,
                "initial_risk_pct": float(risk) if np.isfinite(risk) else None}
    full_order = math.floor(e["max_position_krw"]/(s.close*(1+impact)*(1+buy_fee)))
    planned = min(full_order, math.floor(s.recent_volume_mean5*e["participation"]))
    quantity = min(planned, math.floor(future.volume.iloc[0]*e["participation"]),
                   math.floor(e["max_position_krw"]/(entry*(1+buy_fee))))
    if quantity < 1:
        return {**result, "status": "entry_unfilled", "planned_quantity": planned}
    capital = quantity*entry*(1+buy_fee)
    next_capacity = math.floor(future.volume.iloc[1]*e["participation"])
    result.update(filled=True, entry_price=entry, stop_price=stop, initial_risk_pct=float(risk),
                  quantity=quantity, planned_quantity=planned, full_budget_quantity=full_order,
                  planned_notional=float(planned*s.close), capital=capital,
                  entry_fill_fraction=quantity/planned, entry_at=(future.timestamp.iloc[0]-pd.Timedelta(minutes=1)).isoformat(),
                  next_bar_exit_capacity_pass=next_capacity >= quantity,
                  full_budget_next_bar_capacity_pass=next_capacity >= full_order)
    left, proceeds, trigger, sold_weighted, first_exit, last_exit = quantity, 0., None, 0., None, None
    for i, bar in enumerate(future.itertuples(index=False)):
        if trigger is not None and i >= trigger+reaction_bars:
            amount = min(left, math.floor(bar.volume*e["participation"]))
            if amount:
                price = bar.open*(1-impact)
                proceeds += amount*price*(1-(e["sell_fee_bps"]+sell_tax_bps(e, bar.timestamp))/10000)
                sold_weighted += amount*price
                left -= amount
                first_exit = i if first_exit is None else first_exit
                last_exit = i
        if not left:
            break
        if trigger is None and bar.low <= stop:
            trigger = i
    last = future.iloc[i]
    sell_cost = (e["sell_fee_bps"]+sell_tax_bps(e, last.timestamp))/10000
    marked = proceeds+left*last.close*(1-impact)*(1-sell_cost)-capital
    result.update(status="stop_complete" if not left else "stop_residual" if trigger is not None else "no_stop_in_horizon",
                  stop_triggered=trigger is not None, stop_complete=not bool(left), remaining_quantity=left,
                  marked_net_return=float(marked/capital),
                  realized_net_return=float((proceeds-capital)/capital) if not left else None,
                  stop_observed_at=future.timestamp.iloc[trigger].isoformat() if trigger is not None else None,
                  first_exit_bar_end=future.timestamp.iloc[first_exit].isoformat() if first_exit is not None else None,
                  last_exit_bar_end=future.timestamp.iloc[last_exit].isoformat() if last_exit is not None else None,
                  completion_delay_bars=last_exit-trigger if not left else None,
                  adverse_stop_slippage_bps=max(0., (stop-sold_weighted/quantity)/stop*10000) if not left else None)
    return result


def exit_summary(rows):
    filled = [r for r in rows if r["filled"]]
    triggered = [r for r in filled if r["stop_triggered"]]
    completed = [r for r in triggered if r["stop_complete"]]
    losses = [max(0., -r["realized_net_return"])*100 for r in completed]
    return {"alarms": len(rows), "entered": len(filled), "entry_rejections": {k: sum(r["status"] == k for r in rows)
              for k in sorted({r["status"] for r in rows if not r["filled"]})},
            "stop_triggered": len(triggered), "stop_complete": len(completed),
            "next_bar_complete": sum(r["completion_delay_bars"] == 1 for r in completed),
            "next_bar_complete_fraction_of_triggered": sum(r["completion_delay_bars"] == 1 for r in completed)/len(triggered) if triggered else None,
            "residual_stops": len(triggered)-len(completed), "no_stop_in_horizon": len(filled)-len(triggered),
            "next_bar_capacity_fraction": np.mean([r["next_bar_exit_capacity_pass"] for r in filled]).item() if filled else None,
            "full_budget_next_bar_capacity_fraction": np.mean([r["full_budget_next_bar_capacity_pass"] for r in filled]).item() if filled else None,
            "median_planned_notional": float(np.median([r["planned_notional"] for r in filled])) if filled else None,
            "worst_stopped_loss_pct": max(losses) if losses else None,
            "p95_stopped_loss_pct": float(np.quantile(losses, .95)) if losses else None,
            "max_completion_delay_bars": max(r["completion_delay_bars"] for r in completed) if completed else None}
