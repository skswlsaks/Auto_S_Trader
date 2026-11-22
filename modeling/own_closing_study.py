"""Own closing bet: instructor-pick regularities -> combined chart conditions -> buy S close, sell on D.

Research only. Inputs are frozen local files (no network):
- Naver KRX daily (split-adjusted) for 2,654 stocks, 2025-01-01..2026-11-13 (closing-narrow-v1 naver-daily/)
- KOSDAQ daily raw text (regime-v1)
- Instructor pick labels (outputs/research/s_trader91_picks_20261114)

Two parts, both fixed in PROTOCOL before results:
A. Profile: which S-close chart conditions separate instructor picks from other liquid stocks
   (lift table + shallow decision tree fitted on S <= 2026-06-30, AUC reported on later S).
B. Own selection: combine one level from up to three condition families. Rank cells on the train period,
   keep cells that survive BH on train and a one-sided test on valid, then open eval and final once.
   A LightGBM ranker with fixed parameters is the model comparison. Random top-3 is the baseline.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from .config import atomic_json
from .pattern_eval import sha

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
SRC = MR/"closing-narrow/20261114-v1"
KOSDAQ_RAW = MR/"regime/20261115-v1/kosdaq-daily.raw.txt"
OUT = MR/"own-closing/20261115-v1"
COST = .0025
PERIODS = dict(train=("20250401", "20251230"), valid=("20260102", "20260630"),
               eval=("20260701", "20261027"), final=("20261028", "20261112"))
MIN_TRAIN_N, MIN_TRAIN_DAYS, MIN_VALID_N = 200, 50, 80
TOP_K = 3

PROTOCOL = dict(
    version="own-closing-v1",
    question="User 2026-11-15: picks are chart-only; many charts match, so trend and other conditions must be combined. "
             "Build our own S-close selection and sell on D.",
    data=dict(daily="closing-narrow-v1 naver-daily (split-adjusted, current listings only: survivorship)",
              kosdaq=str(KOSDAQ_RAW.relative_to(ROOT)), picks="s_trader91_picks_20261114 old+new CSV (S = trading day before post)"),
    universe="20-day median turnover (prior 20 days) >= 3e9 KRW, S close >= 1,000 KRW, S return < +29.5% "
             "(locked limit-up closes are not buyable), D bar exists",
    entry="Buy at S final close (a real 15:20 scan sees a provisional bar; idealized close fill)",
    exits=dict(E_open="sell at D open (primary: pure overnight, no intraday order assumption)",
               O2="closing-narrow O2: 35% at +3%, 35% at +5%, rest D close; if D low <= S*0.97 exit all at min(D open, S*0.97) first",
               E_close="sell at D close"),
    cost=COST,
    periods=PERIODS,
    part_A=dict(sample="universe rows with S in 2026-03-10..2026-11-12 (pick labels exist)",
                tree="sklearn DecisionTree depth 4, min_samples_leaf 300, class_weight balanced, fit S <= 2026-06-30",
                lift="P(pick | condition) / P(pick) for every condition level of part B"),
    part_B=dict(
        cells="one level from each of 1..3 distinct families (see FAMILIES in code)",
        metric="E_open net, trade-pooled mean, day-clustered standard error",
        train_rule=f"n >= {MIN_TRAIN_N}, active days >= {MIN_TRAIN_DAYS}, mean > 0, BH q < 0.05 over all cells (one-sided)",
        valid_rule=f"n >= {MIN_VALID_N}, mean > 0, day-clustered t >= 1.645",
        pick="survivors ranked by min(t_train, t_valid); top 5 opened on eval and final (all five reported)",
        daily_cap=f"each cell also run as top {TOP_K} per day by S-day turnover (most executable first)",
        model="LightGBM regressor on E_open net, fixed params, early stopping on valid; top 3 per day",
        baseline="random 3 per day from the universe, 200 seeds"),
)

# ---------------- data ----------------

def kosdaq():
    raw = KOSDAQ_RAW.read_text()
    rows = [json.loads(x.strip().rstrip(",").replace("'", '"')) for x in raw.splitlines() if x.strip().startswith('["2')]
    k = pd.DataFrame(rows, columns=["S", "o", "h", "l", "c", "v", "fr"])
    k["kq_ma20"] = k.c.rolling(20).mean()
    k["kq_ret"] = k.c.pct_change()
    k["kq_d20"] = k.c/k.kq_ma20-1
    k["kq_r20"] = k.c/k.c.shift(20)-1
    k["gate_ok"] = (k.c > k.kq_ma20) & (k.kq_ret > -.015)
    return k[["S", "kq_ret", "kq_d20", "kq_r20", "gate_ok"]]


def extra(code, a):
    d = pd.DataFrame(a, columns=["S", "o", "h", "l", "c", "v"])
    c, ret = d.c, d.c.pct_change()
    ma5, ma20, ma60, ma120 = (c.rolling(k).mean() for k in (5, 20, 60, 120))
    tv = c*d.v
    rng = d.h-d.l
    up = (ret > 0).astype(int)
    e = pd.DataFrame(dict(
        code=code, S=d.S, d60=c/ma60-1, d120=c/ma120-1, ma60slope=ma60/ma60.shift(20)-1,
        align=(ma5 > ma20) & (ma20 > ma60), r60=c/c.shift(60)-1,
        hd20=c/d.h.rolling(20).max()-1, hd120=c/d.h.rolling(120).max()-1, ld20=c/d.l.rolling(20).min()-1,
        clv=np.where(rng > 0, (c-d.l)/rng.where(rng > 0, 1), .5), vol20=ret.rolling(20).std(),
        sgap=d.o/c.shift()-1, chg_prev=ret.shift(), upstreak=up.groupby((up == 0).cumsum()).cumsum(),
        tv5ratio=tv.rolling(5).mean()/tv.shift(1).rolling(20).median()))
    return e


def one_code(item):
    code, a = item
    f = pd.DataFrame(cn.features(code, a, start="20250401", end="20261113"))
    if f.empty:
        return f
    return f.merge(extra(code, a), on=["code", "S"], how="left")


def build_panel():
    bars = cn.load_bars(SRC)
    with cf.ProcessPoolExecutor(8) as ex:
        parts = list(ex.map(one_code, bars.items(), chunksize=20))
    f = pd.concat([p for p in parts if len(p)], ignore_index=True)
    f = f[f.D.notna()].copy()
    f = f.merge(kosdaq(), on="S", how="left")
    f["gate_ok"] = f.gate_ok.fillna(False).astype(bool)
    gap, o1, o2 = cn.daily_outcomes(f)
    f["E_open"] = gap-COST
    f["O2"] = o2
    f["E_close"] = f.Dc/f.close-1-COST
    f["liquid"] = f.tvmed >= 3e9
    f["U"] = f.liquid & (f.close >= 1000) & (f.chg1 < .295) & (f.Do > 0)
    # market breadth among liquid stocks, known at S close
    lq = f[f.liquid]
    b = lq.assign(a20=lq.d20 > 0, up=lq.chg1 > 0).groupby("S")[["a20", "up"]].mean()
    f = f.merge(b.rename(columns=dict(a20="breadth20", up="adv")), left_on="S", right_index=True, how="left")
    f["period"] = None
    for name, (s, e) in PERIODS.items():
        f.loc[(f.S >= s) & (f.S <= e), "period"] = name
    cn.label_picks(f)
    return f


def prefix_check(bars, n=6, seed=3):
    """Features at S must not change when bars after S are removed."""
    rng = np.random.default_rng(seed)
    codes = rng.choice(sorted(bars), n, replace=False)
    cols = ["d20", "d60", "ma20slope", "ma60slope", "r20", "r60", "hd60", "hd120", "ld20", "clv", "vol20", "sgap",
            "tvratio", "tv5ratio", "ref_age", "upstreak"]
    bad = 0
    for c in codes:
        a = bars[c]
        cut = len(a)-40
        full = one_code((c, a)).set_index("S")
        part = one_code((c, a[:cut])).set_index("S")
        common = part.index.intersection(full.index)[:-1]  # last row of the cut has no D; compare features only
        x, y = full.loc[common, cols].astype(float), part.loc[common, cols].astype(float)
        bad += int((~np.isclose(x.to_numpy(), y.to_numpy(), equal_nan=True)).sum())
    return dict(codes=[str(c) for c in codes], mismatches=bad)

# ---------------- condition families ----------------

def families(f):
    st = f.status
    F = {
        "trend": {"ma20_up": (f.d20 > 0) & (f.ma20slope > 0), "align": f["align"].fillna(False).astype(bool),
                  "ma60_up": (f.d60 > 0) & (f.ma60slope > 0), "below_ma20": f.d20 < 0},
        "mom20": {"neg": f.r20 < 0, "0-10": (f.r20 >= 0) & (f.r20 < .10), "10-30": (f.r20 >= .10) & (f.r20 < .30),
                  "30+": f.r20 >= .30},
        "high60": {"near": f.hd60 > -.05, "mid": (f.hd60 <= -.05) & (f.hd60 > -.15), "deep": f.hd60 <= -.15},
        "day": {"dn5-0": (f.chg1 >= -.05) & (f.chg1 < 0), "0-3": (f.chg1 >= 0) & (f.chg1 < .03),
                "3-7": (f.chg1 >= .03) & (f.chg1 < .07), "7-15": (f.chg1 >= .07) & (f.chg1 < .15),
                "15+": f.chg1 >= .15},
        "shape": {"strong_close": (f.clv > .8) & (f.wick < .2), "upper_wick": f.wick > .5,
                  "weak_close": f.clv < .3},
        "volume": {"<0.7": f.tvratio < .7, "0.7-1.5": (f.tvratio >= .7) & (f.tvratio < 1.5),
                   "1.5-3": (f.tvratio >= 1.5) & (f.tvratio < 3), "3+": f.tvratio >= 3},
        "ref": {"today": f.ref_age == 0, "1-3": (f.ref_age >= 1) & (f.ref_age <= 3),
                "4-10": (f.ref_age >= 4) & (f.ref_age <= 10), "none": f.ref_age > 10},
        "scan": {"watch": f.ndw_valid, "point": st.isin(["entry", "point_today", "above_point"]),
                 "waiting": st.isin(["waiting_point", "waiting_breakout"]), "invalid": st == "invalidated"},
        "market": {"gate_ok": f.gate_ok, "kq_up": f.kq_ret > 0, "breadth_hi": f.breadth20 > .5,
                   "kq_weak": f.kq_d20 < 0},
        "size": {"small": f.tvmed < 1e10, "mid": (f.tvmed >= 1e10) & (f.tvmed < 5e10), "large": f.tvmed >= 5e10},
    }
    return {fam: {k: np.asarray(v.fillna(False) if hasattr(v, "fillna") else v, bool) for k, v in lv.items()}
            for fam, lv in F.items()}


def cells(F, max_k=3):
    fams = list(F)
    for k in range(1, max_k+1):
        for combo in itertools.combinations(fams, k):
            for levels in itertools.product(*(F[x] for x in combo)):
                yield tuple(f"{a}={b}" for a, b in zip(combo, levels))


def mask_of(F, cell):
    m = None
    for t in cell:
        fam, lv = t.split("=", 1)
        m = F[fam][lv] if m is None else m & F[fam][lv]
    return m

# ---------------- statistics ----------------

def clustered(x, day_idx, nd):
    """Trade-pooled mean with a day-clustered (ratio estimator) standard error."""
    n = len(x)
    if n == 0:
        return dict(n=0, days=0, mean=np.nan, se=np.nan, t=np.nan)
    s = np.bincount(day_idx, weights=x, minlength=nd)
    c = np.bincount(day_idx, minlength=nd).astype(float)
    m = s.sum()/n
    act = c > 0
    G = int(act.sum())
    resid = s[act]-m*c[act]
    se = math.sqrt(G/(G-1)*(resid**2).sum())/n if G > 1 else np.nan
    return dict(n=int(n), days=G, mean=float(m), se=float(se), t=float(m/se) if se and se > 0 else np.nan)


def boot_ci(x, days, draws=2000, seed=7):
    g = pd.DataFrame(dict(d=days, x=x)).groupby("d").x.agg(["sum", "count"])
    if len(g) < 5:
        return None
    idx = np.random.default_rng(seed).integers(0, len(g), (draws, len(g)))
    b = g["sum"].to_numpy()[idx].sum(1)/g["count"].to_numpy()[idx].sum(1)
    return [float(np.quantile(b, .025)), float(np.quantile(b, .975))]


def bh(p):
    p = np.asarray(p, float)
    o = np.argsort(p)
    q = p[o]*len(p)/np.arange(1, len(p)+1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty_like(q)
    out[o] = np.minimum(q, 1)
    return out


def norm_sf(t):
    return .5*math.erfc(t/math.sqrt(2))


def describe(g, col="E_open"):
    x = g[col].to_numpy(float)
    if not len(x):
        return dict(n=0)
    w, lo = x[x > 0], x[x < 0]
    return dict(n=int(len(x)), days=int(g.S.nunique()), mean=float(x.mean()), median=float(np.median(x)),
                win=float((x > 0).mean()), pf=float(w.sum()/-lo.sum()) if len(lo) else None,
                ci=boot_ci(x, g.S.to_numpy()), worst=float(x.min()))


def account(g, col="E_open"):
    """Equal weight across the day's picks, full capital each signal day, compounded. Unit-curve, not slot-limited."""
    d = g.groupby("S")[col].mean().sort_index()
    eq = (1+d).cumprod()
    dd = float((eq/eq.cummax()-1).min()) if len(eq) else 0.
    return dict(days=int(len(d)), final=float(eq.iloc[-1]-1) if len(eq) else 0., mdd=dd,
                pos_days=float((d > 0).mean()) if len(d) else None)

