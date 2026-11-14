"""Metrics v2: quality, tail risk, drawdown/recovery, stability, concentration, exposure, significance.

Shared by Arena runs (added on top of the v1 fields in metrics.score, which stay unchanged) and by
study cells (arena.studies). Everything is computed from closed-trade net returns and the daily
unit-stake series, so every model and every condition cell is measured the same way.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()


def day_bootstrap(entry_days, rets, n_boot: int = 1000, seed: int = 7):
    """Mean-return CI and one-sided p(mean <= 0), resampling whole entry days."""
    if len(rets) == 0:
        return [None, None], None
    days, inv = np.unique(np.asarray(entry_days), return_inverse=True)
    if len(days) < 2:
        return [None, None], None
    sums = np.bincount(inv, weights=np.asarray(rets, float))
    cnts = np.bincount(inv).astype(float)
    idx = np.random.default_rng(seed).integers(0, len(days), size=(n_boot, len(days)))
    means = sums[idx].sum(1) / cnts[idx].sum(1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    return [float(lo), float(hi)], float((means <= 0).mean())


def _runs_longest(mask) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def extended(rets, order_key, entry_days, daily_pp, day_labels, n_entries, *, tol=1e-9,
             symbols=None, hold_minutes=None, risk=None, active_days=None) -> dict:
    """rets/order_key/entry_days/symbols/hold_minutes/risk: per closed trade.
    daily_pp/day_labels: unit-stake P&L per calendar business day (percentage points)."""
    r = np.asarray(rets, float)
    out: dict = {}
    n = len(r)
    if n:
        o = np.argsort(np.asarray(order_key), kind="stable")
        r_ord = r[o]
    wins, losses = r[r > tol], r[r < -tol]
    out["profit_rate"] = float((r > tol).sum() / n_entries) if n_entries else None
    out["profit_rate_01"] = float((r > 0.001).sum() / n_entries) if n_entries else None  # meaningful win: > +0.1%
    out["payoff_ratio"] = float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None
    if risk is not None and n:
        rk = np.asarray(risk, float)
        ok = np.isfinite(rk) & (rk > 0)
        R = r[ok] / rk[ok]
        out["expectancy_r"] = float(R.mean()) if len(R) else None
        out["sqn"] = float(math.sqrt(min(len(R), 100)) * R.mean() / R.std(ddof=1)) if len(R) > 2 and R.std(ddof=1) > 0 else None
    k = max(1, math.ceil(0.05 * n)) if n else 0
    out["cvar5"] = float(np.sort(r)[:k].mean()) if n else None
    out["max_consecutive_losses"] = _runs_longest(r_ord < -tol) if n else 0
    out["mean_ex_top5"] = float(np.sort(r)[:-5].mean()) if n > 10 else None

    # daily series: drawdown, recovery, ulcer, sortino, monthly stability
    d = np.asarray(daily_pp, float)
    if len(d):
        cum = np.cumsum(d)
        peak = np.maximum.accumulate(np.maximum(cum, 0.0))
        dd = cum - peak
        mdd = float(dd.min())
        out["worst_day_pp"] = float(d.min())
        out["longest_drawdown_days"] = _runs_longest(dd < -1e-9)
        out["recovered"] = bool(dd[-1] >= -1e-9)
        out["ulcer_index"] = float(math.sqrt((dd ** 2).mean()))
        out["calmar"] = float(cum[-1] / -mdd) if mdd < 0 else None
        down = d[d < 0]
        dsd = math.sqrt((down ** 2).sum() / len(d)) if len(down) else 0.0
        out["sortino_daily_ann"] = float(d.mean() / dsd * math.sqrt(252)) if dsd > 0 else None
        sd = d.std(ddof=1) if len(d) > 1 else 0.0
        out["sr_daily"] = float(d.mean() / sd) if sd > 0 else None
        if len(d) > 3 and sd > 0:
            z = (d - d.mean()) / d.std()
            out["skew_daily"], out["kurt_daily"] = float((z ** 3).mean()), float((z ** 4).mean())
        out["t_days"] = int(len(d))
        months: dict[str, float] = {}
        for lab, v in zip(day_labels, d):
            months[lab[:7]] = months.get(lab[:7], 0.0) + float(v)
        out["monthly_pp"] = {m: round(v, 4) for m, v in sorted(months.items())}
        traded = [m for m, v in months.items() if abs(v) > 1e-12]
        out["positive_month_ratio"] = (sum(months[m] > 0 for m in traded) / len(traded)) if traded else None
    if n >= 4:
        h = n // 2
        out["mean_first_half"], out["mean_second_half"] = float(r_ord[:h].mean()), float(r_ord[h:].mean())

    if symbols is not None and n:
        _, cnt = np.unique(np.asarray(symbols), return_counts=True)
        out["n_symbols"] = int(len(cnt))
        out["top_symbol_share"] = float(cnt.max() / n)
    if active_days is not None and len(d):
        out["day_ratio"] = float(active_days / len(d))
        out["trades_per_active_day"] = float(n_entries / active_days) if active_days else None
    if hold_minutes is not None and n:
        hm = np.asarray(hold_minutes, float)
        hm = hm[np.isfinite(hm)]
        out["avg_hold_minutes"] = float(hm.mean()) if len(hm) else None

    ci, p = day_bootstrap(entry_days, r)
    out["p_value_mean_gt0"] = p
    out["_ci95_np"] = ci
    return out


# ---------- multiple-testing / selection-bias corrections ----------

def bh_qvalues(pvals: dict) -> dict:
    """Benjamini–Hochberg q-values for {key: p} (None ignored)."""
    items = sorted(((p, k) for k, p in pvals.items() if p is not None))
    m, q, prev = len(items), {}, 1.0
    for rank in range(m, 0, -1):
        p, k = items[rank - 1]
        prev = min(prev, p * m / rank)
        q[k] = prev
    return q


def deflated_sharpe(sr, t, skew, kurt, sr_trials) -> float | None:
    """Bailey & López de Prado (2014): P(true SR > 0) after selecting the best of N trials.
    sr: daily (non-annualised) Sharpe of the candidate; sr_trials: daily Sharpes of all trials."""
    trials = [s for s in sr_trials if s is not None and math.isfinite(s)]
    if sr is None or t is None or t < 10 or len(trials) < 2:
        return None
    var = float(np.var(trials, ddof=1))
    n = len(trials)
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * _N.inv_cdf(1 - 1 / n) + g * _N.inv_cdf(1 - 1 / (n * math.e))) if var > 0 else 0.0
    sk = skew if skew is not None else 0.0
    ku = kurt if kurt is not None else 3.0
    den = 1 - sk * sr + (ku - 1) / 4 * sr * sr
    if den <= 0:
        return None
    return float(_N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den)))


def random_percentile(value, pool_rets, n, draws: int = 2000, seed: int = 11) -> float | None:
    """Share of random same-size selections from the candidate pool whose mean is below `value`."""
    pool = np.asarray(pool_rets, float)
    if value is None or n <= 0 or len(pool) == 0:
        return None
    n = min(n, len(pool))
    rng = np.random.default_rng(seed)
    idx = np.argsort(rng.random((draws, len(pool))), axis=1)[:, :n]
    means = pool[idx].mean(1)
    return float((means < value).mean())
