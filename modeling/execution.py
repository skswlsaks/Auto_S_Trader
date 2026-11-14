from __future__ import annotations

import bisect
import math

import numpy as np
import pandas as pd


def sell_tax_bps(execution, at):
    day = pd.Timestamp(at).tz_convert("Asia/Seoul").strftime("%Y-%m-%d")
    rate = execution["sell_tax_bps"]
    for row in execution.get("sell_tax_schedule", []):
        if day >= row["effective_from"]:
            rate = row["bps"]
    return rate


def outcome(bars, signal_at, stop_price, config, delay_bars=0, cost_multiplier=1.):
    """Conservative OHLC research fills, not reconstructed broker executions."""
    e = config["execution"]
    signal_at = pd.Timestamp(signal_at)
    horizon = e["max_holding_minutes"]
    base = {"filled": False, "label_status": "insufficient_future", "ambiguous": False}
    local = signal_at.tz_convert("Asia/Seoul")
    if local.hour * 60 + local.minute + delay_bars + horizon > 920:
        return base
    first = bars.timestamp.searchsorted(signal_at, side="right") + delay_bars
    future = bars.iloc[first:]
    if len(future) < horizon:
        return base
    expected = signal_at + pd.Timedelta(minutes=1 + delay_bars)
    if future.timestamp.iloc[0] != expected or (future.timestamp.iloc[:horizon].diff().dropna() != pd.Timedelta(minutes=1)).any():
        return {**base, "label_status": "future_gap"}
    impact = (e["slippage_bps"] + e["half_spread_bps"]) / 10000 * cost_multiplier
    buy_fee = e["buy_fee_bps"] / 10000 * cost_multiplier
    entry = float(future.open.iloc[0]) * (1 + impact)
    risk = entry - stop_price
    if not 0 < risk / entry <= e["max_initial_risk_pct"]:
        return {**base, "label_status": "risk_rejected"}
    quantity = min(math.floor(e["max_position_krw"] / (entry * (1 + buy_fee))),
                   math.floor(float(future.volume.iloc[0]) * e["participation"]))
    if quantity < 1:
        return {**base, "label_status": "entry_unfilled"}
    capital = quantity * entry * (1 + buy_fee)
    left, proceeds = quantity, 0.
    stop, trend_low, ambiguous = stop_price, stop_price, False
    path_times, path_pnl, path_remaining, path_ambiguous = [], [], [], []
    target_latched = [False, False]
    exit_reason = "timeout"
    force_exit = False
    lows = []
    for bar_index, bar in enumerate(future.itertuples(index=False)):
        sell_fee = (e["sell_fee_bps"] + sell_tax_bps(e, bar.timestamp)) / 10000 * cost_multiplier
        capacity = max(0, math.floor(bar.volume * e["participation"]))
        def sell(wanted, price):
            nonlocal left, proceeds, capacity
            amount = min(left, wanted, capacity)
            proceeds += amount * price * (1 - impact) * (1 - sell_fee)
            left -= amount
            capacity -= amount
        if force_exit and left:
            sell(left, bar.open)
        if left and not force_exit and bar.low <= stop:
            ambiguous |= bar.high >= entry * 1.03
            exit_reason = "stop"
            force_exit = True
            sell(left, min(bar.open, stop))
        if left and not force_exit:
            for k, (gain, fraction) in enumerate(((.03, .35), (.05, .70))):
                already_latched = target_latched[k]
                if already_latched or bar.high >= entry * (1 + gain):
                    target_latched[k] = True
                    target = math.floor(quantity * fraction)
                    sell(max(0, target - (quantity - left)), bar.open if already_latched else entry * (1 + gain))
            if bar.high >= entry * (1 + e["breakeven_trigger_pct"]):
                # Break-even includes modeled round-trip costs; stop never decreases.
                breakeven = entry * (1 + buy_fee) / ((1 - impact) * (1 - sell_fee))
                if breakeven <= bar.high:
                    stop = max(stop, breakeven)
                    if left and bar.low <= stop:
                        ambiguous = True
                        exit_reason, force_exit = "breakeven_ambiguous", True
                        # This stop is armed after the high, so the earlier open
                        # is not an executable post-trigger price.
                        sell(left, stop)
            lows.append(bar.low)
            if len(lows) >= 5 and lows[-3] < min(lows[-5:-3]) and lows[-3] < min(lows[-2:]):
                trend_low = max(trend_low, lows[-3])
            if bar.close < trend_low:
                exit_reason, force_exit = "trend_reversal", True
        if bar_index == horizon - 1 and left and not force_exit:
            exit_reason, force_exit = "timeout", True
            sell(left, bar.close)
        marked = proceeds + left * bar.close * (1 - impact) * (1 - sell_fee) - capital
        path_times.append(int(bar.timestamp.value))
        path_pnl.append(float(marked))
        path_remaining.append(int(left))
        path_ambiguous.append(bool(ambiguous))
        if not left:
            break
    pnl = path_pnl[-1]
    return {"filled": True, "label_status": "open_residual" if left else "complete", "ambiguous": bool(ambiguous),
            "entry_at": future.timestamp.iloc[0] - pd.Timedelta(minutes=1) + pd.Timedelta(microseconds=1),
            "exit_at": pd.NaT if left else pd.Timestamp(path_times[-1], tz="UTC"),
            "valuation_at": pd.Timestamp(path_times[-1], tz="UTC"), "remaining_quantity": int(left), "entry_price": entry,
            "quantity": quantity, "capital": capital, "net_pnl": pnl,
            "net_R": np.nan if left else pnl / (quantity * risk), "net_return": pnl / capital,
            "exit_reason": exit_reason, "path_times": path_times, "path_pnl": path_pnl,
            "path_remaining": path_remaining, "path_ambiguous": path_ambiguous}