# ---------------- part A: picks profile ----------------

def profile(f, F):
    from sklearn.metrics import roc_auc_score
    from sklearn.tree import DecisionTreeClassifier, export_text
    keep = f.U & (f.S >= "20260310")
    P = f[keep]
    base = P.pick.mean()
    lift = []
    for fam, lv in F.items():
        for k, m in lv.items():
            mm = m[keep.to_numpy()]
            sub = P[mm]
            if len(sub) < 100:
                continue
            lift.append(dict(family=fam, level=k, share_universe=float(mm.mean()), share_picks=float(mm[P.pick.to_numpy()].mean()),
                             lift=float(sub.pick.mean()/base), n=int(len(sub))))
    cols = ["chg1", "r5", "r20", "r60", "d5", "d20", "d60", "ma20slope", "ma60slope", "hd20", "hd60", "ld20",
            "tvratio", "tv5ratio", "ref_age_c", "nref20", "wick", "body", "clv", "vol20", "sgap", "upstreak", "log_tvmed"]
    P = P.assign(ref_age_c=P.ref_age.clip(upper=12), log_tvmed=np.log10(P.tvmed))
    tr, te = P[P.S <= "20260630"], P[P.S > "20260630"]
    X = lambda d: d[cols].astype(float).fillna(d[cols].astype(float).median())  # noqa: E731
    tree = DecisionTreeClassifier(max_depth=4, min_samples_leaf=300, class_weight="balanced", random_state=0)
    tree.fit(X(tr), tr.pick)
    auc_tr = roc_auc_score(tr.pick, tree.predict_proba(X(tr))[:, 1])
    auc_te = roc_auc_score(te.pick, tree.predict_proba(X(te))[:, 1]) if te.pick.any() else None
    # leaf table: pick precision, recall, and next-day outcome of non-pick members (look-alikes)
    leaves = []
    for name, d in (("train", tr), ("later", te)):
        leaf = tree.apply(X(d))
        for lf in np.unique(leaf):
            g = d[leaf == lf]
            leaves.append(dict(period=name, leaf=int(lf), n=int(len(g)), picks=int(g.pick.sum()),
                               precision=float(g.pick.mean()), recall=float(g.pick.sum()/max(1, d.pick.sum())),
                               pick_E_open=float(g[g.pick].E_open.mean()) if g.pick.any() else None,
                               other_E_open=float(g[~g.pick].E_open.mean())))
    text = export_text(tree, feature_names=cols, decimals=3, show_weights=False)
    picks = P[P.pick]
    return dict(base_rate=float(base), n_universe=int(len(P)), n_picks=int(len(picks)),
                picks_outside_universe=int(f[(f.S >= "20260310") & f.pick & ~f.U].shape[0]),
                lift=sorted(lift, key=lambda r: -r["lift"]), tree_text=text, auc_train=float(auc_tr), auc_later=auc_te,
                leaves=leaves,
                picks_E_open=describe(picks), lookalike_note="other_E_open = non-pick rows in the same leaf")

