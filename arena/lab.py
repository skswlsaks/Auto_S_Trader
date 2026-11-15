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
    """Refresh the Arena condition cache from local research outputs: the newest KOSDAQ regime table
    and the per-stock daily feature table. Without research outputs (CI) the pulled cache is kept."""
    update_stock_table()
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
# same wording as the reference-candle scanner (jongga_scan.mjs evaluate())
PICK_STATUS_LABELS = {"invalidated": "이탈(셋업 종료)", "waiting_point": "포인트 미형성", "point_today": "오늘 포인트 형성",
                      "waiting_breakout": "포인트 돌파 대기", "exit_today": "오늘 이탈", "entry": "진입 신호", "above_point": "포인트 위 유지"}
STAGE_LABELS = {"new_reference": "새 기준봉", "waiting_point": "포인트 미형성", "point_today": "오늘 포인트 형성", "point_entry": "진입 신호",
                "above_point": "포인트 위 유지", "waiting_breakout": "포인트 돌파 대기", "invalid_reference": "이탈(셋업 종료)"}

# One-paragraph explanation per condition column, shown behind the "?" icon in the lab and returned by MCP.
# S = the last trading day before the trade day D (the picks' 'S일'); everything here is known before entry.
_REF = ("기준봉 = 종가가 전날보다 +5.8% 이상 오른 양봉, 또는 고가가 전날 종가 +12% 이상이었던 음봉\n"
        "포인트 = 기준봉 이후 처음 기준가격(양봉은 종가, 음봉은 시가) 위로 마감한 봉\n"
        "시크릿선 = 양봉 종가에서 몸통의 1/3 아래 가격")
