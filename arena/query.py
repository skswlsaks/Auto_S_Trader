"""Read-side access for agents (MCP) and scripts: the built dashboard data in the shared store,
plus the combination-lab computation in Python (same definitions as web/src/lib/engine.ts).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np

from .metrics_ext import day_bootstrap, random_percentile
from .store import SITE

TOL = 1e-9


class SharedData:
    """Reads site/data/* from ARENA_REMOTE (s3://... or a shared folder); falls back to the local build."""

    def __init__(self, remote: str | None = None, ttl: float = 60.0):
        self.remote = (remote or os.environ.get("ARENA_REMOTE") or "").rstrip("/") or None
        self.ttl, self._cache = ttl, {}

    @property
    def where(self) -> str:
        return self.remote or str(SITE)

    def _read(self, key: str) -> bytes:
        if self.remote and self.remote.startswith("s3://"):
            r = subprocess.run(["aws", "s3", "cp", f"{self.remote}/site/data/{key}", "-"], capture_output=True)
            if r.returncode:
                raise FileNotFoundError(f"{self.remote}/site/data/{key}: {r.stderr.decode().strip()[:200]}")
            return r.stdout
        base = Path(self.remote) / "site" / "data" if self.remote else SITE / "data"
        p = base / key
        if not p.exists():
            raise FileNotFoundError(str(p))
        return p.read_bytes()

    def json(self, key: str):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        val = json.loads(self._read(key))
        self._cache[key] = (time.time(), val)
        return val

    def index(self):
        return self.json("index.json")

    def run(self, run_id: str):
        try:
            return self.json(f"runs/{run_id}.json")
        except FileNotFoundError:
            raise FileNotFoundError(f"run '{run_id}' not found in {self.where}. Use arena_list_runs to see run_ids.") from None

    def study(self, study_id: str):
        try:
            return self.json(f"studies/{study_id}/study.json")
        except FileNotFoundError:
            raise FileNotFoundError(f"study '{study_id}' not found in {self.where}. Use arena_list_studies.") from None

    def lab(self, source_id: str):
        meta = next((s for s in self.index()["lab"] if s["source_id"] == source_id), None)
        if not meta:
            raise KeyError(f"unknown lab source '{source_id}'. Use arena_list_lab_sources.")
        return self.json(f"lab/{meta['file']}")


# ---------- combination lab (Python twin of the browser engine) ----------

def resolve_filters(src: dict, filters: dict | None) -> dict[str, list[int]]:
    """Map {column: [value or label, ...]} to code lists; unknown names raise with the valid options."""
    out = {}
    for key, wanted in (filters or {}).items():
        col = next((c for c in src["columns"] if c["key"] == key), None)
        if col is None:
            raise ValueError(f"unknown column '{key}'. columns: {[c['key'] for c in src['columns']]}")
        wanted = [wanted] if isinstance(wanted, (str, int, bool)) else wanted
        codes = []
        for w in wanted:
            w = str(w)
            if w in col["values"]:
                codes.append(col["values"].index(w))
            elif w in col["labels"]:
                codes.append(col["labels"].index(w))
            else:
                raise ValueError(f"'{w}' is not a value of {key}. values: {list(zip(col['values'], col['labels']))}")
        out[key] = sorted(set(codes))
    return out


def select(src: dict, codes: dict[str, list[int]]) -> np.ndarray:
    mask = np.ones(src["n"], bool)
    for c in src["columns"]:
        allowed = codes.get(c["key"])
        if allowed is not None and len(allowed) < len(c["values"]):
            mask &= np.isin(np.asarray(c["codes"]), allowed)
    return np.nonzero(mask)[0]


def lab_metrics(src: dict, idx: np.ndarray, bootstrap: bool = True) -> dict:
    r = np.asarray(src["ret"], float)[idx]
    d = np.asarray(src["day"])[idx]
    n = len(r)
    if not n:
        return {"n": 0}
    wins, losses = r[r > TOL], r[r < -TOL]
    lo, hi = d.min(), d.max()
    daily = np.bincount(d - lo, weights=r * 100, minlength=hi - lo + 1)
    cum = np.cumsum(daily)
    dd = cum - np.maximum.accumulate(np.maximum(cum, 0))
    sd = daily.std(ddof=1) if len(daily) > 1 else 0.0
    srt = np.sort(r)
    k = max(1, int(np.ceil(0.05 * n)))
    out = {"n": int(n), "days": int(len(np.unique(d))), "mean": float(r.mean()), "median": float(np.median(r)),
           "accuracy": float((r >= -TOL).mean()), "profit_rate": float((r > TOL).mean()), "profit_rate_01": float((r > 0.001).mean()),
           "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
           "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
           "sum_pp": float(r.sum() * 100), "cvar5": float(srt[:k].mean()), "max_drawdown_pp": float(dd.min()),
           "sharpe_daily_ann": float(daily.mean() / sd * np.sqrt(252)) if sd > 0 else None,
           "first_day": src["days"][lo], "last_day": src["days"][hi]}
    if bootstrap:
        ci, p = day_bootstrap(d, r)
        out["mean_ci95"], out["p_value_mean_gt0"] = ci, p
    return out


def lab_query(src: dict, filters: dict | None = None, group_by: list[str] | None = None) -> dict:
    codes = resolve_filters(src, filters)
    model_cols = [c["key"] for c in src["columns"] if c["role"] == "model"]
    for k in model_cols:  # one model value by default: mixing strategies is meaningless
        codes.setdefault(k, [0])
    sel = select(src, codes)
    base = select(src, {k: v for k, v in codes.items() if k in model_cols})
    conditioned = len(sel) != len(base)
    res = {"selection": lab_metrics(src, sel), "same_model_without_conditions": lab_metrics(src, base) if conditioned else None,
           "applied_filters": {c["key"]: [c["values"][i] for i in codes[c["key"]]] for c in src["columns"] if c["key"] in codes}}
    if conditioned and len(sel):
        res["random_selection_percentile"] = random_percentile(res["selection"]["mean"], np.asarray(src["ret"], float)[base], len(sel), draws=1000)
    if group_by:
        cols = [next(c for c in src["columns"] if c["key"] == g) for g in group_by]
        keys = np.stack([np.asarray(c["codes"])[sel] for c in cols], axis=1)
        groups = []
        for combo in np.unique(keys, axis=0):
            gi = sel[(keys == combo).all(1)]
            groups.append({"group": {c["key"]: c["labels"][v] for c, v in zip(cols, combo)}, **lab_metrics(src, gi, bootstrap=False)})
        res["groups"] = sorted(groups, key=lambda g: -g.get("mean", -9))
    return res
