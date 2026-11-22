"""Step 1b: stock momentum kept + multi-week sector relative strength, on our closing scan.

Research only. Follows the user's hypothesis (2026-11-15): what matters is whether the stock's own
uptrend is intact, plus a sector that is outperforming the market over weeks (e.g. semis Sep-Oct).
Definitions are generic and fixed in PROTOCOL before results; they are NOT tuned to the semis example.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from . import theme_momentum_study as tm
from .config import atomic_json
from .pattern_eval import sha

OUT = tm.MR/"momentum-trend/20261115-v1"
PROTOCOL = dict(
    version="momentum-trend-v1",
    data="closing-narrow-v1 features + theme-momentum-v1 themes.json (current membership)",
    stock_momentum=dict(SM="r20 > 0 AND close > MA20 (d20 > 0) AND MA20 rising vs 5 sessions ago AND close >= 0.90 x 60-day high"),
    sector_trend=dict(th20="max over the stock's themes of the median 20-day return of OTHER members (>= 5 others)",
                      th20_rel="th20 - market median 20-day return that day (all stocks)",
                      th_above="share of other members of that best theme with close > MA20",
                      TT="th20_rel > 0 AND th_above >= 0.5"),
    sets=dict(SM="R1 and SM", SM_TT="R1 and SM and TT", SM_TT_S1="SM_TT top 3 per day by S1 (instructor similarity)",
              SM_TT_TOP="SM_TT top 3 per day by th20_rel"),
    references=["R1", "PICKS_F"],
    outcomes=tm.PROTOCOL["outcomes"], periods=tm.PROTOCOL["periods"], pass_bar=tm.PROTOCOL["pass_bar"],
    diagnostics="R1 eval: mean gap / O2 by quintile of r20, hd60, th20_rel; picks' within-day percentile on each",
    caveat=tm.PROTOCOL["caveat"])


def sector_scores(f, th):
    mkt = f.groupby("S").r20.median().rename("mkt20")
    x = f[["code", "S", "r20", "d20"]].merge(th[["theme", "code"]], on="code")
    x = x[np.isfinite(x.r20) & np.isfinite(x.d20)]
    g = x.groupby(["theme", "S"])
    x["n"] = g.r20.transform("size")
    x = x[x.n >= 6].copy()
    g = x.groupby(["theme", "S"])
    above = (x.d20 > 0).astype(float)
    x["above_lo"] = (g.d20.transform(lambda s: (s > 0).sum()) - above)/(x.n-1)

    def loo(s):
        srt = np.sort(s.to_numpy())
        return pd.Series([np.median(np.delete(srt, np.searchsorted(srt, v))) for v in s.to_numpy()], index=s.index)
    x["med_lo"] = g.r20.transform(loo)
    best = x.sort_values("med_lo", ascending=False).drop_duplicates(["code", "S"])
    best = best[["code", "S", "theme", "med_lo", "above_lo"]].rename(
        columns=dict(theme="best_theme", med_lo="th20", above_lo="th_above"))
    best = best.merge(mkt, left_on="S", right_index=True)
    best["th20_rel"] = best.th20 - best.mkt20
    return best


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    f = pd.read_parquet(tm.SRC/"features.parquet")
    th = tm.load_themes()
    f = f.merge(sector_scores(f, th), on=["code", "S"], how="left")
    f["SM"] = (f.r20 > 0) & (f.d20 > 0) & (f.ma20slope > 0) & (f.hd60 >= -.10)
    f["TT"] = (f.th20_rel > 0) & (f.th_above >= .5)
    r1 = f[f.R1]
    smtt = r1[r1.SM & r1.TT]
    sets = dict(R1=r1, PICKS_F=f[f.pick & ~f.risk], SM=r1[r1.SM], SM_TT=smtt,
                SM_TT_S1=tm.topk(smtt, "S1"), SM_TT_TOP=tm.topk(smtt, "th20_rel"))
    per = lambda S: np.where(S <= "20260630", "train", np.where(S <= "20261027", "eval", "late"))
    rows, sel = [], []
    for name, d in sets.items():
        d = d.assign(period=per(d.S), set=name)
        sel.append(d)
        for p in ("train", "eval", "late"):
            g = d[d.period == p]
            row = dict(set=name, period=p, days=int(g.S.nunique()), n=len(g), picks_in=int(g.pick.sum()),
                       pick_share=float(g.pick.mean()) if len(g) else None)
            for o in ("gap", "O1", "O2"):
                st = cn.stats(g[o], g.S)
                row.update({f"{o}_mean": st.get("mean"), f"{o}_ci": st.get("ci")})
            rows.append(row)
        if name in ("SM_TT", "SM_TT_S1", "SM_TT_TOP"):
            e = d[d.period == "eval"]
            for m, g in e.groupby(e.S.str[:6]):
                rows.append(dict(set=name, period=f"eval-{m}", days=int(g.S.nunique()), n=len(g),
                                 picks_in=int(g.pick.sum()), gap_mean=g.gap.mean(), O2_mean=g.O2.mean(),
                                 top_themes=g.best_theme.map(dict(zip(th.theme, th.tname))).value_counts().head(3).to_dict()))
    res = pd.DataFrame(rows)
    pd.concat(sel, ignore_index=True).to_parquet(OUT/"selections.parquet", index=False)
    # diagnostics on R1
    diag = {}
    for p in ("train", "eval"):
        e = r1[per(r1.S) == p].copy()
        dd = {}
        for c in ("r20", "hd60", "th20_rel"):
            q = e.groupby("S")[c].rank(pct=True)
            b = pd.cut(q, [0, .2, .4, .6, .8, 1], labels=False, include_lowest=True)
            dd[c] = dict(gap_by_quintile=[round(float(v)*100, 2) for v in e.groupby(b).gap.mean()],
                         O2_by_quintile=[round(float(v)*100, 2) for v in e.groupby(b).O2.mean()],
                         pick_pct=round(float(q[e.pick].median()), 3))
        dd["SM_share"] = dict(picks=round(float(e[e.pick].SM.mean()), 3), others=round(float(e[~e.pick].SM.mean()), 3))
        dd["TT_share"] = dict(picks=round(float(e[e.pick].TT.mean()), 3), others=round(float(e[~e.pick].TT.mean()), 3))
        diag[p] = dd
    res.to_csv(OUT/"summary.csv", index=False)
    atomic_json(OUT/"summary.json", dict(results=json.loads(res.to_json(orient="records")), diagnostics=diag))
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 80)
    show = res.copy()
    for c in ("gap_mean", "O1_mean", "O2_mean", "pick_share"):
        show[c] = (show[c]*100).round(2)
    for c in ("gap_ci", "O2_ci", "O1_ci"):
        show[c] = show[c].map(lambda v: f"[{v[0]*100:+.2f},{v[1]*100:+.2f}]" if isinstance(v, list) else "")
    print(show.drop(columns=["O1_ci"]).to_string(index=False))
    print(json.dumps(diag, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