CONDITION_HELP = {
    "regime_gate": "S일 장 마감 기준 코스닥 지수 국면입니다. S일 코스닥 종가가 20일 이동평균 위이고 S일 코스닥 등락률이 −1.5%보다 높으면 '통과'입니다. D일 장 시작 전에 확정됩니다.",
    "gate_ok": "S일 장 마감 기준 코스닥 지수 국면입니다. S일 코스닥 종가가 20일 이동평균 위이고 S일 코스닥 등락률이 −1.5%보다 높으면 '국면 통과'입니다. D일 장 시작 전에 확정됩니다.",
    "kosdaq_ma20": "S일 코스닥 지수 종가가 코스닥 20일 이동평균 위인지 아래인지입니다.",
    "kosdaq_prev_ret": "S일 코스닥 지수의 하루 등락률 구간입니다.",
    "weekday": "진입일(D)의 요일입니다. 표본이 작아 우연한 차이가 잘 생기므로 탐색용으로만 보세요.",
    "month": "진입일(D)이 속한 달입니다. 시기별로 결과가 고른지 확인하는 용도이며, 다가올 달을 미리 고르는 조건으로는 쓸 수 없습니다.",
    "entry_window": "실제 진입 시각(KST)이 속한 시간대입니다. 08:00–09:00은 장전 시간외 거래입니다.",
    "market": "종목이 상장된 시장(코스피·코스닥)입니다.",
    "stage": ("우리 일간 스캔이 S일 종가로 매긴 기준봉 셋업 단계입니다.\n"
              "· 새 기준봉: S일 자체가 기준봉\n"
              "· 포인트 미형성: 기준봉 이후 아직 기준가격 위 마감이 없음\n"
              "· 오늘 포인트 형성: S일에 처음 기준가격 위로 마감\n"
              "· 포인트 돌파 대기: 포인트 형성 후 그 아래에 있음\n"
              "· 진입 신호: S일 종가가 포인트봉 종가를 돌파\n"
              "· 포인트 위 유지: 이미 포인트 위에서 유지 중\n"
              "· 이탈(셋업 종료): 기준봉 이후 시크릿선이나 20일선 아래로 마감한 적 있음\n\n" + _REF),
    "rank": ("S일 종가 기준 후보 종목의 일봉 강도 점수 순위입니다(1이 가장 강함). 상위 3개만 D일 진입 대상입니다.\n"
             "점수 = 20일 모멘텀 50% + 60일선의 20일 기울기 25% + 20일 ATR 15% + 20일 거래대금 10% (각각 그날 후보 안의 백분위)"),
    "s_chg": "S일 하루 등락률입니다. 추천 표에 값이 있으면 그 값을, 없으면 종목 일봉에서 계산합니다. +10~20%는 '위험 표시' 기준 중 하나입니다.",
    "ref_age": "S일 기준으로 가장 최근 기준봉이 며칠 전인지입니다. 당일(0일) = S일 자체가 기준봉이며 '위험 표시' 기준 중 하나입니다.\n\n" + _REF,
    "stop_dist": ("진입가 대비 최초 손절가까지의 거리, (진입가 − 손절가) / 진입가입니다. 진입 순간 확정됩니다.\n"
                  "· 종가·시가 매수: −3%\n"
                  "· 시가 이탈 후 회복: 시가 이후 최저가\n"
                  "· 모델 신호: 눌림 저점 − 0.1 ATR\n"
                  "모든 손절가는 진입가의 0.3~3% 아래로 제한합니다."),
    "pick_status": ("추천 종목이 S일 종가 기준으로 우리 기준봉 스캐너에서 어느 단계였는지입니다.\n"
                    "· 포인트 미형성: 기준봉 이후 아직 기준가격 위 마감이 없음\n"
                    "· 오늘 포인트 형성: S일에 처음 기준가격 위로 마감\n"
                    "· 포인트 돌파 대기: 포인트 형성 후 그 아래에 있음\n"
                    "· 진입 신호: S일 종가가 포인트봉 종가를 돌파\n"
                    "· 포인트 위 유지: 이미 포인트 위에서 유지 중\n"
                    "· 오늘 이탈: S일에 시크릿선이나 20일선 아래로 마감\n"
                    "· 이탈(셋업 종료): 그 전에 이미 이탈 ('위험 표시' 기준 중 하나)\n\n" + _REF),
    "vol_ratio": "S일 거래량을 S일까지 20거래일 거래량의 중앙값으로 나눈 값입니다. 1배보다 크면 평소보다 거래가 많았던 날입니다.",
    "ret5": "S일 종가를 5거래일 전 종가와 비교한 수익률입니다. 최근 단기 상승·하락 정도를 나타냅니다.",
    "ma20_dist": "S일 종가가 20일 이동평균(S일 포함)보다 몇 % 위·아래인지입니다. 클수록 단기 과열에 가깝습니다.",
    "hi60_dist": "S일 종가가 최근 60거래일 최고가보다 몇 % 아래인지입니다. −3% 이내는 신고가 근처입니다.",
    "turnover20": "S일까지 20거래일 일별 거래대금(종가 × 거래량)의 중앙값입니다. 유동성과 체결 가능 규모를 나타냅니다.",
    "atr14": "S일까지 14거래일의 (고가 − 저가) / 전날 종가 평균입니다. 종목이 하루에 보통 얼마나 움직이는지를 나타냅니다.",
    "scope": ("진입 신호를 찾은 시간대입니다(신호 봉이 끝난 시각, KST). 신호 다음 봉 시가에 체결합니다.\n"
              "· 전 시간대 첫 신호: W1~W3 중 가장 먼저 난 신호\n"
              "· 익일 보유: 남은 물량을 다음 거래일 15:20까지 보유"),
    "pop": "거래 대상 종목 집합입니다.\n· 강사 추천: s_trader91 추천 글의 종목\n· 관찰 명단: 우리 일간 스캔이 고른 전체 시장 관찰 종목",
    "period": ("분류기 학습 구간과의 관계입니다.\n"
               "· 학습 전: D가 2026-08-26 이전. 뒤틀림 분류기(M1 계열)가 학습에 쓴 기간이라 그 모델의 결과가 실제보다 좋게 나올 수 있습니다.\n"
               "· 학습 후: 2026-08-27 이후"),
    "risk_flag": ("아래 중 하나라도 해당하면 '위험 표시 있음'입니다. 모두 S일 종가로 확정됩니다.\n"
                  "· S일 +10~20% 급등\n· S일 자체가 기준봉(경과 0일)\n· 추천 상태 이탈(셋업 종료)"),
    "kind": ("추천 글이 올라온 시점입니다.\n"
             "· after: 전날 장 마감 후(저녁) 게시. D일 08:01 장전 봉부터 진입 가능\n"
             "· pre: D일 아침 장 시작 전 게시"),
}
# The Arena runs' `stage` is an intraday rule stage, not the daily scanner stage above.
RUN_STAGE_HELP = ("진입 신호가 난 분봉에서 구조 규칙(shape_v2)이 어느 단계였는지입니다.\n"
                  "· 관찰: 진행 중인 구조 없음\n"
                  "· 돌파: 두 고점을 이은 저항선을 종가로 돌파\n"
                  "· 눌림: 돌파 뒤 저가가 낮아지며 되밀림\n"
                  "· 재상승 확인: 눌림 저점이 기준 저점보다 높은 상태에서 직전 봉 고가와 저항선을 다시 넘음")
STALE_DAYS = 7  # the last daily bar must be at most this many calendar days before the trade day


def candle_dirs() -> list[Path]:
    """Daily-candle folders written by research runs, highest priority first: the shared collection,
    then per-experiment copies (newest first). All use the same `<symbol>-1d-{raw,adjusted}.parquet` format."""
    root = OUTPUTS / "model-research"
    extra = [p for pat in ("*/*/data/candles", "*/*/daily") for p in root.glob(pat) if p.is_dir()]
    return [root / "data" / "candles"] + sorted(extra, key=lambda p: p.stat().st_mtime, reverse=True)


def _bucket(values, edges, labels) -> np.ndarray:
    # round first: the shared table stores float32, and values sitting exactly on an edge (e.g. a close
    # exactly 10% under the 60-day high) must land in the same bucket whichever path computed them
    v = pd.to_numeric(pd.Series(values), errors="coerce").astype(float).round(6)
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


