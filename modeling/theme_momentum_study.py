"""Step 1: does S-day theme co-movement help our closing scan pick instructor-like next-day gaps?

Research only. Reuses closing-narrow-v1 features (all stocks, Naver daily) and adds Naver theme
membership (current, cursor-paginated). Sets, outcomes and the pass bar are fixed in PROTOCOL
before any result is read.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from .config import atomic_json
from .pattern_eval import sha

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
SRC = MR/"closing-narrow/20261114-v1"
OUT = MR/"theme-momentum/20261115-v1"
PROTOCOL = dict(
    version="theme-momentum-v1",
    data=dict(features="closing-narrow-v1 features.parquet (2,654 stocks, S >= 2026-03-10)",
              themes="themes.json: Naver theme stocklist, cursor-paginated, fetched 2026-11-15 (current membership, "
                     "survivorship + look-ahead in membership)"),
    theme_day="per (theme, S): members with finite S-day return chg1; need >= 5 other members. "
              "Stock-level values exclude the stock itself",
    scores=dict(th_med="max over the stock's themes of the median S-day return of other members",
                th_breadth="max over themes of the share of other members with S-day return > +3%"),
    sets=dict(R1="closing-narrow R1 (scan C0, liquid, no risk flag) - reference",
              PICKS_F="instructor picks without risk flag - reference",
              T1="R1 top 3 per day by th_med",
              T2="R1 top 3 per day by th_breadth (tie: th_med)",
              TS="R1 top 3 per day by mean percentile rank of S1 (instructor similarity) and th_med",
              TH="R1 with th_med >= +2%, then top 3 by S1"),
    outcomes=dict(gap="D open / S close - 1", O1="closing-narrow O1 (upper bound)", O2="closing-narrow O2 (stop first)"),
    periods=dict(train="S <= 2026-06-30", eval="2026-07-01..2026-10-27 (decision)", late="S >= 2026-10-28 (report only)"),
    pass_bar="A set passes if on eval: mean gap >= +1.0% AND O2 mean > 0 with day-bootstrap 95% CI low > 0; "
             "and on train mean gap > +0.5%. Only passing sets go to paper trading. 4 candidate sets are tried, "
             "so a marginal pass is treated as exploratory.",
    caveat="S final close is used; a real 15:00 scan sees provisional prices",
)


def load_themes():
    t = json.loads((OUT/"themes.json").read_text())["themes"]
    return pd.DataFrame([(r["no"], r["name"], c) for r in t for c in r["members"]], columns=["theme", "tname", "code"])


def theme_scores(f, th):
    x = f[["code", "S", "chg1", "gap"]].merge(th[["theme", "code"]], on="code")
    x = x[np.isfinite(x.chg1)]
    g = x.groupby(["theme", "S"])
    x["n"] = g.chg1.transform("size")
    x = x[x.n >= 6].copy()  # >= 5 others
    g = x.groupby(["theme", "S"])
    up = (x.chg1 > .03).astype(float)
    x["breadth_lo"] = (g.chg1.transform(lambda s: (s > .03).sum()).reindex(x.index) - up)/(x.n-1)
    # leave-one-out median
    def loo(s):
        v = s.to_numpy()
        srt = np.sort(v)
        out = np.empty(len(v))
        for i, val in enumerate(v):
            k = np.searchsorted(srt, val)
            rest = np.delete(srt, k)
            out[i] = np.median(rest)
        return pd.Series(out, index=s.index)
    x["med_lo"] = x.groupby(["theme", "S"]).chg1.transform(loo)
    s = x.groupby(["code", "S"]).agg(th_med=("med_lo", "max"), th_breadth=("breadth_lo", "max"), n_themes=("theme", "size"))
    # theme-day diagnostic: S-day theme median return vs D-day theme median gap
    td = x.groupby(["theme", "S"]).agg(t_ret=("chg1", "median"), t_gap=("gap", "median"), n=("n", "first")).reset_index()
    return s.reset_index(), td


def topk(df, key, k=3):
    return df.sort_values(["S", key], ascending=[True, False]).groupby("S").head(k)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__)),
                                          themes_sha256=sha(OUT/"themes.json")))
    f = pd.read_parquet(SRC/"features.parquet")
    th = load_themes()
    s, td = theme_scores(f, th)
    f = f.merge(s, on=["code", "S"], how="left")
    r1 = f[f.R1].copy()
    r1["rk"] = (r1.groupby("S").S1.rank(pct=True) + r1.groupby("S").th_med.rank(pct=True))/2
    sets = dict(R1=r1, PICKS_F=f[f.pick & ~f.risk],
                T1=topk(r1.dropna(subset=["th_med"]), "th_med"),
                T2=topk(r1.dropna(subset=["th_med"]).assign(tb=lambda d: d.th_breadth + d.th_med*1e-3), "tb"),
                TS=topk(r1.dropna(subset=["rk"]), "rk"),
                TH=topk(r1[r1.th_med >= .02], "S1"))
    per = lambda S: np.where(S <= "20260630", "train", np.where(S <= "20261027", "eval", "late"))
    rows, sel = [], []
    for name, d in sets.items():
        d = d.assign(period=per(d.S), set=name)
        sel.append(d)
        for p in ("train", "eval", "late"):
            g = d[d.period == p]
            row = dict(set=name, period=p, days=int(g.S.nunique()), picks_in=int(g.pick.sum()),
                       pick_share=float(g.pick.mean()) if len(g) else None)
            for o in ("gap", "O1", "O2"):
                st = cn.stats(g[o], g.S)
                row.update({f"{o}_n": st.get("n"), f"{o}_mean": st.get("mean"), f"{o}_ci": st.get("ci")})
            rows.append(row)
    res = pd.DataFrame(rows)
    sel = pd.concat(sel, ignore_index=True)
    sel.to_parquet(OUT/"selections.parquet", index=False)
    # diagnostics
    td["period"] = per(td.S)
    diag = {}
    for p in ("train", "eval", "late"):
        g = td[td.period == p].dropna()
        q = pd.qcut(g.t_ret, 5, labels=False, duplicates="drop")
        diag[p] = dict(theme_days=len(g), spearman=float(g.t_ret.corr(g.t_gap, method="spearman")),
                       gap_by_ret_quintile=[float(v) for v in g.groupby(q).t_gap.mean()],
                       ret_quintile_edges=[float(v) for v in g.groupby(q).t_ret.min()])
    pk = f[f.R1].copy()
    pk["th_pct"] = pk.groupby("S").th_med.rank(pct=True)
    diag["pick_th_med_pct_in_R1"] = dict(picks=float(pk[pk.pick].th_pct.median()), others=float(pk[~pk.pick].th_pct.median()),
                                         coverage=float(f[f.R1].th_med.notna().mean()))
    res.to_csv(OUT/"summary.csv", index=False)
    atomic_json(OUT/"summary.json", dict(results=res.to_dict("records"), diagnostics=diag))
    pd.set_option("display.width", 250)
    show = res.copy()
    for c in [c for c in show if c.endswith("_mean") or c == "pick_share"]:
        show[c] = (show[c]*100).round(2)
    for c in [c for c in show if c.endswith("_ci")]:
        show[c] = show[c].map(lambda v: None if v is None else f"[{v[0]*100:+.2f},{v[1]*100:+.2f}]")
    print(show.to_string(index=False))
    print(json.dumps(diag, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