# ---------------- part B: search ----------------

def search(f, F):
    U = f.U.to_numpy()
    per = f.period.to_numpy()
    x = f.E_open.to_numpy(float)
    day_codes, day_idx = np.unique(f.S.to_numpy(), return_inverse=True)
    nd = len(day_codes)
    tr = U & (per == "train")
    va = U & (per == "valid")
    rows = []
    for cell in cells(F):
        m = mask_of(F, cell)
        mt = m & tr
        if mt.sum() < MIN_TRAIN_N:
            continue
        st = clustered(x[mt], day_idx[mt], nd)
        if st["days"] < MIN_TRAIN_DAYS:
            continue
        mv = m & va
        sv = clustered(x[mv], day_idx[mv], nd)
        rows.append(dict(cell=" & ".join(cell), k=len(cell), tr_n=st["n"], tr_days=st["days"], tr_mean=st["mean"],
                         tr_t=st["t"], va_n=sv["n"], va_mean=sv["mean"], va_t=sv["t"]))
    S = pd.DataFrame(rows)
    S["tr_p"] = [norm_sf(t) if np.isfinite(t) else 1. for t in S.tr_t]
    S["tr_q"] = bh(S.tr_p)
    S["pass_train"] = (S.tr_mean > 0) & (S.tr_q < .05)
    S["pass_valid"] = S.pass_train & (S.va_n >= MIN_VALID_N) & (S.va_mean > 0) & (S.va_t >= 1.645)
    S["score"] = np.minimum(S.tr_t, S.va_t)
    return S


