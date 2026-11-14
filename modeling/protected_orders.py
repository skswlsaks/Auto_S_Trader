"""Versioned +0.5% system BE protection; fixed +3%/+5% partial orders."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

from .daytrade import RULES
from .execution import sell_tax_bps
from .trade_accuracy import TOLERANCE

BE_GAIN=.005

# shortcut: frozen baseline source is preserved; unify versioned engines when retiring that baseline.
def partial_trade(future,entry,execution,bearish,model_enabled=True,activation_delay=False):
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
    armed_at,armed_from_open,pending_arm = None,False,False
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
            activation = max(entry["entry_price"]*(1+BE_GAIN),be_raw)
            if not armed and (pending_arm or (not activation_delay and bar.open>=activation)):
                armed=True;armed_at=bar.timestamp.isoformat();armed_from_open=True
            stop = max(entry["stop_price"],be_raw) if armed else entry["stop_price"]
            target_touch = any(bar.high*(1-impact)>=entry["entry_price"]*(1+t) and sold_targets[k]<desired[k]
                               for k,t in enumerate(RULES["targets"]))
            if bar.low<=stop:
                ambiguous |= target_touch or (not armed and bar.high>=activation)
                forced = "breakeven_stop" if armed else "structural_stop"
                be_observed |= armed
                sell(left,min(bar.open,stop)*(1-impact),forced)
            else:
                if not armed and bar.high>=activation:
                    if activation_delay:pending_arm=True
                    else:
                        armed=True;armed_at=bar.timestamp.isoformat()
                        if bar.low<=be_raw:
                            ambiguous=True;be_observed=True;forced="breakeven_same_bar_ambiguous"
                            sell(left,be_raw*(1-impact),forced)
                for k,gain in enumerate(RULES["targets"]) if forced is None else ():
                    limit = entry["entry_price"]*(1+gain)
                    if bar.high*(1-impact)>=limit:
                        sold = sell(desired[k]-sold_targets[k],max(bar.open*(1-impact),limit),f"tp{round(gain*100)}")
                        sold_targets[k] += sold
                        if sold and first_tp is None:first_tp=bar.timestamp.isoformat()
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
        be_armed_at=armed_at,be_armed_from_open=armed_from_open,be_activation_gain=BE_GAIN,
        be_activation_delay_bars=int(activation_delay),
        ambiguous=bool(ambiguous),first_tp=first_tp,exit_reason=forced or exit_reason,
        exit_bar_end=bar.timestamp.isoformat() if not left else None,
        holding_bars=i+1,fills=fills)


def categorize(frame):
    """BE is a successful exit only when all shares are sold and aggregate net >=0."""
    f=frame.copy()
    done=f.complete.astype(bool)
    net=pd.to_numeric(f.realized_net_return,errors="raise")
    if not np.isfinite(net.loc[done]).all():raise ValueError("Missing realized return")
    if not done.eq(f.remaining_quantity.eq(0)).all():raise ValueError("Inconsistent completion")
    f["be_fill"]=f.fills.map(lambda values:any(v["reason"].startswith("breakeven") and v["quantity"]>0 for v in values))
    success=done & net.ge(-TOLERANCE)
    f["outcome_category"]=np.select([~done,done & ~success,success & f.be_fill,success & net.abs().le(TOLERANCE)],
        ["unclosed","loss","rise_then_breakeven_success","other_flat_success"],default="profit_success")
    f["success"]=success
    f["be_protection_failed"]=f.be_fill & ~success
    f["exact_flat"]=done & net.abs().le(TOLERANCE)
    return f


def category_counts(frame):
    if frame.empty:
        return dict(entries=0,profit_success=0,rise_then_breakeven_success=0,other_flat_success=0,
            loss=0,unclosed=0,exact_flat=0,be_protection_failed=0,successes=0,accuracy=None)
    f=categorize(frame)
    counts=f.outcome_category.value_counts().to_dict()
    assert sum(counts.values())==len(f)
    return dict(entries=len(f),profit_success=int(counts.get("profit_success",0)),
        rise_then_breakeven_success=int(counts.get("rise_then_breakeven_success",0)),
        other_flat_success=int(counts.get("other_flat_success",0)),loss=int(counts.get("loss",0)),
        unclosed=int(counts.get("unclosed",0)),exact_flat=int(f.exact_flat.sum()),
        be_protection_failed=int(f.be_protection_failed.sum()),successes=int(f.success.sum()),
        accuracy=float(f.success.mean()) if len(f) else None)
