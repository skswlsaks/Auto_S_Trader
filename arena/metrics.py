"""Standard metrics. Every run is scored by this code only, never by self-reported numbers."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _day(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone(KST)
    return dt.date().isoformat()


def wilson(successes: int, n: int, z: float = 1.96):
    if n == 0:
        return [None, None]
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, center - half), min(1.0, center + half)]


def business_days(start: str, end: str) -> list[str]:
    d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= last:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def day_bootstrap_mean(trades: list, n_boot: int = 1000, seed: int = 7):
    """95% interval of mean net return, resampling whole entry days (keeps same-day correlation)."""
    by_day: dict[str, list[float]] = {}
    for t in trades:
        if t["net_return"] is not None:
            by_day.setdefault(_day(t["entry_at"]), []).append(t["net_return"])
    days = list(by_day.values())
    if len(days) < 2:
        return [None, None]
    rng, means = random.Random(seed), []
    for _ in range(n_boot):
        sample = [r for _ in days for r in rng.choice(days)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return [means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]]


def score(trades: list, track: dict, split: str) -> dict:
    tol = track.get("success_tolerance", 1e-9)
    n = len(trades)
    closed = [t for t in trades if t["complete"]]
    rets = [t["net_return"] for t in closed]
    success = sum(1 for r in rets if r >= -tol)
    wins = [r for r in rets if r > tol]
    losses = [r for r in rets if r < -tol]
    srt = sorted(rets)

    # Unit-stake cumulative curve by exit day (KST), in percentage points.
    window = track["windows"][split]
    days_all = [_day(t["entry_at"]) for t in trades]
    start = window.get("start") or min(days_all)
    end = window.get("end") or max([_day(t["exit_at"]) for t in closed] + days_all)
    cal = business_days(start, end)
    daily = dict.fromkeys(cal, 0.0)
    for t in closed:
        d = _day(t["exit_at"])
        daily[d] = daily.get(d, 0.0) + t["net_return"] * 100
    curve, cum, peak, mdd = [], 0.0, 0.0, 0.0
    for d in sorted(daily):
        cum += daily[d]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
        curve.append([d, round(cum, 4)])
    vals = [daily[d] for d in sorted(daily)]
    mu = sum(vals) / len(vals) if vals else 0.0
    sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0

    active_days = len(set(days_all))
    gates = track.get("gates_by_split", {}).get(split, track.get("gates", {}))
    gate_results = {
        "min_trades": n >= gates.get("min_trades", 0),
        "min_active_days": active_days >= gates.get("min_active_days", 0),
    }
    hist_edges = [x / 100 for x in range(-4, 9)]  # -4% .. +8% in 1% bins
    hist = [0] * (len(hist_edges) + 1)
    for r in rets:
        i = sum(1 for e in hist_edges if r >= e)
        hist[i] += 1
    return {
        "trades": n,
        "closed": len(closed),
        "unclosed": n - len(closed),
        "active_days": active_days,
        "accuracy": success / n if n else None,
        "accuracy_ci95": wilson(success, n),
        "win_rate": len(wins) / len(closed) if closed else None,
        "mean_return": sum(rets) / len(rets) if rets else None,
        "mean_return_ci95": day_bootstrap_mean(closed),
        "median_return": (srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2) if srt else None,
        "mean_win": sum(wins) / len(wins) if wins else None,
        "mean_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
        "worst_trade": srt[0] if srt else None,
        "best_trade": srt[-1] if srt else None,
        "total_return_pp": round(cum, 4),
        "max_drawdown_pp": round(mdd, 4),
        "sharpe_daily_ann": (mu / sd * math.sqrt(252)) if sd > 0 else None,
        "gates": gate_results,
        "passes_gates": all(gate_results.values()),
        "curve": curve,
        "return_hist": {"edges": hist_edges, "counts": hist},
        "exit_reasons": _count(t.get("exit_reason") or ("unclosed" if not t["complete"] else "unknown") for t in trades),
    }


def _count(items):
    out: dict[str, int] = {}
    for k in items:
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
