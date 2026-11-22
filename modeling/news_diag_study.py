"""Step 2a: is the instructor's edge in news, and is that news visible before the S close?

Research only. Diagnostic on a sample before any full news collection. Naver mobile stock news
(minute timestamps). Naver disclosure timestamps are republish times (~06:50) so disclosures are
not used. Protocol and the go/no-go rule are fixed before results are read.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import threading
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import closing_narrow_study as cn
from .config import atomic_json
from .pattern_eval import sha

ROOT = Path(__file__).resolve().parents[2]
MR = ROOT/"outputs/model-research"
SRC = MR/"closing-narrow/20261114-v1"
OUT = MR/"news-diag/20261115-v1"
RAW = OUT/"raw"
H = {"User-Agent": "Mozilla/5.0"}
POS = ["공급계약", "수주", "계약 체결", "특허", "임상", "승인", "FDA", "허가", "인수", "합병", "M&A", "최대주주", "흑자",
       "최대 실적", "사상 최대", "영업이익", "MOU", "협약", "파트너", "독점", "개발 성공", "양산", "수출", "선정"]
NEG = ["유상증자", "전환사채", "CB 발행", "소송", "횡령", "배임", "거래정지", "관리종목", "적자", "하향", "매각 철회", "불성실"]
PROTOCOL = dict(
    version="news-diag-v1",
    sample=dict(period="S in 2026-07-01..2026-10-27", picks="all instructor picks without risk flag (PICKS_F)",
                others="600 random R1 (closing scan, no risk flag) non-pick rows, random_state=7"),
    source="m.stock.naver.com/api/news/stock/{code} (newest-first pages of 20, minute datetime). Pages located by binary "
           "search on datetime and cached; articles de-duplicated by id",
    windows_kst=dict(base="S-20 calendar days 00:00 .. S-1 15:30 (daily average = baseline)",
                     pre="S-1 15:30 .. S 15:20 (visible to a closing scan)",
                     after="S 15:30 .. S 20:00 (NXT after-market window)",
                     night="S 20:00 .. D 08:00 (pre-market)"),
    features=dict(n="article count", named="title contains the stock name", feature="title contains '특징주'",
                  pos="title contains a positive-catalyst keyword (POS)", neg="title contains a negative keyword (NEG)",
                  surge="pre named count >= 3 x baseline daily named average and >= 2"),
    go_rule="Go to a full news selection test only if BOTH: (1) picks' share with >=1 named pre-close article is >= 1.5x "
            "the others' share, AND (2) among the 600 others, rows with >=1 named pre-close article have mean next-day "
            "gap >= +0.5%p above rows without. Otherwise report where the picks' news sits in time.",
    pos_keywords=POS, neg_keywords=NEG,
)


def get(url):
    for a in range(4):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=15).read())
        except urllib.error.HTTPError as e:
            if e.code == 400:
                raise
            err = e
            time.sleep(1+a)
        except Exception as e:
            err = e
            time.sleep(1+a)
    raise err


def page(code, p):
    path = RAW/code/f"{p}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass  # partial file from an earlier run; refetch
    try:
        r = get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=20&page={p}")
    except urllib.error.HTTPError as e:
        if e.code != 400:  # 400 = page past the end of history
            raise
        r = []
    items = [i for g in r for i in g.get("items", [])] if isinstance(r, list) else []
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False))
    os.replace(tmp, path)
    time.sleep(.15)
    return items


def oldest(items):
    return min(i["datetime"] for i in items) if items else None


def find_page(code, target, hi=4000):
    """Smallest page whose oldest article is older than target (pages are newest-first)."""
    lo, best = 1, None
    while lo <= hi:
        mid = (lo+hi)//2
        it = page(code, mid)
        if not it:
            hi = mid-1
            continue
        if oldest(it) < target:
            best, hi = mid, mid-1
        else:
            lo = mid+1
    return best


def collect_code(code, start, end):
    """All articles with start <= datetime <= end (YYYYMMDDHHMM)."""
    p_end = find_page(code, end) or 1
    p_end = max(1, p_end-1)
    p_start = find_page(code, start)
    last = p_start if p_start else p_end+400
    last = min(last, p_end+600)  # cap for heavy-news large caps (noted in coverage)
    arts = {}
    for p in range(p_end, last+1):
        it = page(code, p)
        if not it:
            break
        for i in it:
            if start <= i["datetime"] <= end:
                arts[i["id"]] = dict(id=i["id"], datetime=i["datetime"], title=i.get("titleFull") or i.get("title", ""),
                                     office=i.get("officeName"))
        if oldest(it) < start:
            break
    return code, list(arts.values()), last-p_end+1


def sample():
    f = pd.read_parquet(SRC/"features.parquet")
    e = f[(f.S >= "20260701") & (f.S <= "20261027")]
    pk = e[e.pick & ~e.risk].assign(grp="pick")
    ot = e[e.R1 & ~e.pick].sample(600, random_state=7).assign(grp="other")
    return pd.concat([pk, ot], ignore_index=True)


def ts(day, hm, delta_days=0):
    d = datetime.strptime(day, "%Y%m%d")+timedelta(days=delta_days)
    return d.strftime("%Y%m%d")+hm


def prev_day(S, days):
    i = days.index(S)
    return days[i-1] if i > 0 else S


def names():
    u = json.loads((MR/"whole-market/20261030-v1/universe.json").read_text())["symbols"]
    return {s["symbol"]: s.get("name", "") for s in u}


def analyze(smp, arts):
    nm = names()
    days = sorted(pd.read_parquet(SRC/"features.parquet", columns=["S"]).S.unique())
    rows = []
    for r in smp.itertuples(index=False):
        a = arts.get(r.code, pd.DataFrame(columns=["datetime", "title"]))
        name = nm.get(r.code, "")
        S1 = prev_day(r.S, days)
        w = dict(base=(ts(r.S, "0000", -20), ts(S1, "1530")), pre=(ts(S1, "1530"), ts(r.S, "1520")),
                 after=(ts(r.S, "1530"), ts(r.S, "2000")), night=(ts(r.S, "2000"), ts(r.D, "0800")))
        out = dict(code=r.code, S=r.S, D=r.D, grp=r.grp, gap=r.gap, O2=r.O2, name=name)
        for k, (s, e) in w.items():
            x = a[(a.datetime >= s) & (a.datetime < e)] if len(a) else a
            t = x.title if len(x) else pd.Series([], dtype=str)
            named = t.str.contains(re.escape(name), regex=True) if name and len(t) else pd.Series(False, index=t.index)
            out[f"{k}_n"] = len(t)
            out[f"{k}_named"] = int(named.sum())
            out[f"{k}_feature"] = int((t.str.contains("특징주") & named).sum()) if len(t) else 0
            out[f"{k}_pos"] = int((t.str.contains("|".join(map(re.escape, POS))) & named).sum()) if len(t) else 0
            out[f"{k}_neg"] = int((t.str.contains("|".join(map(re.escape, NEG))) & named).sum()) if len(t) else 0
        out["base_named_daily"] = out["base_named"]/20
        out["surge"] = out["pre_named"] >= max(2, 3*out["base_named_daily"])
        rows.append(out)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT/"protocol.json", dict(PROTOCOL, source_sha256=sha(Path(__file__))))
    smp = sample()
    smp[["code", "S", "D", "grp"]].to_csv(OUT/"sample.csv", index=False)
    jobs = []
    for c, g in smp.groupby("code"):  # merge overlapping per-row windows [S-21d, D 09:00]
        win = sorted((ts(r.S, "0000", -21), ts(r.D, "0900")) for r in g.itertuples())
        cur = list(win[0])
        for s0, e0 in win[1:]:
            if s0 <= cur[1]:
                cur[1] = max(cur[1], e0)
            else:
                jobs.append((c, *cur)); cur = [s0, e0]
        jobs.append((c, *cur))
    parts, pages = {}, {}
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(collect_code, *j) for j in jobs]
        for n, fu in enumerate(cf.as_completed(futs), 1):
            code, items, npg = fu.result()
            parts.setdefault(code, []).extend(items)
            pages[code] = pages.get(code, 0)+npg
            if n % 50 == 0:
                print(f"collected {n}/{len(jobs)} windows", flush=True)
    arts = {c: pd.DataFrame(v, columns=["id", "datetime", "title", "office"]).drop_duplicates("id") for c, v in parts.items()}
    pd.concat([d.assign(code=c) for c, d in arts.items()], ignore_index=True).to_parquet(OUT/"articles.parquet", index=False)
    res = analyze(smp, arts)
    res.to_parquet(OUT/"rows.parquet", index=False)
    g = res.groupby("grp")
    summ = {}
    for col in [c for c in res if c.split("_")[0] in ("base", "pre", "after", "night")]:
        summ[col] = dict(mean=g[col].mean().round(3).to_dict(), share_pos=g[col].apply(lambda s: float((s > 0).mean())).round(3).to_dict())
    summ["surge"] = g.surge.mean().round(3).to_dict()
    o = res[res.grp == "other"]
    with_n, without = o[o.pre_named > 0], o[o.pre_named == 0]
    summ["others_gap_by_pre_named"] = dict(with_=cn.stats(with_n.gap, with_n.S), without=cn.stats(without.gap, without.S))
    summ["others_O2_by_pre_named"] = dict(with_=cn.stats(with_n.O2, with_n.S), without=cn.stats(without.O2, without.S))
    for k in ("pre_pos", "surge", "pre_feature"):
        m = o[k] > 0
        summ[f"others_gap_by_{k}"] = dict(yes=cn.stats(o[m].gap, o[m].S), no=cn.stats(o[~m].gap, o[~m].S))
    sp = res.groupby("grp").pre_named.apply(lambda s: (s > 0).mean())
    go1 = sp["pick"] >= 1.5*sp["other"]
    go2 = (with_n.gap.mean()-without.gap.mean()) >= .005
    summ["go"] = dict(rule1=bool(go1), rule2=bool(go2), go=bool(go1 and go2), pick_share=float(sp["pick"]), other_share=float(sp["other"]))
    summ["coverage"] = dict(codes=len(arts), pages_median=float(np.median(list(pages.values()))), pages_max=int(max(pages.values())),
                            articles=int(sum(len(d) for d in arts.values())))
    atomic_json(OUT/"summary.json", summ)
    print(json.dumps(summ, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