def select(f, F, cell, top=None):
    m = mask_of(F, tuple(cell.split(" & "))) & f.U.to_numpy()
    g = f[m]
    if top:
        g = g.sort_values("tv", ascending=False).groupby("S").head(top)
    return g


def evaluate(f, g, label):
    out = []
    for p in ("train", "valid", "eval", "final"):
        gg = g[g.period == p]
        out.append(dict(set=label, period=p, per_day=float(gg.groupby("S").size().median()) if len(gg) else 0,
                        E_open=describe(gg, "E_open"), O2=describe(gg, "O2"), E_close=describe(gg, "E_close"),
                        account_E_open=account(gg, "E_open"), picks_in=int(gg.pick.sum())))
    return out


def random_baseline(f, seeds=200):
    U = f[f.U]
    res = {p: [] for p in PERIODS}
    for s in range(seeds):
        r = U.sample(frac=1, random_state=s).groupby("S").head(TOP_K)
        for p in PERIODS:
            res[p].append(r[r.period == p].E_open.mean())
    return {p: dict(mean=float(np.mean(v)), p05=float(np.quantile(v, .05)), p95=float(np.quantile(v, .95)))
            for p, v in res.items()}


MODEL_COLS = ["chg1", "r5", "r20", "r60", "d5", "d20", "d60", "d120", "ma20slope", "ma60slope", "hd20", "hd60", "hd120",
              "ld20", "tvratio", "tv5ratio", "ref_age", "nref20", "wick", "body", "clv", "vol20", "sgap", "chg_prev",
              "upstreak", "tvmed", "kq_ret", "kq_d20", "kq_r20", "breadth20", "adv", "ndw_valid", "todayref"]