FEATURE_COLS = ["day_ret"] + [k for k, *_ in DAILY_BUCKETS]
STOCK_TABLE = CONDITIONS / "stock-daily.parquet"
STOCK_TABLE_VERSION = 1  # bump when daily_features changes so local builds recompute the table
TABLE_START = "2025-09-01"  # earliest feature day kept in the shared table (dev starts 2025-10-29)


def _candle_files(dirs: list[Path]) -> list[Path]:
    return [p for d in dirs if d.is_dir() for p in d.glob("*-1d-*.parquet")]


def update_stock_table(dirs: list[Path] | None = None, dest: Path | None = None, force: bool = False) -> Path | None:
    """Precompute daily features for every symbol in the local candle folders into the condition cache,
    so builds without the candles (CI) produce the same condition columns. `arena publish` uploads it.

    No-op without candles, or when the table is current (same version, newer than every candle file).
    """
    dirs, dest = dirs or candle_dirs(), dest or STOCK_TABLE
    files = _candle_files(dirs)
    if not files:
        return None
    meta_path = dest.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if (not force and dest.exists() and meta.get("version") == STOCK_TABLE_VERSION
            and dest.stat().st_mtime >= max(p.stat().st_mtime for p in files)):
        return dest
    frames = []
    for sym in sorted({p.name.split("-1d-")[0] for p in files}):
        f = daily_features(sym, dirs)
        if f is None:
            continue
        f = f[f.index >= pd.Timestamp(TABLE_START)].dropna(how="all")
        frames.append(f.astype("float32").rename_axis("day").reset_index().assign(symbol=sym))
    if not frames:
        return None
    table = pd.concat(frames, ignore_index=True)[["symbol", "day", *FEATURE_COLS]]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    table.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(dest)
    meta_path.write_text(json.dumps({"version": STOCK_TABLE_VERSION, "rows": int(len(table)),
                                     "symbols": int(table["symbol"].nunique()), "first_day": str(table["day"].min().date()),
                                     "last_day": str(table["day"].max().date()), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "sources": [str(d.relative_to(OUTPUTS)) if d.is_relative_to(OUTPUTS) else str(d) for d in dirs if d.is_dir()]},
                                    ensure_ascii=False, indent=2))
    return dest


_TABLE_CACHE: dict = {}


def _read_table(path: Path) -> pd.DataFrame:
    key = (str(path), path.stat().st_mtime)
    if key not in _TABLE_CACHE:
        _TABLE_CACHE.clear()
        t = pd.read_parquet(path)
        _TABLE_CACHE[key] = t.assign(symbol=t["symbol"].astype(str), day=pd.to_datetime(t["day"]))
    return _TABLE_CACHE[key]


def _feature_rows(symbols, folder: Path | list[Path] | None, table: Path | None) -> pd.DataFrame | None:
    """Daily feature rows for `symbols`: from candle folders when given, else the shared table,
    else the local candle folders."""
    table = table or STOCK_TABLE
    if folder is None and table.exists():
        t = _read_table(table)
        return t[t["symbol"].isin(set(symbols))]
    feats = []
    for sym in symbols:
        f = daily_features(sym, folder)
        if f is not None:
            feats.append(f.rename_axis("day").reset_index().assign(symbol=sym))
    return pd.concat(feats, ignore_index=True) if feats else None


def features_before(df: pd.DataFrame, day_col: str, symbol_col: str, folder: Path | list[Path] | None = None,
                    table: Path | None = None) -> pd.DataFrame:
    """For each row: the daily features of the last bar strictly before its trade day (= S, no look-ahead)."""
    cols = FEATURE_COLS
    keys = pd.DataFrame({"symbol": df[symbol_col].astype(str).to_numpy(),
                         "day": pd.to_datetime(df[day_col].astype(str).str[:10]).astype("datetime64[ns]").to_numpy(), "row": np.arange(len(df))})
    right = _feature_rows(keys["symbol"].unique(), folder, table)
    if right is None or right.empty:
        return pd.DataFrame(np.nan, index=df.index, columns=cols)
    right = right.assign(day=pd.to_datetime(right["day"]).astype("datetime64[ns]")).sort_values("day")
    m = pd.merge_asof(keys.sort_values("day"), right, on="day", by="symbol", allow_exact_matches=False,
                      tolerance=pd.Timedelta(days=STALE_DAYS), direction="backward")
    m = m.sort_values("row")
    return pd.DataFrame(m[cols].to_numpy(float), index=df.index, columns=cols)


def _attach_stock(df: pd.DataFrame, day_col: str, symbol_col: str | None, skip=frozenset(),
                  folder: Path | list[Path] | None = None, table: Path | None = None) -> list[dict]:
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

    feats = features_before(df, day_col, symbol_col, folder, table) if symbol_col and symbol_col in df else None
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
        help_ = c.get("help") or (CONDITION_HELP.get(c["key"]) if c["role"] == "condition" else None)
        cols.append({"key": c["key"], "label": c["label"], "role": c["role"], **({"help": help_} if help_ else {}), "values": values,
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
                     "labels": labels, **({"help": d["help"]} if d.get("help") else {})})
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
             {"key": "stage", "label": "규칙 단계", "role": "condition", "help": RUN_STAGE_HELP, "order": ["observing", "breakout", "pullback", "confirmed"],
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
