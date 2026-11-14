"""Fixed 3%/5% partial orders and break-even protection; shape-only bearish exit."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .execution import sell_tax_bps
from .shape_v2 import detect

RULES = dict(version="daytrade-fixed-orders-v1",targets=[.03,.05],fractions=[.35,.35,.30],
             entry_windows=[[480,630],[780,870]],max_initial_price_risk=.01,
             end_exit_observation=919,min_quantity=3,
             breakeven="Remaining shares' own cost-inclusive break-even, armed after first actual TP fill",
             exit_model="Bearish shape only, no profit prediction or learned TP/BE decision")


def session_bars(frame):
    f = frame.copy().sort_values("timestamp").reset_index(drop=True)
    f["timestamp"] = pd.to_datetime(f.timestamp,utc=True)
    if f.timestamp.duplicated().any():raise ValueError("Duplicate bars")
    local = f.timestamp.dt.tz_convert("Asia/Seoul")
    minute = local.dt.hour*60+local.dt.minute
    f = f.loc[minute.between(481,530)|minute.between(541,920)].copy()
    local = f.timestamp.dt.tz_convert("Asia/Seoul")
    f["day"],f["minute"] = local.dt.strftime("%Y-%m-%d"),local.dt.hour*60+local.dt.minute
    f["segment"] = (f.timestamp.diff().ne(pd.Timedelta(minutes=1))|f.day.ne(f.day.shift())).cumsum()
    return f.reset_index(drop=True)


def entry_time_allowed(timestamp):
    t = pd.Timestamp(timestamp).tz_convert("Asia/Seoul")
    minute = t.hour*60+t.minute
    return any(start<=minute<=end for start,end in RULES["entry_windows"])


def mirror_bars(frame):
    """A fixed reciprocal transform is causal and maps bearish structure to bullish."""
    f = frame.copy()
    for col,source in (("open","open"),("high","low"),("low","high"),("close","close")):
        f[col] = 1e8/frame[source]
    return f


def bearish_structure(frame,symbol):
    # This is a mirrored structural baseline, not a model trained on bearish labels.
    return detect(mirror_bars(frame),symbol+"-bearish")


def make_entry(frame,index,execution):
    signal = frame.iloc[index]
    base = dict(filled=False,signal_at=signal.timestamp.isoformat())
    if index+1>=len(frame):return {**base,"status":"no_next_bar"}
    next_bar = frame.iloc[index+1]
    if next_bar.timestamp!=signal.timestamp+pd.Timedelta(minutes=1):
        return {**base,"status":"stale_signal_at_gap"}
    entry_at = next_bar.timestamp-pd.Timedelta(minutes=1)
    if not entry_time_allowed(entry_at):return {**base,"status":"outside_entry_window"}
    impact = (execution["slippage_bps"]+execution["half_spread_bps"])/10000
    fee = execution["buy_fee_bps"]/10000
    price = float(next_bar.open)*(1+impact)
    support = signal.pullback_low if pd.notna(signal.pullback_low) else signal.recent_low5
    stop = float(support-.1*signal.atr_pct*signal.close)
    risk = (price-stop)/price
    if not np.isfinite(risk) or not 0<risk<=RULES["max_initial_price_risk"]:
        return {**base,"status":"stop_risk_rejected"}
    if not np.isfinite(signal.recent_volume_mean5):return {**base,"status":"unknown_recent_volume"}
    planned = min(math.floor(execution["max_position_krw"]/(signal.close*(1+impact)*(1+fee))),
                  math.floor(signal.recent_volume_mean5*execution["participation"]))
    quantity = min(planned,math.floor(next_bar.volume*execution["participation"]),
                   math.floor(execution["max_position_krw"]/(price*(1+fee))))
    if quantity<RULES["min_quantity"]:
        return {**base,"status":"insufficient_partial_quantity","quantity":quantity}
    return {**base,"filled":True,"status":"entered","entry_at":entry_at.isoformat(),
            "entry_bar_end":next_bar.timestamp.isoformat(),"entry_price":price,"stop_price":stop,
            "initial_risk_pct":risk,"quantity":quantity,"planned_quantity":planned,"capital":quantity*price*(1+fee)}


def partial_trade(future,entry,execution,bearish,model_enabled=True):
    """Resting price targets/stop are system rules; only bearish exits wait for a close.

    Minute OHLC cannot recover order queue or high/low sequence. Stop wins a
    same-bar conflict; a newly armed BE touched in that bar is conservatively hit.
    Limit fills require impacted market high >= limit; target touch is not latched
    as a later market sell. One minute's volume budget is shared by all fills.
    """
    if len(future)!=len(bearish) or future.empty:raise ValueError("Need aligned future signals")
    if future.day.nunique()!=1:raise ValueError("Intraday-only policy")
    if future.timestamp.iloc[0]!=pd.Timestamp(entry["entry_bar_end"]):raise ValueError("Entry mismatch")
    if not entry["filled"] or entry["quantity"]<3:raise ValueError("Need three nonzero tranches")
    gaps = future.timestamp.diff().iloc[1:]
    valid = gaps.eq(pd.Timedelta(minutes=1)) | (future.minute.eq(541)&future.minute.shift().eq(530)).iloc[1:]
    if not valid.all():raise ValueError("Unknown intraday gap")
    impact = (execution["slippage_bps"]+execution["half_spread_bps"])/10000
    q,capital = entry["quantity"],entry["capital"]
    desired = [math.floor(q*.35),math.floor(q*.70)-math.floor(q*.35)]
    sold_targets = [0,0]
    left,proceeds,armed,forced = q,0.,False,None
    fills,ambiguous,be_observed,bear_observed = [],False,False,False
    exit_reason,first_tp = None,None
    for i,bar in enumerate(future.itertuples(index=False)):
        fee = (execution["sell_fee_bps"]+sell_tax_bps(execution,bar.timestamp))/10000
        capacity = math.floor(bar.volume*execution["participation"])
        if i==0:capacity=max(0,capacity-q)
        def sell(wanted,price,reason):
            nonlocal left,proceeds,capacity,exit_reason
            amount = min(left,wanted,capacity)
            if amount:
                proceeds += amount*price*(1-fee)
                left -= amount;capacity -= amount;exit_reason = reason
                fills.append(dict(bar_end=bar.timestamp.isoformat(),quantity=amount,price=float(price),reason=reason))
            return amount
        if forced is not None:
            sell(left,bar.open*(1-impact),forced)
        if left and forced is None:
            be_raw = entry["capital"]/q/((1-impact)*(1-fee))
            stop = max(entry["stop_price"],be_raw) if armed else entry["stop_price"]
            target_touch = any(bar.high*(1-impact)>=entry["entry_price"]*(1+t) and sold_targets[k]<desired[k]
                               for k,t in enumerate(RULES["targets"]))
            if bar.low<=stop:
                ambiguous |= target_touch
                forced = "breakeven_stop" if armed else "structural_stop"
                be_observed |= armed
                sell(left,min(bar.open,stop)*(1-impact),forced)
            else:
                for k,gain in enumerate(RULES["targets"]):
                    limit = entry["entry_price"]*(1+gain)
                    if bar.high*(1-impact)>=limit:
                        sold = sell(desired[k]-sold_targets[k],max(bar.open*(1-impact),limit),f"tp{round(gain*100)}")
                        sold_targets[k] += sold
                        if sold and first_tp is None:first_tp=bar.timestamp.isoformat()
                        if sold_targets[0]>0 and not armed:
                            armed=True
                            if left and bar.low<=be_raw:
                                ambiguous=True;be_observed=True;forced="breakeven_same_bar_ambiguous"
                                # The earlier open is not available after TP. Do not award TP5 first.
                                sell(left,be_raw*(1-impact),forced)
                                break
        if left and forced is None and model_enabled and bool(bearish[i]):
            forced="bearish_structure";bear_observed=True
        if left and forced is None and bar.minute>=RULES["end_exit_observation"]:
            forced="session_end"
        if not left:break
    fee = (execution["sell_fee_bps"]+sell_tax_bps(execution,bar.timestamp))/10000
    marked = (proceeds+left*bar.close*(1-impact)*(1-fee))/capital-1
    net = proceeds/capital-1 if not left else None
    return dict(complete=not bool(left),remaining_quantity=left,realized_net_return=net,
        marked_net_return=float(marked),net_nonnegative=bool(net is not None and net>=-1e-12),
        net_small_profit=bool(net is not None and net>=.001-1e-12),capital=capital,
        quantity=q,tp3_quantity=sold_targets[0],tp5_quantity=sold_targets[1],
        tp3_complete=sold_targets[0]==desired[0],tp5_complete=sold_targets[1]==desired[1],
        be_armed=armed,be_observed=be_observed,bear_observed=bear_observed,
        ambiguous=bool(ambiguous),first_tp=first_tp,exit_reason=forced or exit_reason,
        exit_bar_end=bar.timestamp.isoformat() if not left else None,
        holding_bars=i+1,fills=fills)
