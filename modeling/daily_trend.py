"""Past-only daily macro trend and confirmed-pivot resistance-line breakouts."""
from __future__ import annotations

import numpy as np
import pandas as pd

PARAMETERS=dict(ma_days=60,slope_days=20,pivot_left=2,pivot_right=2,
                anchor_window=60,anchor_min_separation=5,fresh_days=5,hold_days=20)
COHORTS={"baseline":"기존 전일 선정", "macro":"큰 상승 추세",
         "macro_break20":"큰 상승 추세 + 돌파 유지20일", "macro_break5":"큰 상승 추세 + 최근5일 돌파"}


def context(frame):
    f=frame.sort_values("timestamp").reset_index(drop=True)
    if f.empty or f.timestamp.duplicated().any():raise ValueError("Need unique daily timestamps")
    prices=f[["open","high","low","close"]].to_numpy(float)
    if not np.isfinite(prices).all() or (prices<=0).any():raise ValueError("Invalid daily prices")
    dates=pd.to_datetime(f.timestamp,utc=True).dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")
    ma=f.close.rolling(PARAMETERS["ma_days"]).mean()
    slope=ma/ma.shift(PARAMETERS["slope_days"])-1
    macro=f.close.gt(ma)&slope.gt(0)
    high,close=[f[name].to_numpy(float) for name in ("high","close")]
    highs=[];event=None;rows=[]
    right=PARAMETERS["pivot_right"];left=PARAMETERS["pivot_left"]
    for t in range(len(f)):
        # Anchors must already be confirmed at D-1; D confirms the closing breakout.
        anchors=highs[-2:] if len(highs)>=2 else []
        line=None;cross=False
        if anchors:
            (a,ha),(b,hb)=anchors
            if t-a<=PARAMETERS["anchor_window"] and b-a>=PARAMETERS["anchor_min_separation"] and hb<ha:
                m=(hb-ha)/(b-a);now=hb+m*(t-b);prior=hb+m*(t-1-b)
                xs=np.arange(b,t)
                respected=bool((close[b:t]<=hb+m*(xs-b)).all())
                if now>0:
                    line=float(now)
                    cross=bool(t>0 and respected and close[t-1]<=prior and close[t]>now)
                    if cross:event=dict(at=t,a=a,b=b,ha=float(ha),hb=float(hb),slope=float(m))
        if event is not None:
            value=event["hb"]+event["slope"]*(t-event["b"])
            if value<=0 or close[t]<value or t-event["at"]>=PARAMETERS["hold_days"]:event=None
        age=t-event["at"] if event else None
        record=dict(selection_day=dates.iloc[t],macro=bool(macro.iloc[t]),ma60=float(ma.iloc[t]) if pd.notna(ma.iloc[t]) else None,
            ma60_slope20=float(slope.iloc[t]) if pd.notna(slope.iloc[t]) else None,
            candidate_line=line,breakout_today=cross,breakout_age=age,
            breakout_day=dates.iloc[event["at"]] if event else None,
            anchor1_day=dates.iloc[event["a"]] if event else None,
            anchor2_day=dates.iloc[event["b"]] if event else None,
            anchor1_high=event["ha"] if event else None,anchor2_high=event["hb"] if event else None,
            active_line=event["hb"]+event["slope"]*(t-event["b"]) if event else None,
            macro_break20=bool(macro.iloc[t] and event is not None),
            macro_break5=bool(macro.iloc[t] and age is not None and age<PARAMETERS["fresh_days"]))
        rows.append(record)
        j=t-right
        if j>=left:
            if high[j]>high[j-left:j].max() and high[j]>=high[j+1:t+1].max():highs.append((j,high[j]))
        highs=[v for v in highs if t-v[0]<=PARAMETERS["anchor_window"]]
    result=pd.DataFrame(rows)
    for name in ("ma60","ma60_slope20","candidate_line","breakout_age","anchor1_high","anchor2_high","active_line"):
        result[name]=pd.to_numeric(result[name]).astype(float)
    result["baseline"]=True
    return result
