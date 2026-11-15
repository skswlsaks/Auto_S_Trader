"""Combination lab data: compact columnar trade tables with condition columns.

Each lab *source* is a set of trades with categorical columns the user can filter on and group by
in the browser: model/rule dimensions (study dims, or the Arena run itself) plus a shared condition
library. Every condition is known before the entry decision (prior-day KOSDAQ regime, weekday,
entry time window, market, rule stage, daily rank, pick metadata, and the stock's own daily bars up to
the previous close S); post-hoc information is not offered.
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

MISSING = "자료 없음"


# ---------- stock-level conditions ----------
# Every bucket is a fixed threshold (not a quantile of the evaluated trades) so the same label means
# the same thing in every source and the boundaries carry no information from the evaluation period.
# (key, label, edges, bucket labels); buckets are left-closed [a, b).
PICK_BUCKETS = [
    ("s_chg", "S일 등락", [-np.inf, 0, .05, .10, .20, np.inf], ["0% 미만", "0~+5%", "+5~10%", "+10~20%", "+20% 이상"]),
    ("ref_age", "기준봉 경과일", [-np.inf, 1, 4, 8, np.inf], ["당일(0일)", "1~3일", "4~7일", "8일 이상"]),
    ("stop_dist", "손절 거리", [-np.inf, .02, .03, .04, np.inf], ["2% 미만", "2~3%", "3~4%", "4% 이상"]),
]
DAILY_BUCKETS = [
    ("vol_ratio", "S일 거래량 / 20일 중앙값", [-np.inf, .7, 1.5, 3, np.inf], ["0.7배 미만", "0.7~1.5배", "1.5~3배", "3배 이상"]),
    ("ret5", "S일까지 5일 수익률", [-np.inf, 0, .10, .20, np.inf], ["0% 미만", "0~+10%", "+10~20%", "+20% 이상"]),
    ("ma20_dist", "S일 종가 / 20일선", [-np.inf, 0, .10, .20, np.inf], ["20일선 아래", "20일선 0~+10%", "20일선 +10~20%", "20일선 +20% 이상"]),
    ("hi60_dist", "S일 종가 / 60일 고가", [-np.inf, -.20, -.10, -.03, np.inf], ["고가 −20% 미만", "고가 −20~−10%", "고가 −10~−3%", "고가 −3% 이내"]),
    ("turnover20", "20일 중앙 거래대금", [-np.inf, 1e10, 3e10, 1e11, np.inf], ["100억 미만", "100~300억", "300~1000억", "1000억 이상"]),
    ("atr14", "14일 평균 일중 변동폭", [-np.inf, .07, .10, .13, np.inf], ["7% 미만", "7~10%", "10~13%", "13% 이상"]),
]
PICK_STATUS_LABELS = {"invalidated": "무효", "waiting_point": "타점 대기", "point_today": "당일 타점", "waiting_breakout": "돌파 대기",
                      "exit_today": "당일 청산", "entry": "진입", "above_point": "타점 위"}
STAGE_LABELS = {"new_reference": "새 기준봉", "waiting_point": "타점 대기", "point_today": "당일 타점", "point_entry": "타점 진입",
                "above_point": "타점 위", "waiting_breakout": "돌파 대기", "invalid_reference": "기준봉 무효"}
STALE_DAYS = 7  # the last daily bar must be at most this many calendar days before the trade day


def candle_dirs() -> list[Path]:
    """Daily-candle folders written by research runs, highest priority first: the shared collection,
    then per-experiment copies (newest first). All use the same `<symbol>-1d-{raw,adjusted}.parquet` format."""
    root = OUTPUTS / "model-research"
    extra = [p for pat in ("*/*/data/candles", "*/*/daily") for p in root.glob(pat) if p.is_dir()]
    return [root / "data" / "candles"] + sorted(extra, key=lambda p: p.stat().st_mtime, reverse=True)


def _bucket(values, edges, labels) -> np.ndarray:
    v = pd.to_numeric(pd.Series(values), errors="coerce")
    out = pd.cut(v, edges, labels=labels, right=False).astype(object)
    return out.where(out.notna(), MISSING).astype(str).to_numpy()


def _clean(s: pd.Series) -> np.ndarray:
    s = s.astype(object)
    return s.where(s.notna() & ~s.astype(str).isin(["None", "nan", ""]), MISSING).astype(str).to_numpy()


def _read_daily(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    f = pd.read_parquet(path)
    ts = pd.to_datetime(f["timestamp"])
    if ts.dt.tz is not None:  # bars are stamped at KST midnight and stored in UTC (15:00 the day before)
        ts = ts.dt.tz_convert(KST).dt.tz_localize(None)
    f = f.assign(day=ts.dt.normalize()).drop_duplicates("day", keep="last")
    return f.sort_values("day").set_index("day")


def daily_features(symbol: str, folder: Path | list[Path] | None = None) -> pd.DataFrame | None:
    """Per-day features computed from that day's close and earlier bars only.

    Raw bars are merged across folders (earlier folders win on the same day). Price ratios use the
    adjusted series of the first folder that has one (backward adjustment scales a whole window
    equally, so it adds no look-ahead to ratios); volume and turnover use raw values.
    """
    folders = [folder] if isinstance(folder, Path) else (folder or candle_dirs())
    raws = [r for r in (_read_daily(f / f"{symbol}-1d-raw.parquet") for f in folders) if r is not None and not r.empty]
    if not raws:
        return None
    raw = pd.concat(raws)
    raw = raw[~raw.index.duplicated(keep="first")].sort_index()
    adj = next((a for a in (_read_daily(f / f"{symbol}-1d-adjusted.parquet") for f in folders) if a is not None), None)
    px = raw[["open", "high", "low", "close"]]
    if adj is not None:
        px = adj[["open", "high", "low", "close"]].reindex(raw.index).fillna(px)
    c = px["close"]
    out = pd.DataFrame(index=raw.index)
    out["day_ret"] = c / c.shift(1) - 1
    out["vol_ratio"] = raw["volume"] / raw["volume"].rolling(20).median()
    out["ret5"] = c / c.shift(5) - 1
    out["ma20_dist"] = c / c.rolling(20).mean() - 1
    out["hi60_dist"] = c / px["high"].rolling(60).max() - 1
    out["turnover20"] = (raw["close"] * raw["volume"]).rolling(20).median()
    out["atr14"] = ((px["high"] - px["low"]) / c.shift(1)).rolling(14).mean()
    return out.replace([np.inf, -np.inf], np.nan)


def features_before(df: pd.DataFrame, day_col: str, symbol_col: str, folder: Path | None = None) -> pd.DataFrame:
    """For each row: the daily features of the last bar strictly before its trade day (= S, no look-ahead)."""
    cols = ["day_ret"] + [k for k, *_ in DAILY_BUCKETS]
    keys = pd.DataFrame({"symbol": df[symbol_col].astype(str).to_numpy(),
                         "day": pd.to_datetime(df[day_col].astype(str).str[:10]).to_numpy(), "row": np.arange(len(df))})
    feats = []
    for sym in keys["symbol"].unique():
        f = daily_features(sym, folder)
        if f is not None:
            feats.append(f.rename_axis("day").reset_index().assign(symbol=sym))
    if not feats:
        return pd.DataFrame(np.nan, index=df.index, columns=cols)
    right = pd.concat(feats, ignore_index=True).sort_values("day")
    m = pd.merge_asof(keys.sort_values("day"), right, on="day", by="symbol", allow_exact_matches=False,
                      tolerance=pd.Timedelta(days=STALE_DAYS), direction="backward")
    m = m.sort_values("row")
    return pd.DataFrame(m[cols].to_numpy(float), index=df.index, columns=cols)


def _attach_stock(df: pd.DataFrame, day_col: str, symbol_col: str | None, skip=frozenset(),
                  folder: Path | None = None) -> list[dict]:
    """Stock-level conditions known before entry: pick metadata already in the trade table plus
    features of the stock's own daily bars up to S. Columns with no usable data are not added."""
    added: list[dict] = []

    def add(key, label, values, order=None, labels=None):
        if key in skip or (np.asarray(values) == MISSING).all():
            return
        df[key] = values
        order = order if order is not None else sorted(set(values) - {MISSING})
        added.append({"key": key, "label": label, "role": "condition", "order": order + [MISSING],
                      **({"labels": labels} if labels else {})})

    feats = features_before(df, day_col, symbol_col, folder) if symbol_col and symbol_col in df else None
    chg = pd.to_numeric(df["chg1"], errors="coerce") if "chg1" in df else pd.Series(np.nan, index=df.index)
    if feats is not None:  # S-day change from the pick table, else from the stock's own S bar
        chg = chg.fillna(feats["day_ret"])
    raw = {"s_chg": chg, "ref_age": df.get("ref_age"), "stop_dist": df.get("_risk")}
    for key, label, edges, names in PICK_BUCKETS:
        if raw[key] is not None:
            add(key, label, _bucket(raw[key].to_numpy(), edges, names), names)
    if "pick_status" in df:
        add("pick_status", "추천 상태", _clean(df["pick_status"]), list(PICK_STATUS_LABELS), PICK_STATUS_LABELS)
    if "stage" in df:
        add("stage", CONDITION_LABELS["stage"], _clean(df["stage"]), list(STAGE_LABELS), STAGE_LABELS)
    if "market" in df:
        add("market", CONDITION_LABELS["market"], _clean(df["market"]))
    if feats is not None:
        for key, label, edges, names in DAILY_BUCKETS:
            add(key, label, _bucket(feats[key].to_numpy(), edges, names), names)
    return added


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
    cols += _attach_stock(df, "_day", spec["measure"].get("symbol"), skip={c["key"] for c in cols})
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
            rows.append({"model": run["run_id"], "symbol": t.get("symbol"), "ret": t["net_return"], "order": t["entry_at"],
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
    cols += _attach_stock(df, "day", "symbol", skip={c["key"] for c in cols})
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
