"""Studies: condition x model experiment grids (arena-study/v0).

A study is one experiment folder with a trade table whose columns include the condition dimensions
(entry rule, exit policy, time window, regime gate, population, period ...). The platform computes
the same metrics for every cell of the grid, plus "all" marginals for dimensions that partition the
same strategy's trades (e.g. gate on/off, pre/post), and corrects for the number of cells compared.

Experiment folders written by other sessions are read only. Their spec comes from a `study.json`
in the folder if present, else from a built-in adapter keyed by the folder name.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics_ext import bh_qvalues, day_bootstrap, deflated_sharpe, extended
from .store import OUT, now

STUDIES = OUT / "studies"
SCHEMA = "arena-study/v0"
TOL = 1e-9
ALL = "*"

ENTRY_LABELS = {
    "A_close": "S일 종가 매수 (참고)", "B_open": "D일 시가 매수 (참고)", "G1": "시가 이탈 후 회복",
    "M1": "뒤틀림 분류기", "M2": "shape_v2 규칙", "M1o": "분류기, 시가 위", "G1M1": "회복 후 분류기",
    "G1M2": "회복 후 shape_v2",
}
POLICY_LABELS = {
    "X0_base": "기본 (+0.5% 본절, 3%·5% 익절)", "X1_be_after_tp3": "3% 익절 후 본절", "X2_runner_trail": "3% 후 2.5% 추적",
    "X3_runner_bear": "3% 후 하락 뒤틀림 청산", "X4_trail_overnight": "추적 + 익일 보유", "X5_stop_only": "손절만",
}
SCOPE_LABELS = {"ALL": "전 시간대 첫 신호", "W1": "09:01–10:30", "W2": "13:01–14:30", "W3": "15:01–15:19 (당일 청산)",
                "W3_overnight": "15:01–15:19 (익일 보유)"}
PERIOD_LABELS = {"pre": "학습 전 (분류기 학습 구간, 참고)", "post": "학습 후"}


def _entry_labels(values):
    out = {}
    for v in values:
        base, filt = (v[:-2], True) if v.endswith("_F") else (v, False)
        out[v] = ENTRY_LABELS.get(base, base) + (" · 위험 표시 제외" if filt else "")
    return out


def _adapter(folder: Path) -> dict | None:
    kind, version = folder.parent.name, folder.name
    proto = json.loads((folder / "protocol.json").read_text()) if (folder / "protocol.json").exists() else {}
    common = {"schema": SCHEMA, "study_id": f"{kind}-{version}", "source": f"model-research/{kind}/{version}",
              "trades": "trades.parquet", "measure": {"return": "net", "date": "D", "symbol": "symbol",
              "entry": "entry", "stop": "stop", "order": ["D", "signal_minute"], "exit_reason": "reason",
              "filter": {"status": "ok"}}, "protocol_version": proto.get("version")}
    if kind == "entry-gate":
        return common | {
            "title": "진입 조건 × 뒤틀림 모델 + 국면 게이트",
            "purpose": "시가 이탈 후 회복(G1)과 뒤틀림 분류기(M1/M2)를 단독·결합으로 비교하고, 코스닥 국면 게이트 효과를 본다.",
            "dimensions": [
                {"key": "variant", "label": "진입 방식", "aggregate": False},
                {"key": "scope", "label": "시간대", "aggregate": False, "labels": SCOPE_LABELS},
                {"key": "gate_ok", "label": "국면 게이트", "aggregate": True, "labels": {"True": "국면 통과", "False": "국면 미통과"}},
                {"key": "pop", "label": "모집단", "aggregate": False, "labels": {"picks": "강사 추천", "watch": "관찰 명단"}},
                {"key": "period", "label": "기간", "aggregate": True, "labels": PERIOD_LABELS},
            ],
            "defaults": {"rows": "variant", "cols": "period", "fixed": {"pop": "picks", "scope": "ALL", "gate_ok": ALL}},
            "notes": proto.get("regime", {}).get("gate_ok"),
        }
    if kind in ("picks-runner", "holdout-picks"):
        dims = [{"key": "entry_variant", "label": "진입 방식", "aggregate": False},
                {"key": "policy", "label": "청산 정책", "aggregate": False, "labels": POLICY_LABELS},
                {"key": "risk_flag", "label": "위험 표시", "aggregate": True, "labels": {"True": "위험 표시 있음", "False": "위험 표시 없음"}}]
        if kind == "picks-runner":
            dims.append({"key": "period", "label": "기간", "aggregate": True, "labels": PERIOD_LABELS})
            return common | {
                "title": "진입 방식 × 청산 정책 (강사 추천 종목)",
                "purpose": "같은 진입에서 본절·추적·하락 뒤틀림·익일 보유 등 청산 정책을 바꿔 결과를 비교한다.",
                "dimensions": dims, "defaults": {"rows": "entry_variant", "cols": "policy", "fixed": {"risk_flag": ALL, "period": "post"}},
                "notes": proto.get("note"),
            }
        lock = json.loads((folder / "holdout-lock.json").read_text()) if (folder / "holdout-lock.json").exists() else {}
        return common | {
            "title": "잠금 시험: 진입 방식 × 청산 정책",
            "purpose": "결정에 쓰지 않도록 잠가 둔 기간에 사전 등록한 조합을 적용한 최종 시험.",
            "dimensions": dims, "defaults": {"rows": "entry_variant", "cols": "policy", "fixed": {"risk_flag": ALL}},
            "locked_holdout": True, "holdout": lock.get("period"), "primary": lock.get("primary"),
            "measure": common["measure"] | {"filter": {}},
        }
    return None


def load_spec(folder: Path) -> dict:
    folder = Path(folder)
    if (folder / "study.json").exists():
        spec = json.loads((folder / "study.json").read_text())
    else:
        spec = _adapter(folder)
    if not spec:
        raise SystemExit(f"{folder}: no study.json and no built-in adapter")
    return spec


def _frame(folder: Path, spec: dict) -> pd.DataFrame:
    m = spec["measure"]
    df = pd.read_parquet(folder / spec.get("trades", "trades.parquet"))
    for col, val in (m.get("filter") or {}).items():
        df = df[df[col] == val]
    df = df[np.isfinite(df[m["return"]].astype(float))].copy()
    for d in spec["dimensions"]:
        df[d["key"]] = df[d["key"]].astype(str)
    df["_day"] = df[m["date"]].astype(str).str[:10]
    order = [c for c in m.get("order", []) if c in df.columns]
    df["_order"] = df[order].astype(str).agg("|".join, axis=1) if order else df["_day"]
    if m.get("entry") in df.columns and m.get("stop") in df.columns:
        e, s = df[m["entry"]].astype(float), df[m["stop"]].astype(float)
        df["_risk"] = (e - s) / e
    return df


def cell_id(key: dict) -> str:
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def _cell_metrics(g: pd.DataFrame, spec: dict, calendar: np.ndarray) -> tuple[dict, dict]:
    m = spec["measure"]
    r = g[m["return"]].to_numpy(float)
    n = len(r)
    wins, losses = r[r > TOL], r[r < -TOL]
    days = g["_day"].to_numpy()
    lo, hi = days.min(), days.max()
    cal = calendar[(calendar >= lo) & (calendar <= hi)]
    by_day = pd.Series(r * 100).groupby(days).sum()
    daily = by_day.reindex(cal, fill_value=0.0).to_numpy()
    cum = np.cumsum(daily)
    peak = np.maximum.accumulate(np.maximum(cum, 0))
    sd = daily.std(ddof=1) if len(daily) > 1 else 0.0
    ext = extended(r, g["_order"].to_numpy(), days, daily, list(cal), n, tol=TOL,
                   symbols=g[m["symbol"]].to_numpy() if m.get("symbol") in g else None,
                   risk=g["_risk"].to_numpy() if "_risk" in g else None, active_days=len(by_day))
    ci = ext.pop("_ci95_np")
    srt = np.sort(r)
    metrics = {
        "n": n, "days": int(len(by_day)), "mean": float(r.mean()), "median": float(np.median(r)),
        "mean_ci95": ci, "accuracy": float((r >= -TOL).mean()),
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "sum_pp": float(r.sum() * 100), "max_drawdown_pp": float((cum - peak).min()) if len(cum) else None,
        "sharpe_daily_ann": float(daily.mean() / sd * np.sqrt(252)) if sd > 0 else None,
        "worst_trade": float(srt[0]), "best_trade": float(srt[-1]),
        **{k: v for k, v in ext.items() if k not in ("monthly_pp",)},
    }
    edges = [x / 100 for x in range(-4, 9)]
    hist = np.bincount(np.searchsorted(edges, r, side="right"), minlength=len(edges) + 1).tolist()
    detail = {"curve": [[d, round(float(c), 4)] for d, c in zip(cal, cum)], "monthly_pp": ext.get("monthly_pp", {}),
              "return_hist": {"edges": edges, "counts": hist}}
    if m.get("exit_reason") in g:
        detail["exit_reasons"] = g[m["exit_reason"]].astype(str).value_counts().to_dict()
    return metrics, detail


def build_study(folder: Path, out_dir: Path) -> dict:
    folder = Path(folder)
    spec = load_spec(folder)
    df = _frame(folder, spec)
    calendar = np.array(sorted(df["_day"].unique()))
    dims = [d["key"] for d in spec["dimensions"]]
    for d in spec["dimensions"]:
        d["values"] = sorted(df[d["key"]].unique().tolist())
        if d["key"] == "entry_variant" or d["key"] == "variant":
            d["labels"] = _entry_labels(d["values"]) | (d.get("labels") or {})
    agg_dims = [d["key"] for d in spec["dimensions"] if d.get("aggregate")]
    cells, details = [], {}
    for r_ in range(len(agg_dims) + 1):
        for collapsed in itertools.combinations(agg_dims, r_):
            keep = [k for k in dims if k not in collapsed]
            groups = df.groupby(keep, sort=True) if keep else [((), df)]
            for vals, g in groups:
                vals = vals if isinstance(vals, tuple) else (vals,)
                key = dict(zip(keep, vals)) | {k: ALL for k in collapsed}
                key = {k: key[k] for k in dims}
                cid = cell_id(key)
                metrics, detail = _cell_metrics(g, spec, calendar)
                cells.append({"id": cid, "key": key, "base": not collapsed, **metrics})
                details[cid] = detail
    # Multiple-testing family = every cell a user can look at (base cells and "all" marginals), which is
    # more conservative than base cells alone.
    base = [c for c in cells if c["base"]]
    q = bh_qvalues({c["id"]: c["p_value_mean_gt0"] for c in cells})
    srs = [c.get("sr_daily") for c in cells]
    for c in cells:
        c["q_value"] = q.get(c["id"])
        c["significant"] = bool(c["q_value"] is not None and c["q_value"] < 0.05 and c["mean"] > 0)
        c["deflated_sharpe"] = deflated_sharpe(c.get("sr_daily"), c.get("t_days"), c.get("skew_daily"), c.get("kurt_daily"), srs)
    best = max((c for c in base if c["n"] >= 30), key=lambda c: c["mean"], default=None)
    sid = spec["study_id"]
    summary = {
        **{k: v for k, v in spec.items() if k not in ("trades",)},
        "updated_at": datetime.fromtimestamp((folder / spec.get("trades", "trades.parquet")).stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "built_at": now(), "n_trades": int(len(df)), "n_days": int(len(calendar)),
        "first_day": calendar[0] if len(calendar) else None, "last_day": calendar[-1] if len(calendar) else None,
        "trials": len(cells), "base_cells": len(base), "significant_cells": sum(c["significant"] for c in cells),
        "best_cell": {"id": best["id"], "key": best["key"], "mean": best["mean"], "n": best["n"], "q_value": best["q_value"]} if best else None,
        "has_report": (folder / "REPORT.md").exists(),
        "cells": cells,
    }
    sdir = out_dir / sid
    if sdir.exists():
        shutil.rmtree(sdir)
    (sdir / "cells").mkdir(parents=True)
    (sdir / "study.json").write_text(json.dumps(summary, ensure_ascii=False, default=_json))
    for cid, det in details.items():
        (sdir / "cells" / f"{cid}.json").write_text(json.dumps(det, ensure_ascii=False, default=_json))
    if (folder / "REPORT.md").exists():
        shutil.copy(folder / "REPORT.md", sdir / "REPORT.md")
    return summary


def _json(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


# ---------- local cache of study sources (what `study add/submit/pull` manage) ----------

def add_local(folder: Path) -> Path:
    """Copy an experiment folder's study inputs into the local Arena cache (source stays untouched)."""
    folder = Path(folder).resolve()
    spec = load_spec(folder)
    dest = STUDIES / spec["study_id"]
    dest.mkdir(parents=True, exist_ok=True)
    for name in (spec.get("trades", "trades.parquet"), "protocol.json", "REPORT.md", "holdout-lock.json", "summary.csv"):
        if (folder / name).exists():
            shutil.copy2(folder / name, dest / name)
    (dest / "study.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    return dest


def build_all(out_dir: Path, sources: Path = STUDIES) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for folder in sorted(p for p in sources.iterdir() if p.is_dir()) if sources.exists() else []:
        s = build_study(folder, out_dir)
        index.append({k: s[k] for k in ("study_id", "title", "purpose", "updated_at", "built_at", "n_trades", "n_days",
                                         "first_day", "last_day", "trials", "significant_cells", "best_cell", "source")}
                     | {"locked_holdout": s.get("locked_holdout", False),
                        "dimensions": [{"key": d["key"], "label": d["label"], "n": len(d["values"])} for d in s["dimensions"]]})
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, default=_json))
    return index
