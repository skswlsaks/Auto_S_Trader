"""Step 2b: does the instructor's post itself move the price (follower buying)?

Research only. Natural experiment: posts made after 08:00 on D leave NXT pre-market bars (08:00-08:50)
before the post. Split the S-close -> D 09:01 open gap into a pre-post part and a post part.
Fixed before results: windows and measures below; no pass bar (diagnostic).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import entry_gate_study as gate
from . import premarket_entry_study as pm
from .config import atomic_json
from .daytrade import session_bars
from .pattern_eval import sha

OUT = gate.MR/"post-impact/20261115-v1"
PROTOCOL = dict(
    version="post-impact-v1",
    sample="instructor picks (old+new CSV), kind=pre, post on D between 08:05 and 08:59 KST, D minute file with >=1 traded "
           "pre-market bar ending before the post and a complete regular session",
    measures=dict(pre_px="close of the last traded pre-market bar whose bar end <= post minute",
                  post_px="open of the first traded pre-market bar whose bar end >= post minute + 2 (if any before 08:50)",
                  open_px="D 09:01 bar open", s_close="S close from features (Naver, rescaled if 1m scale differs >2%)",
                  pre_gap="pre_px / S close - 1", post_jump="post_px / pre_px - 1", rest="open_px / pre_px - 1",
                  vol="pre-market turnover per minute before vs after the post"),
    groups=dict(early="post 08:05-08:49 (pre-market continues after the post)", late="post 08:50-08:59 (after pre-market close)"),
)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    post = pm.post_times().reset_index()
    post = post[(post.kind == "pre") & (post.post_ts.dt.strftime("%Y-%m-%d") == post.D)]
    post["pmin"] = post.post_ts.dt.hour*60+post.post_ts.dt.minute
    post = post[(post.pmin >= 485) & (post.pmin <= 539)]
    f = pd.read_parquet(gate.MR/"closing-narrow/20261114-v1/features.parquet", columns=["code", "S", "D", "close", "Do", "risk"])
    f["Dk"] = pd.to_datetime(f.D).dt.strftime("%Y-%m-%d")
    post = post.merge(f, left_on=["code", "D"], right_on=["code", "Dk"], suffixes=("", "_f"))
    rows = []
    for r in post.itertuples(index=False):
        p = gate.day_path(r.code, r.D)
        if p is None:
            continue
        b = session_bars(pd.read_parquet(p))
        m, o, c, v = b.minute.to_numpy(), b.open.to_numpy(float), b.close.to_numpy(float), b.volume.to_numpy(float)
        reg = np.flatnonzero(m == 541)
        if not len(reg):
            continue
        open_px = o[reg[0]]
        ratio = open_px/r.Do if r.Do > 0 else np.nan
        s_close = r.close*ratio if np.isfinite(ratio) and abs(ratio-1) > .02 else r.close
        pre = np.flatnonzero((m >= 481) & (m <= min(r.pmin, 530)) & (v > 0))
        if not len(pre):
            continue
        pre_px = c[pre[-1]]
        aft = np.flatnonzero((m >= r.pmin+2) & (m <= 530) & (v > 0))
        post_px = o[aft[0]] if len(aft) else np.nan
        win_b = (m >= 481) & (m <= min(r.pmin, 530))
        win_a = (m >= r.pmin+1) & (m <= 530)
        tb = (c[win_b]*v[win_b]).sum()/max(1, win_b.sum())
        ta = (c[win_a]*v[win_a]).sum()/max(1, win_a.sum()) if win_a.any() else np.nan
        rows.append(dict(code=r.code, D=r.D, post=str(r.post_ts), pmin=r.pmin, risk=r.risk,
                         group="late" if r.pmin >= 530 else "early", s_close=s_close, pre_px=pre_px, post_px=post_px,
                         open_px=open_px, pre_gap=pre_px/s_close-1, post_jump=post_px/pre_px-1, rest=open_px/pre_px-1,
                         open_gap=open_px/s_close-1, tv_before=tb, tv_after=ta))
    t = pd.DataFrame(rows)
    t.to_parquet(OUT/"rows.parquet", index=False)
    summ = {}
    for g, d in [("all", t), *t.groupby("group")]:
        summ[g] = dict(n=len(d), days=int(d.D.nunique()),
                       **{f"{k}_mean": round(float(d[k].mean())*100, 2) for k in ("pre_gap", "post_jump", "rest", "open_gap")},
                       **{f"{k}_median": round(float(d[k].median())*100, 2) for k in ("pre_gap", "post_jump", "rest", "open_gap")},
                       share_pre_gap_pos=round(float((d.pre_gap > 0).mean()), 2),
                       tv_ratio_after_before_median=round(float((d.tv_after/d.tv_before).median()), 2) if d.tv_after.notna().any() else None)
    atomic_json(OUT/"summary.json", summ)
    print(json.dumps(summ, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