def portfolio(candidates, scores, threshold, config, all_days=None):
    """Chronological, cash/slot constrained replay of fixed candidate outcomes."""
    e = config["execution"]
    work = candidates.copy()
    work["score"] = np.asarray(scores)
    days = sorted(all_days or work.day.unique().tolist())
    cutoff = pd.Timestamp(days[-1] + "T15:20:00", tz="Asia/Seoul").value if all_days else None
    work = work.loc[np.isfinite(work.score) & (work.score >= threshold)]
    work = work.sort_values(["timestamp", "score", "symbol"], ascending=[True, False, True])
    cash = float(e["initial_cash_krw"])
    active, accepted, equity_curve = [], [], []
    daily_pnl = {}
    prior_day, day_start, last_equity, halted = None, cash, cash, False
    events = sorted(set(int(t.value) for t in work.timestamp) | {int(t) for p in work.get("path_times", []) for t in p})
    if cutoff is not None:
        events = [t for t in events if t <= cutoff]
    else:
        days = sorted(set(days) | {pd.Timestamp(t, tz="UTC").tz_convert("Asia/Seoul").strftime("%Y-%m-%d") for t in events})
    groups = {int(t.value): g for t, g in work.groupby("timestamp", sort=False)}
    for now in events:
        day = pd.Timestamp(now, tz="UTC").tz_convert("Asia/Seoul").strftime("%Y-%m-%d")
        if day != prior_day:
            if prior_day is not None:
                daily_pnl[prior_day] = last_equity - day_start
            day_start, prior_day, halted = last_equity, day, False
        remaining = []
        for p in active:
            if pd.notna(p["exit_at"]) and int(p["exit_at"].value) <= now:
                cash += p["capital"] + p["net_pnl"]
                accepted[p["log_index"]].update(net_pnl=float(p["net_pnl"]), net_R=float(p["net_R"]),
                                                 closed=True, ambiguous=bool(p["ambiguous"]), exit_at=p["exit_at"].isoformat())
            else:
                remaining.append(p)
        active = remaining
        mark = 0.
        for p in active:
            index = bisect.bisect_right(p["path_times"], now) - 1
            mark += p["capital"] + (p["path_pnl"][index] if index >= 0 else -p["capital"] * e["buy_fee_bps"] / 10000)
        current_equity = cash + mark
        halted |= current_equity <= day_start * (1 - e["daily_loss_limit_pct"])
        if now in groups and not halted:
            for row in groups[now].to_dict("records"):
                if row["label_status"] in ("exit_unfilled", "future_gap"):
                    raise ValueError("Selected trade has unknown history; cannot construct a complete execution path")
                if not row["filled"] or not np.isfinite(row["score"]) or row["score"] < threshold:
                    continue
                if len(active) >= e["max_positions"] or any(p["symbol"] == row["symbol"] for p in active) or cash < row["capital"]:
                    continue
                cash -= row["capital"]
                row["log_index"] = len(accepted)
                active.append(row)
                accepted.append({"symbol": row["symbol"], "timestamp": row["timestamp"].isoformat(),
                                 "score": float(row["score"]), "net_pnl": None, "net_R": None,
                                 "closed": False, "ambiguous": False, "exit_at": None, "day": day})
        last_equity = current_equity
        equity_curve.append(current_equity)
    open_positions = []
    for p in active:
        index = bisect.bisect_right(p["path_times"], events[-1]) - 1
        pnl = float(p["path_pnl"][index]) if index >= 0 else -float(p["capital"]) * e["buy_fee_bps"] / 10000
        remaining = int(p["path_remaining"][index]) if index >= 0 else int(p["quantity"])
        ambiguous = bool(p["path_ambiguous"][index]) if index >= 0 else False
        accepted[p["log_index"]].update(net_pnl=pnl, ambiguous=ambiguous)
        open_positions.append({"symbol": p["symbol"], "remaining_quantity": remaining, "marked_net_pnl": pnl,
                               "valuation_at": pd.Timestamp(p["path_times"][index], tz="UTC").isoformat() if index >= 0 else None})
    final_equity = cash + sum(p["capital"] + accepted[p["log_index"]]["net_pnl"] for p in active)
    if prior_day:
        daily_pnl[prior_day] = final_equity - day_start
    daily = np.array([daily_pnl.get(d, 0.) for d in days])
    curve = np.array([e["initial_cash_krw"], *equity_curve, final_equity], dtype=float)
    peaks = np.maximum.accumulate(curve)
    closed = [t for t in accepted if t["closed"]]
    pnls = np.array([t["net_pnl"] for t in closed])
    return {"net_pnl_krw": final_equity - e["initial_cash_krw"], "return_pct": (final_equity / e["initial_cash_krw"] - 1) * 100,
            "available_cash_krw": cash, "equity_krw": final_equity,
            "realized_pnl_krw": float(pnls.sum()), "marked_open_pnl_krw": sum(p["marked_net_pnl"] for p in open_positions),
            "open_positions": open_positions, "closed_trades": len(closed),
            "max_drawdown_pct": float(np.min(curve / peaks - 1) * 100), "trades": len(accepted),
            "win_rate": float(np.mean(pnls > 0)) if len(pnls) else None,
            "mean_net_R": float(np.mean([t["net_R"] for t in closed])) if closed else None,
            "ambiguous_trades": sum(t["ambiguous"] for t in accepted),
            "daily_pnl": dict(zip(days, daily.tolist())), "trade_log": accepted}