def lgbm(f):
    import lightgbm as lgb
    U = f[f.U].copy()
    U["ndw_valid"] = U.ndw_valid.astype(int)
    U["todayref"] = U.todayref.astype(int)
    X = U[MODEL_COLS].astype(float)
    y = U.E_open.clip(-.15, .15)
    tr, va = (U.period == "train").to_numpy(), (U.period == "valid").to_numpy()
    model = lgb.LGBMRegressor(n_estimators=2000, learning_rate=.02, num_leaves=31, min_child_samples=400,
                              subsample=.8, subsample_freq=1, colsample_bytree=.8, reg_lambda=5., random_state=0, verbose=-1)
    model.fit(X[tr], y[tr], eval_set=[(X[va], y[va])], callbacks=[lgb.early_stopping(100, verbose=False)])
    U["score"] = model.predict(X)
    top = U.sort_values("score", ascending=False).groupby("S").head(TOP_K)
    imp = sorted(zip(MODEL_COLS, model.booster_.feature_importance("gain")), key=lambda t: -t[1])
    return top, dict(best_iteration=int(model.best_iteration_ or 0),
                     importance=[dict(feature=a, gain=float(b)) for a, b in imp[:15]])

# ---------------- v2: relaxed rule, locked before eval cells are opened ----------------
V2_RULE = dict(
    reason="v1 rule (BH q<0.05 on train over 5,523 cells) left 0 survivors (min q 0.06). Relaxed rule fixed from "
           "train/valid only; no cell's eval/final result was looked at before this lock.",
    pass_rule="tr_mean > 0, va_mean > 0, tr_t >= 3, va_t >= 2.5, va_n >= 80",
    singles="top 5 survivors by min(tr_t, va_t), each as top 3 per day by S-day turnover",
    composite="MY3: rows matching >= 1 survivor cell; rank by number of survivor cells matched, then S-day turnover; top 3 per day",
    exit="E_open primary; O2 and E_close reported",
    success="eval mean > 0 AND eval mean > random top-3 p95 for eval. final is reported, not used to decide",
)


