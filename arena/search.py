"""Combination search over a lab source: every model value x up to `max_conds` condition values.

The honest version of "which combination is best": rank on a *selection* period, correct for the
number of combinations tried (Benjamini-Hochberg over a day-clustered one-sided test of mean > 0),
then look at the same combination on a later *test* period it was not chosen on, and optionally on
another source (a locked holdout), matched by column values.

Mirrored exactly by web/src/lib/search.ts (parity fixture: web/tests/fixtures/search.json).
"""
from __future__ import annotations

import itertools
import math

import numpy as np

TOL = 1e-9
SKIP_ALWAYS = {"month"}  # calendar buckets: they only re-slice the split itself
SORTS = ("sel", "test", "worst", "q")


def erfc(x: float) -> float:
    """Numerical Recipes erfcc (fractional error < 1.2e-7); same formula in search.ts for parity."""
    z = abs(x)
    t = 1 / (1 + 0.5 * z)
    r = t * math.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (-0.18628806 + t * (
        0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))))
    return r if x >= 0 else 2 - r


def p_mean_gt0(ret: np.ndarray, day: np.ndarray) -> float | None:
    """One-sided p for mean > 0 with a day-clustered standard error (normal approximation)."""
    n = len(ret)
    if n < 2:
        return None
    m = ret.sum() / n
    _, inv = np.unique(day, return_inverse=True)
    k = inv.max() + 1
    if k < 2:
        return None
    e = np.bincount(inv, weights=ret - m, minlength=k)
    var = float((e * e).sum()) / (n * n) * k / (k - 1)
    if var <= 0:
        return 0.0 if m > 0 else 1.0
    return 0.5 * erfc(m / math.sqrt(var) / math.sqrt(2))


