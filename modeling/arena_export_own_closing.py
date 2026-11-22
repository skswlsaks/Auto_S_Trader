"""Export the 2026-11-15 own-closing experiments as Arena study folders (source folders are not modified).

Writes outputs/model-research/arena-export/<study_id>/{trades.parquet, study.json, protocol.json, REPORT.md}.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from . import own_closing_exit as ex
from . import own_closing_gapdown as gd
from . import own_closing_rank as rk
from . import own_closing_study as oc

MR = oc.MR
EXPORT = MR/"arena-export"
OC = MR/"own-closing"
NOTE = ("탐색 결과: 조합 선택과 비교 모두 이미 본 기간(2025-04 ~ 2026-11-12)에서 했습니다. 독립 시험이 아닙니다. "
        "S일 종가 매수 → D일(다음 거래일) 매도, 비용 0.25% 차감.")
PERIOD = {"key": "period", "label": "기간", "aggregate": True,
          "labels": {"train": "학습 2025-04~12", "valid": "검증 2026-01~06", "eval": "평가 2026-07~10/27",
                     "final": "최종 10/28~11/12"}}
FAM = {
    "trend": {"ma20_up": "20일선 위·상승", "align": "정배열(5>20>60)", "ma60_up": "60일선 위·상승", "below_ma20": "20일선 아래"},
    "mom20": {"neg": "20일 수익률 음수", "0-10": "20일 수익률 0~10%", "10-30": "20일 수익률 10~30%", "30+": "20일 수익률 30%+"},
    "high60": {"near": "60일 고점 −5% 이내", "mid": "60일 고점 −5~−15%", "deep": "60일 고점 −15% 이하"},
    "day": {"dn5-0": "S일 −5~0%", "0-3": "S일 0~3%", "3-7": "S일 +3~7%", "7-15": "S일 +7~15%", "15+": "S일 +15% 이상"},
    "shape": {"strong_close": "강한 종가", "upper_wick": "윗꼬리 50%+", "weak_close": "약한 종가"},
    "volume": {"<0.7": "거래대금 0.7배 미만", "0.7-1.5": "거래대금 0.7~1.5배", "1.5-3": "거래대금 1.5~3배", "3+": "거래대금 3배+"},
    "ref": {"today": "기준봉 당일", "1-3": "기준봉 1~3일 전", "4-10": "기준봉 4~10일 전", "none": "기준봉 없음"},
    "scan": {"watch": "관찰 명단", "point": "포인트 돌파·위", "waiting": "포인트·돌파 대기", "invalid": "스캔 이탈"},
    "market": {"gate_ok": "코스닥 게이트 통과", "kq_up": "코스닥 상승일", "breadth_hi": "시장 폭 50%+", "kq_weak": "코스닥 20일선 아래"},
    "size": {"small": "거래대금 100억 미만", "mid": "거래대금 100~500억", "large": "거래대금 500억+"},
}
EXIT_KO = {"E_open": "D 시가 매도", "E_close": "D 종가 매도", "TP3": "+3% 지정가(미체결 종가)",
           "TP5": "+5% 지정가(미체결 종가)", "O2": "3%/5% 분할 + −3% 손절"}


def ko(cell):
    return " + ".join(FAM[a][b] for a, b in (t.split("=", 1) for t in cell.split(" & ")))


def iso(d):
    d = str(d)
    return d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:]}"


def write(study_id, title, purpose, src, df, dims, defaults, extra_reason=True):
    out = EXPORT/study_id
    out.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df["status"] = "ok"
    df["D"] = df["D"].map(iso)
    for d in dims:
        df[d["key"]] = df[d["key"]].astype(str)
    df.to_parquet(out/"trades.parquet", index=False)
    spec = {"schema": "arena-study/v0", "study_id": study_id, "source": f"model-research/own-closing/{src.name}",
            "trades": "trades.parquet",
            "measure": {"return": "net", "date": "D", "symbol": "symbol", "order": ["D"],
                        **({"exit_reason": "reason"} if extra_reason and "reason" in df else {}), "filter": {"status": "ok"}},
            "title": title, "purpose": purpose, "dimensions": dims, "defaults": defaults, "locked_holdout": False,
            "notes": NOTE}
    (out/"study.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    for name in ("protocol.json", "REPORT.md"):
        if (src/name).exists():
            shutil.copy2(src/name, out/name)
    print(study_id, len(df), "rows")
    return out


def strategies():
    f = pd.read_parquet(OC/"20261115-v1/panel.parquet")
    F = oc.families(f)
    lock = json.loads((OC/"20261115-v1/lock-v2.json").read_text())
    sets = {}
    for i, c in enumerate(lock["singles"], 1):
        sets[f"C{i}"] = (oc.select(f, F, c, oc.TOP_K), f"칸{i}: {ko(c)} (하루 3종목)")
    sets["MY3"] = (oc.composite(f, F, lock["survivors"]), "MY3: 통과 22칸 결합, 하루 3종목")
    sel = pd.read_parquet(OC/"20261115-v1/selections.parquet")
    sets["LGBM"] = (sel[sel.set == "lgbm"], "LightGBM 상위 3종목")
    risk = ((f.chg1 > .10) & (f.chg1 <= .20)) | (f.ref_age == 0) | (f.status == "invalidated")
    sets["PICKS_F"] = (f[f.U & f.pick & ~risk], "참고: 강사 픽(위험 표시 제외)")
    rows = []
    for k, (g, _) in sets.items():
        g = g[g.period.notna()]
        for e in ("E_open", "O2", "E_close"):
            rows.append(pd.DataFrame(dict(strategy=k, exit=e, symbol=g.code.to_numpy(), S=g.S.to_numpy(), D=g.D.to_numpy(),
                                          period=g.period.to_numpy(), net=g[e].to_numpy(float))))
    df = pd.concat(rows, ignore_index=True)
    dims = [{"key": "strategy", "label": "전략", "aggregate": False, "labels": {k: v[1] for k, v in sets.items()}},
            {"key": "exit", "label": "다음날 매도", "aggregate": False, "labels": {k: EXIT_KO[k] for k in ("E_open", "O2", "E_close")}},
            PERIOD]
    write("own-closing-20261115-v1", "나만의 종가베팅: 조건 결합 MY3 (평가 기간 실패)",
          "강사 규칙 재현 후 조건 10가족 5,523조합을 학습·검증으로 고르고 평가 기간을 한 번 열었다. MY3·상위 5칸·LightGBM·강사 픽 비교.",
          OC/"20261115-v1", df, dims, {"rows": "strategy", "cols": "exit", "fixed": {"period": "eval"}})


def ranking():
    f = pd.read_parquet(OC/"20261115-v1/panel.parquet")
    f = rk.add_exits(f)
    F = oc.families(f)
    cand = pd.read_csv(OC/"20261115-rank-v1/top3-candidates.csv")
    cells = list(dict.fromkeys(cand.cell))
    rows, labels = [], {}
    for i, c in enumerate(cells, 1):
        g = oc.select(f, F, c, oc.TOP_K)
        g = g[g.period.notna()]
        key = f"K{i:02d}"
        labels[key] = ko(c)
        for e in rk.EXITS:
            rows.append(pd.DataFrame(dict(combo=key, exit=e, symbol=g.code.to_numpy(), S=g.S.to_numpy(), D=g.D.to_numpy(),
                                          period=g.period.to_numpy(), net=g[e].to_numpy(float))))
    df = pd.concat(rows, ignore_index=True)
    dims = [{"key": "combo", "label": "조건 조합", "aggregate": False, "labels": labels},
            {"key": "exit", "label": "다음날 매도", "aggregate": False, "labels": EXIT_KO}, PERIOD]
    write("own-closing-rank-20261115-v1", "우리 조건 결합 종가베팅 — 상위 후보 × 매도 방식",
          "6,275조합 중 하루 3종목 기준 세 기간 모두 양수였던 후보 조합을 매도 방식 5종으로 비교 (순위는 이미 본 기간 기준).",
          OC/"20261115-rank-v1", df, dims, {"rows": "combo", "cols": "exit", "fixed": {"period": "*"}})


def exit_v1():
    t = pd.read_parquet(OC/"20261115-exit-v1/results.parquet").rename(columns={"code": "symbol"})
    dims = [{"key": "set", "label": "조합", "aggregate": False, "labels": {k: v[1] for k, v in ex.SETS.items()}},
            {"key": "policy", "label": "청산", "aggregate": False, "labels": {
                "TP3": "+3% 지정가", "TP5": "+5% 지정가", "X3": "+3% 35% 후 하락 뒤틀림", "BEAR": "하락 뒤틀림 전량(−3% 손절)",
                "BEAR_NS": "하락 뒤틀림 전량(손절 없음)", "TP5_H": "+5% 지정가 + 추세 연장", "X3_H": "X3 + 추세 연장",
                "BEAR_H": "뒤틀림(손절) + 추세 연장", "BEAR_NS_H": "뒤틀림(무손절) + 추세 연장"}}, PERIOD]
    write("own-closing-exit-20261115-v1", "종가베팅 청산: 고정 +3/+5% vs 하락 뒤틀림 모델, 추세 연장",
          "상위 4조합 거래를 1분봉(D~D+3)으로 재생해 고정 지정가, 하락 뒤틀림 청산, 추세 상방 시 최대 4거래일 연장을 비교.",
          OC/"20261115-exit-v1", t, dims, {"rows": "set", "cols": "policy", "fixed": {"period": "*"}})


def exit_v2():
    t = pd.read_parquet(OC/"20261115-exit-v2/results.parquet").rename(columns={"code": "symbol"})
    tin = pd.read_parquet(ex.OUT/"trades-in.parquet")[["set", "code", "S", "D"]].drop_duplicates().rename(columns={"code": "symbol"})
    t = t.merge(tin, on=["set", "symbol", "S"], how="left")
    t[["split", "cut", "hold"]] = t.policy.str.split("|", expand=True)
    dims = [{"key": "set", "label": "조합", "aggregate": False, "labels": {k: v[1] for k, v in ex.SETS.items()}},
            {"key": "split", "label": "부분 매도", "aggregate": False,
             "labels": {"TP5": "+5% 전량", "P35": "+3% 35%, +5% 35%, 나머지 종가", "P50": "+3% 50%, +5% 50%"}},
            {"key": "cut", "label": "시가 기준 정리", "aggregate": False,
             "labels": {"none": "없음", "GO": "갭하락이면 시가 전량 매도", "OS1": "시가 −1% 손절", "GO+OS1": "갭하락 정리 + 시가 손절"}},
            {"key": "hold", "label": "보유", "aggregate": False, "labels": {"1d": "D일만", "H": "추세 상방이면 최대 4거래일"}},
            PERIOD]
    write("own-closing-exit-20261115-v2", "종가베팅 청산: 부분 매도 × 갭하락 정리 × 추세 연장",
          "같은 거래에 부분 매도 3종, 시가 기준 정리 4종, 연장 여부 2종(24규칙)을 적용. 급등주형은 갭하락 즉시 정리가 손해, 대형 윗꼬리형은 이득.",
          OC/"20261115-exit-v2", t.drop(columns=["policy"]), dims,
          {"rows": "cut", "cols": "split", "fixed": {"set": "R1", "hold": "H", "period": "*"}})


def gapdown():
    g = pd.read_parquet(OC/"20261115-gapdown-v1/gapdown.parquet")
    tin = pd.read_parquet(ex.OUT/"trades-in.parquet")[["set", "code", "S", "D"]].drop_duplicates()
    g = g.merge(tin, on=["set", "code", "S"], how="left")
    g["gapb"] = pd.cut(g.gap, [-1, -.04, -.02, 0], labels=["lt4", "2to4", "0to2"]).astype(str)
    cols = list(gd.TIMES)+["CLOSE", "BE", "BE_T1100", "OPEN2", "ORH", "ORL", "ORH_ORL", "P50"]
    df = g.melt(id_vars=["set", "code", "S", "D", "period", "gapb"], value_vars=cols, var_name="rule", value_name="net")
    df = df.rename(columns={"code": "symbol"})
    dims = [{"key": "set", "label": "조합", "aggregate": False, "labels": {k: v[1] for k, v in ex.SETS.items()}},
            {"key": "rule", "label": "매도 시점·방식", "aggregate": False, "labels": {
                "T0901": "09:01 시가", "T0930": "09:30", "T1000": "10:00", "T1100": "11:00", "T1300": "13:00",
                "T1420": "14:20", "CLOSE": "15:20 종가", "BE": "본전 지정가(미체결 종가)", "BE_T1100": "11시까지 본전 대기",
                "OPEN2": "시가 +2% 지정가", "ORH": "09:01~09:30 고점 지정가", "ORL": "09:30 이후 30분 저점 이탈 손절",
                "ORH_ORL": "30분 고점 지정가 / 저점 손절 중 먼저", "P50": "현재 규칙(+3% 50%, +5% 50%)"}},
            {"key": "gapb", "label": "갭 크기", "aggregate": True,
             "labels": {"0to2": "0 ~ −2%", "2to4": "−2 ~ −4%", "lt4": "−4% 이하"}}, PERIOD]
    write("own-closing-gapdown-20261115-v1", "갭하락으로 시작한 종가베팅은 언제 팔까",
          "D일 시가가 매수가보다 낮게 열린 567건에 매도 시점·방식 14가지를 적용. 급등주형은 시가 즉시 매도가 가장 나쁘고 30~60분 반등 후가 나음.",
          OC/"20261115-gapdown-v1", df, dims, {"rows": "rule", "cols": "set", "fixed": {"gapb": "*", "period": "*"}},
          extra_reason=False)


def main():
    EXPORT.mkdir(parents=True, exist_ok=True)
    strategies()
    ranking()
    exit_v1()
    exit_v2()
    gapdown()


if __name__ == "__main__":
    main()