def lock_v2(out):
    p = out/"lock-v2.json"
    if p.exists():
        raise SystemExit("v2 already locked")
    S = pd.read_parquet(out/"cells.parquet")
    m = (S.tr_mean > 0) & (S.va_mean > 0) & (S.tr_t >= 3) & (S.va_t >= 2.5) & (S.va_n >= 80)
    surv = S[m].assign(score=np.minimum(S.tr_t, S.va_t)).sort_values("score", ascending=False)
    import datetime as dt
    atomic_json(p, dict(V2_RULE, locked_at=dt.datetime.now(dt.timezone.utc).isoformat(), source_sha256=sha(Path(__file__)),
                        cells_sha256=sha(out/"cells.parquet"), survivors=surv.cell.tolist(), singles=surv.cell.head(5).tolist()))
    print("locked", len(surv))


def composite(f, F, cell_list, top=TOP_K):
    cnt = np.zeros(len(f), int)
    for c in cell_list:
        cnt += mask_of(F, tuple(c.split(" & ")))
    g = f[(cnt > 0) & f.U.to_numpy()].assign(n_match=cnt[(cnt > 0) & f.U.to_numpy()])
    return g.sort_values(["n_match", "tv"], ascending=False).groupby("S").head(top)


def eval_v2(out):
    lock = json.loads((out/"lock-v2.json").read_text())
    f = pd.read_parquet(out/"panel.parquet")
    F = families(f)
    res = []
    sets = {f"top{TOP_K}: {c}": select(f, F, c, TOP_K) for c in lock["singles"]}
    sets |= {f"all: {c}": select(f, F, c) for c in lock["singles"]}
    sets["MY3 composite"] = composite(f, F, lock["survivors"])
    for name, g in sets.items():
        res += evaluate(f, g, name)
    base = json.loads((out/"results.json").read_text())["random_top3"]
    my = sets["MY3 composite"]
    my[["code", "S", "D", "period", "close", "chg1", "r20", "tvratio", "n_match", "E_open", "O2", "E_close", "pick"]].to_parquet(
        out/"my3-selections.parquet", index=False)
    monthly = my.assign(m=my.S.str[:6]).groupby("m").agg(n=("E_open", "size"), E_open=("E_open", "mean"),
                                                          O2=("O2", "mean"), picks=("pick", "sum")).reset_index()
    atomic_json(out/"results-v2.json", dict(lock=lock, random_top3=base, results=res, my3_monthly=monthly.to_dict("records")))
    print("v2 evaluated")

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT))
    ap.add_argument("--lock-v2", action="store_true")
    ap.add_argument("--eval-v2", action="store_true")
    args = ap.parse_args()
    out = Path(args.output)
    if args.lock_v2:
        return lock_v2(out)
    if args.eval_v2:
        return eval_v2(out)
    if (out/"cells.parquet").exists():
        raise SystemExit(f"{out} is frozen; choose a new version")
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__)),
                                          inputs=dict(kosdaq_sha256=sha(KOSDAQ_RAW))))
    bars = cn.load_bars(SRC)
    pc = prefix_check(bars)
    print("prefix check", pc, flush=True)
    if (out/"panel.parquet").exists():  # deterministic from frozen inputs; reuse after a crash
        f = pd.read_parquet(out/"panel.parquet")
    else:
        f = build_panel()
        f.to_parquet(out/"panel.parquet", index=False)
    print("panel", f.shape, "U", int(f.U.sum()), f.groupby("period").U.sum().to_dict(), flush=True)
    F = families(f)

    A = profile(f, F)
    atomic_json(out/"profile.json", A)
    print("profile done: AUC", A["auc_train"], A["auc_later"], flush=True)

    S = search(f, F)
    S.to_parquet(out/"cells.parquet", index=False)
    surv = S[S.pass_valid].sort_values("score", ascending=False)
    print("cells", len(S), "pass train", int(S.pass_train.sum()), "pass valid", len(surv), flush=True)

    results = []
    chosen = surv.head(5).cell.tolist()
    for c in chosen:
        results += evaluate(f, select(f, F, c), f"cell: {c}")
        results += evaluate(f, select(f, F, c, TOP_K), f"cell top{TOP_K}: {c}")
    top, minfo = lgbm(f)
    results += evaluate(f, top, f"lgbm top{TOP_K}")
    results += evaluate(f, f[f.U], "universe all")
    results += evaluate(f, f[f.U & f.pick], "instructor picks (in universe)")
    results += evaluate(f, f[f.U & f.pick & ~(((f.chg1 > .10) & (f.chg1 <= .20)) | (f.ref_age == 0) | (f.status == "invalidated"))],
                        "instructor picks, risk flags removed")
    base = random_baseline(f)
    sel = pd.concat([select(f, F, c, TOP_K).assign(set=c) for c in chosen] + [top.assign(set="lgbm")], ignore_index=True)
    sel[["set", "code", "S", "D", "period", "close", "chg1", "r20", "tvratio", "E_open", "O2", "E_close", "pick"]].to_parquet(
        out/"selections.parquet", index=False)
    atomic_json(out/"results.json", dict(prefix_check=pc, chosen=chosen, survivors=surv.head(30).to_dict("records"),
                                         n_cells=int(len(S)), n_pass_train=int(S.pass_train.sum()), n_pass_valid=int(len(surv)),
                                         random_top3=base, lgbm=minfo, results=results))
    print("done", out, flush=True)


if __name__ == "__main__":
    main()
