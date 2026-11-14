"""Combination lab data: compact columnar trade tables with condition columns.

Each lab *source* is a set of trades with categorical columns the user can filter on and group by
in the browser: model/rule dimensions (study dims, or the Arena run itself) plus a shared condition
library. Every condition is known before the entry decision (prior-day KOSDAQ regime, weekday,
entry time window, market, rule stage, daily rank); post-hoc information is not offered.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .store import OUT, OUTPUTS

KST = timezone(timedelta(hours=9))
CONDITIONS = OUT / "conditions"
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


# ---------- condition library ----------

def update_conditions() -> Path | None:
    """Copy the newest KOSDAQ regime table produced by research runs into the Arena condition cache."""
    found = sorted((OUTPUTS / "model-research").glob("*/*/kosdaq-gate.csv"), key=lambda p: p.stat().st_mtime)
    if not found:
        return None
    CONDITIONS.mkdir(parents=True, exist_ok=True)
    dest = CONDITIONS / "kosdaq.csv"
    shutil.copy2(found[-1], dest)
    return dest


def kosdaq_by_trade_day() -> pd.DataFrame | None:
    """For each trading day D: the regime known at the previous close S (no look-ahead)."""
    p = CONDITIONS / "kosdaq.csv"
    if not p.exists():
        return None
    k = pd.read_csv(p).sort_values("day").reset_index(drop=True)
    prev = k.shift(1)
    out = pd.DataFrame({"day": k["day"].astype(str)})
    out["regime_gate"] = prev["gate_ok"].map({True: "통과", False: "미통과", "True": "통과", "False": "미통과"}).fillna("자료 없음")
    above = prev["close"] > prev["ma20"]
    out["kosdaq_ma20"] = np.where(prev["ma20"].isna(), "자료 없음", np.where(above, "20일선 위", "20일선 아래"))
    r = prev["ret"]
    out["kosdaq_prev_ret"] = pd.cut(r, [-np.inf, -0.015, 0, 0.015, np.inf],
                                    labels=["−1.5% 이하", "−1.5~0%", "0~+1.5%", "+1.5% 이상"]).astype(str).replace("nan", "자료 없음")
    return out


CONDITION_LABELS = {
    "regime_gate": "전날 코스닥 국면 게이트", "kosdaq_ma20": "전날 코스닥 20일선", "kosdaq_prev_ret": "전날 코스닥 등락",
    "weekday": "요일", "month": "월", "entry_window": "진입 시간대", "market": "시장", "stage": "규칙 단계", "rank": "일봉 강도 순위",
}


def _window(ts) -> str:
    t = pd.Timestamp(ts)
    t = t.tz_convert(KST) if t.tzinfo else t
    m = t.hour * 60 + t.minute
    if m < 9 * 60:
        return "08:00–09:00 (장전)"
    if m <= 10 * 60 + 30:
        return "09:00–10:30"
    if m < 13 * 60:
        return "10:30–13:00"
    if m <= 14 * 60 + 30:
        return "13:00–14:30"
    return "14:30 이후"


# ---------- encoding ----------

def _encode(df: pd.DataFrame, columns: list[dict], day_col: str, ret_col: str, order_col: str, meta: dict) -> dict:
    df = df.sort_values([order_col]).reset_index(drop=True)
    days = sorted(df[day_col].astype(str).unique())
    day_idx = {d: i for i, d in enumerate(days)}
    cols = []
    for c in columns:
        v = df[c["key"]].astype(str)
        values = c.get("order") or sorted(v.unique(), key=lambda s: (s == "자료 없음", s))
        values = [x for x in values if x in set(v)] + [x for x in sorted(set(v)) if x not in (c.get("order") or [])]
        values = list(dict.fromkeys(values))
        code = {x: i for i, x in enumerate(values)}
        labels = c.get("labels") or {}
        cols.append({"key": c["key"], "label": c["label"], "role": c["role"], "values": values,
                     "labels": [labels.get(x, x) for x in values], "codes": v.map(code).astype(int).tolist()})
    return {**meta, "n": int(len(df)), "days": days, "day": df[day_col].astype(str).map(day_idx).astype(int).tolist(),
            "ret": np.round(df[ret_col].astype(float).to_numpy(), 8).tolist(), "columns": cols}


def _attach_regime(df: pd.DataFrame, day_col: str) -> list[dict]:
    k = kosdaq_by_trade_day()
    if k is None:
        return []
    m = df[[day_col]].astype(str).merge(k, left_on=day_col, right_on="day", how="left")
    added = []
    for c in ("regime_gate", "kosdaq_ma20", "kosdaq_prev_ret"):
        df[c] = m[c].fillna("자료 없음").to_numpy()
        added.append({"key": c, "label": CONDITION_LABELS[c], "role": "condition"})
    return added


def study_source(folder: Path, spec: dict) -> dict:
    from .studies import _entry_labels, _frame
    df = _frame(folder, spec)
    cols = []
    for d in spec["dimensions"]:
        labels = d.get("labels") or {}
        if d["key"] in ("variant", "entry_variant"):
            labels = _entry_labels(sorted(df[d["key"]].unique())) | labels
        cols.append({"key": d["key"], "label": d["label"], "role": "model" if d["key"] in ("variant", "entry_variant", "policy") else "condition",
                     "labels": labels})
    if not any(d["key"] == "gate_ok" for d in spec["dimensions"]):  # same definition already a study dimension
        cols += _attach_regime(df, "_day")
    else:
        cols += [c for c in _attach_regime(df, "_day") if c["key"] != "regime_gate"]
    df["weekday"] = pd.to_datetime(df["_day"]).dt.weekday.map(lambda i: WEEKDAYS[i])
    df["month"] = df["_day"].str[:7]
    cols += [{"key": "weekday", "label": "요일", "role": "condition", "order": WEEKDAYS[:5]},
             {"key": "month", "label": "월", "role": "condition"}]
    meta = {"source_id": f"study:{spec['study_id']}", "title": spec["title"], "kind": "study", "study_id": spec["study_id"],
            "locked_holdout": spec.get("locked_holdout", False)}
    return _encode(df, cols, "_day", spec["measure"]["return"], "_order", meta)


def runs_source(track_id: str, split: str, runs: list[tuple[dict, list]]) -> dict | None:
    rows = []
    for run, trades in runs:
        for t in trades:
            if not t.get("complete") or t.get("net_return") is None:
                continue
            ctx = t.get("ctx") or {}
            ent = pd.Timestamp(t["entry_at"])
            rows.append({"model": run["run_id"], "ret": t["net_return"], "order": t["entry_at"],
                         "day": (ent.tz_convert(KST) if ent.tzinfo else ent).strftime("%Y-%m-%d"),
                         "entry_window": _window(t["entry_at"]), "market": ctx.get("market", "자료 없음"),
                         "stage": ctx.get("stage", "자료 없음"), "rank": str(ctx.get("rank", "자료 없음"))})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    cols = [{"key": "model", "label": "모델 (run)", "role": "model"}]
    cols += _attach_regime(df, "day")
    df["weekday"] = pd.to_datetime(df["day"]).dt.weekday.map(lambda i: WEEKDAYS[i])
    cols += [{"key": "entry_window", "label": CONDITION_LABELS["entry_window"], "role": "condition",
              "order": ["08:00–09:00 (장전)", "09:00–10:30", "10:30–13:00", "13:00–14:30", "14:30 이후"]},
             {"key": "market", "label": "시장", "role": "condition"},
             {"key": "stage", "label": "규칙 단계", "role": "condition", "order": ["observing", "breakout", "pullback", "confirmed"],
              "labels": {"observing": "관찰", "breakout": "돌파", "pullback": "눌림", "confirmed": "재상승 확인"}},
             {"key": "rank", "label": "일봉 강도 순위", "role": "condition", "order": ["1", "2", "3"]},
             {"key": "weekday", "label": "요일", "role": "condition", "order": WEEKDAYS[:5]}]
    meta = {"source_id": f"runs:{track_id}:{split}", "title": f"Arena 모델 · {split}", "kind": "runs",
            "track_id": track_id, "split": split, "locked_holdout": split == "holdout"}
    return _encode(df, cols, "day", "ret", "order", meta)


def write_sources(out_dir: Path, sources: list[dict]) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for s in sources:
        fname = s["source_id"].replace(":", "__") + ".json"
        (out_dir / fname).write_text(json.dumps(s, ensure_ascii=False, separators=(",", ":")))
        index.append({"source_id": s["source_id"], "file": fname, "title": s["title"], "kind": s["kind"], "n": s["n"],
                      "locked_holdout": s["locked_holdout"], "first_day": s["days"][0], "last_day": s["days"][-1],
                      "columns": [{"key": c["key"], "label": c["label"], "role": c["role"], "n": len(c["values"])} for c in s["columns"]],
                      "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False))
    return index