def bh(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q, run = [1.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        run = min(run, ps[i] * m / rank)
        q[i] = min(1.0, run)
    return q


def stats(ret: np.ndarray, day: np.ndarray, with_p: bool = False) -> dict | None:
    n = len(ret)
    if not n:
        return None
    w, l = ret[ret > TOL], ret[ret < -TOL]
    out = {"n": int(n), "days": int(len(np.unique(day))), "mean": float(ret.sum() / n), "sum_pp": float(ret.sum() * 100),
           "accuracy": float((ret >= -TOL).sum() / n), "profit_rate_01": float((ret > 0.001).sum() / n),
           "profit_factor": float(w.sum() / -l.sum()) if len(l) else None,
           "mean_ex_top5": float(np.sort(ret)[:-5].sum() / (n - 5)) if n > 10 else None}
    if with_p:
        out["p"] = p_mean_gt0(ret, day)
    return out


def _time_partition(codes: np.ndarray, day: np.ndarray) -> bool:
    """True when each value occupies its own contiguous date range (e.g. a before/after period column)."""
    spans = sorted((day[codes == v].min(), day[codes == v].max()) for v in np.unique(codes))
    return len(spans) > 1 and all(a[1] < b[0] for a, b in zip(spans, spans[1:]))


def default_cut(src: dict) -> str:
    """First day of the last third of the source's trading days."""
    used = np.unique(np.asarray(src["day"]))
    return src["days"][int(used[min(len(used) - 1, (2 * len(used)) // 3)])]


def search(src: dict, model_keys: list[str] | None = None, cond_keys: list[str] | None = None, max_conds: int = 2,
           split: dict | None = None, min_n: int = 25, min_days: int = 10, holdout: dict | None = None,
           sort: str = "sel", top: int = 30, min_test_n: int = 10) -> dict:
    cols = {c["key"]: c for c in src["columns"]}
    ret, day = np.asarray(src["ret"], float), np.asarray(src["day"])
    code = {k: np.asarray(c["codes"]) for k, c in cols.items()}
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {SORTS}")
    if not 0 <= max_conds <= 2:
        raise ValueError("max_conds must be 0, 1 or 2")

    split = dict(split or {"by": "date", "cut": default_cut(src)})
    if split["by"] == "date":
        cut = next((i for i, d in enumerate(src["days"]) if d >= split["cut"]), len(src["days"]))
        part = np.where(day < cut, 1, 2)
    elif split["by"] == "column":
        c = cols[split["key"]]
        sv = [c["values"].index(v) for v in split["select"]]
        tv = [c["values"].index(v) for v in split["test"]]
        part = np.where(np.isin(code[split["key"]], sv), 1, np.where(np.isin(code[split["key"]], tv), 2, 0))
    else:
        raise ValueError("split.by must be 'date' or 'column'")

    model_keys = model_keys or [k for k, c in cols.items() if c["role"] == "model"]
    skipped = {}
    if cond_keys is None:
        cond_keys = []
        for k, c in cols.items():
            if c["role"] != "condition" or k in model_keys:
                continue
            if k in SKIP_ALWAYS or k == split.get("key"):
                skipped[k] = "calendar or split column"
            elif len(np.unique(code[k])) < 2:
                skipped[k] = "one value only"
            elif _time_partition(code[k], day):
                skipped[k] = "splits by date"
            else:
                cond_keys.append(k)
    for k in [*model_keys, *cond_keys]:
        if k not in cols:
            raise ValueError(f"unknown column '{k}'. columns: {list(cols)}")

    colsets = [cs for r in range(max_conds + 1) for cs in itertools.combinations(cond_keys, r)]
    mcodes = np.stack([code[k] for k in model_keys], 1) if model_keys else np.zeros((len(ret), 0), int)
    groups = {}
    for i, row in enumerate(map(tuple, mcodes)):
        groups.setdefault(row, []).append(i)

    rows = []
    for mkey in sorted(groups):
        I = np.asarray(groups[mkey])
        for cs in colsets:
            radix = [len(cols[k]["values"]) for k in cs]
            joint = np.zeros(len(I), np.int64)
            for k, rx in zip(cs, radix):
                joint = joint * rx + code[k][I]
            sel_mask = part[I] == 1
            counts = np.bincount(joint[sel_mask], minlength=int(np.prod(radix)) if cs else 1)
            for b in np.nonzero(counts >= min_n)[0]:
                J = I[joint == b]
                js, jt = J[part[J] == 1], J[part[J] == 2]
                s = stats(ret[js], day[js], with_p=True)
                if s["days"] < min_days:
                    continue
                vals, rest = [], int(b)
                for rx in reversed(radix):
                    vals.append(rest % rx)
                    rest //= rx
                vals.reverse()
                rows.append({"model": {k: cols[k]["values"][v] for k, v in zip(model_keys, mkey)},
                             "model_labels": {k: cols[k]["labels"][v] for k, v in zip(model_keys, mkey)},
                             "conds": {k: cols[k]["values"][v] for k, v in zip(cs, vals)},
                             "cond_labels": {k: cols[k]["labels"][v] for k, v in zip(cs, vals)},
                             "sel": s, "test": stats(ret[jt], day[jt])})

    tried = len(rows)
    # the same trades reached through different labels (e.g. an "_F" model == base model + a flag) count once
    rows.sort(key=lambda r: len(r["conds"]))  # stable: fewer conditions first
    seen, distinct = {}, []
    for r in rows:
        t = r["test"] or {"n": 0, "sum_pp": 0.0}
        key = (r["sel"]["n"], round(r["sel"]["sum_pp"], 7), t["n"], round(t["sum_pp"], 7), r["sel"]["days"])
        if key in seen:
            seen[key]["aliases"] += 1
            continue
        r["aliases"] = 0
        seen[key] = r
        distinct.append(r)
    for r, q in zip(distinct, bh([1.0 if r["sel"]["p"] is None else r["sel"]["p"] for r in distinct])):
        r["sel"]["q"] = q
        t = r["test"]
        r["robust"] = bool(q < 0.05 and t and t["n"] >= min_test_n and t["mean"] > 0)

    def score(r):
        t = r["test"]["mean"] if r["test"] and r["test"]["n"] >= min_test_n else -math.inf
        return {"sel": -r["sel"]["mean"], "test": -t, "worst": -min(r["sel"]["mean"], t), "q": r["sel"]["q"]}[sort]
    distinct.sort(key=lambda r: (score(r), -r["sel"]["n"]))
    out_rows = distinct[:top]
    if holdout is not None:
        for r in out_rows:
            r["holdout"] = match(holdout, {**r["model"], **r["conds"]})
    sel_days = np.unique(day[part == 1]); test_days = np.unique(day[part == 2])
    rng = lambda d: [src["days"][int(d[0])], src["days"][int(d[-1])]] if len(d) else None
    return {"split": {**split, "select_range": rng(sel_days), "test_range": rng(test_days)},
            "model_keys": model_keys, "cond_keys": list(cond_keys), "skipped_columns": skipped, "max_conds": max_conds,
            "min_n": min_n, "min_days": min_days, "sort": sort, "tried": tried, "distinct": len(distinct),
            "significant": int(sum(r["sel"]["q"] < 0.05 for r in distinct)), "robust": int(sum(r["robust"] for r in distinct)),
            "rows": out_rows}


def match(src: dict, values: dict[str, str]) -> dict | None:
    """Stats of the same model/condition values on another source; None when a column is missing there."""
    cols = {c["key"]: c for c in src["columns"]}
    ret, day = np.asarray(src["ret"], float), np.asarray(src["day"])
    mask = np.ones(len(ret), bool)
    for k, v in values.items():
        c = cols.get(k)
        if c is None:
            return None
        if v not in c["values"]:
            return {"n": 0}
        mask &= np.asarray(c["codes"]) == c["values"].index(v)
    return stats(ret[mask], day[mask]) or {"n": 0}
