from __future__ import annotations

import bisect
import math

import numpy as np
import pandas as pd


def outcome(bars, signal_at, stop_price, config, delay_bars=0, cost_multiplier=1.):
    """Conservative OHLC research fills, not reconstructed broker executions."""
    e = config["execution"]
    signal_at = pd.Timestamp(signal_at)
    future = bars[(bars.timestamp > signal_at) & (bars.timestamp.dt.tz_convert("Asia/Seoul").dt.date == signal_at.tz_convert("Asia/Seoul").date())].iloc[delay_bars:]
    horizon = e["max_holding_minutes"]
    base = {"filled": False, "label_status": "insufficient_future", "ambiguous": False}
    if len(future) < horizon:
        return base
    expected = signal_at + pd.Timedelta(minutes=1 + delay_bars)
    if future.timestamp.iloc[0] != expected or (future.timestamp.iloc[:horizon].diff().dropna() != pd.Timedelta(minutes=1)).any():
        return {**base, "label_status": "future_gap"}
    impact = (e["slippage_bps"] + e["half_spread_bps"]) / 10000 * cost_multiplier
    buy_fee = e["buy_fee_bps"] / 10000 * cost_multiplier
    sell_fee = (e["sell_fee_bps"] + e["sell_tax_bps"]) / 10000 * cost_multiplier
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
    path_times, path_pnl = [], []
    target_done = [False, False]
    exit_reason = "timeout"
    force_exit = False
    lows = []
    for bar_index, bar in enumerate(future.itertuples(index=False)):
        capacity = max(0, math.floor(bar.volume * e["participation"]))
        def sell(wanted, price):
            nonlocal left, proceeds, capacity
            amount = min(left, wanted, capacity)
            proceeds += amount * price * (1 - impact) * (1 - sell_fee)
            left -= amount
            capacity -= amount
        if force_exit and left:
            sell(left, bar.open)
        if left and bar.low <= stop:
            ambiguous |= bar.high >= entry * 1.03
            exit_reason = "stop"
            force_exit = True
            sell(left, min(bar.open, stop))
        if left and not force_exit:
            for k, (gain, fraction) in enumerate(((.03, .35), (.05, .70))):
                if bar.high >= entry * (1 + gain):
                    target = math.floor(quantity * fraction)
                    sell(max(0, target - (quantity - left)), entry * (1 + gain))
                    target_done[k] = quantity - left >= target
            if bar.high >= entry * (1 + e["breakeven_trigger_pct"]):
                # Break-even includes modeled round-trip costs; stop never decreases.
                breakeven = entry * (1 + buy_fee) / ((1 - impact) * (1 - sell_fee))
                if breakeven <= bar.high:
                    stop = max(stop, breakeven)
                    if left and bar.low <= stop:
                        ambiguous = True
                        exit_reason, force_exit = "breakeven_ambiguous", True
                        sell(left, min(bar.open, stop))
            lows.append(bar.low)
            if len(lows) >= 5 and lows[-3] < min(lows[-5:-3]) and lows[-3] < min(lows[-2:]):
                trend_low = max(trend_low, lows[-3])
            if bar.close < trend_low:
                exit_reason, force_exit = "trend_reversal", True
        if bar_index >= horizon - 1 and left:
            exit_reason, force_exit = "timeout", True
            sell(left, bar.close)
        marked = proceeds + left * bar.close * (1 - impact) * (1 - sell_fee) - capital
        path_times.append(int(bar.timestamp.value))
        path_pnl.append(float(marked))
        if not left:
            break
    if left:
        # Do not invent a full exit when the last candle cannot absorb the order.
        final = future.iloc[len(path_times) - 1]
        if capacity < left:
            return {**base, "label_status": "exit_unfilled", "ambiguous": ambiguous}
        sell(left, final.close)
        if left:
            return {**base, "label_status": "exit_unfilled", "ambiguous": ambiguous}
        path_pnl[-1] = proceeds - capital
    pnl = proceeds - capital
    return {"filled": True, "label_status": "complete", "ambiguous": bool(ambiguous),
            "entry_at": future.timestamp.iloc[0] - pd.Timedelta(minutes=1) + pd.Timedelta(microseconds=1),
            "exit_at": pd.Timestamp(path_times[-1], tz="UTC"), "entry_price": entry,
            "quantity": quantity, "capital": capital, "net_pnl": pnl,
            "net_R": pnl / (quantity * risk), "net_return": pnl / capital,
            "exit_reason": exit_reason, "path_times": path_times, "path_pnl": path_pnl}


def portfolio(candidates, scores, threshold, config, all_days=None):
    """Chronological, cash/slot constrained replay of fixed candidate outcomes."""
    e = config["execution"]
    work = candidates.copy()
    work["score"] = np.asarray(scores)
    work = work.sort_values(["timestamp", "score", "symbol"], ascending=[True, False, True])
    days = sorted(all_days or work.day.unique().tolist())
    cash = float(e["initial_cash_krw"])
    active, accepted, equity_curve = [], [], []
    daily_pnl = {}
    prior_day, day_start, last_equity, halted = None, cash, cash, False
    events = sorted(set(int(t.value) for t in work.timestamp) | {int(t) for p in work.get("path_times", []) for t in p})
    groups = {int(t.value): g for t, g in work.groupby("timestamp", sort=False)}
    for now in events:
        day = pd.Timestamp(now, tz="UTC").tz_convert("Asia/Seoul").strftime("%Y-%m-%d")
        if day != prior_day:
            if prior_day is not None:
                daily_pnl[prior_day] = last_equity - day_start
            day_start, prior_day, halted = last_equity, day, False
        remaining = []
        for p in active:
            if int(p["exit_at"].value) <= now:
                cash += p["capital"] + p["net_pnl"]
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
                if row["label_status"] == "exit_unfilled" and np.isfinite(row["score"]) and row["score"] >= threshold:
                    raise ValueError("Selected trade has unresolved exit; refusing a profitability report that drops residual positions")
                if not row["filled"] or not np.isfinite(row["score"]) or row["score"] < threshold:
                    continue
                if len(active) >= e["max_positions"] or any(p["symbol"] == row["symbol"] for p in active) or cash < row["capital"]:
                    continue
                cash -= row["capital"]
                active.append(row)
                accepted.append({"symbol": row["symbol"], "timestamp": row["timestamp"].isoformat(),
                                 "score": float(row["score"]), "net_pnl": float(row["net_pnl"]), "net_R": float(row["net_R"]),
                                 "ambiguous": bool(row["ambiguous"]), "day": day})
        last_equity = current_equity
        equity_curve.append(current_equity)
    if active:
        raise ValueError("Replay ended with unprocessed positions")
    if prior_day:
        daily_pnl[prior_day] = cash - day_start
    daily = np.array([daily_pnl.get(d, 0.) for d in days])
    curve = np.array([e["initial_cash_krw"], *equity_curve, cash], dtype=float)
    peaks = np.maximum.accumulate(curve)
    pnls = np.array([t["net_pnl"] for t in accepted])
    return {"net_pnl_krw": cash - e["initial_cash_krw"], "return_pct": (cash / e["initial_cash_krw"] - 1) * 100,
            "max_drawdown_pct": float(np.min(curve / peaks - 1) * 100), "trades": len(accepted),
            "win_rate": float(np.mean(pnls > 0)) if len(pnls) else None,
            "mean_net_R": float(np.mean([t["net_R"] for t in accepted])) if accepted else None,
            "ambiguous_trades": sum(t["ambiguous"] for t in accepted),
            "daily_pnl": dict(zip(days, daily.tolist())), "trade_log": accepted}
