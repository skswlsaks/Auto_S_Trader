"use client";
// "Find the best combination" mode of the lab: exhaustive model x condition search with an honest check.
import { useEffect, useMemo, useState } from "react";
import { Badge, Callout, Chip, cx, Empty, Loading, Panel, Segmented, SelectBox, Stat, StatGrid, Tip } from "@/components/ui";
import { useLabSource } from "@/lib/data";
import { int, num, prob, spct } from "@/lib/format";
import { defaultCut, search, type SearchResult, type SearchRow, type SearchStats, type SortKey, type Split } from "@/lib/search";
import type { LabIndexItem, LabSource } from "@/lib/types";

type Q = { get: (k: string) => string | null; set: (p: Record<string, string | null | undefined>) => void };

/** Condition columns whose values each occupy their own date range (e.g. before/after a training cut). */
function periodColumns(src: LabSource) {
  return src.columns.filter(c => c.role === "condition" && c.key !== "month").flatMap(c => {
    const span = new Map<number, [number, number]>();
    c.codes.forEach((v, i) => { const s = span.get(v); const d = src.day[i]; if (!s) span.set(v, [d, d]); else { s[0] = Math.min(s[0], d); s[1] = Math.max(s[1], d); } });
    const order = [...span.entries()].sort((a, b) => a[1][0] - b[1][0]);
    if (order.length < 2 || !order.every(([, s], i) => i === 0 || order[i - 1][1][1] < s[0])) return [];
    const vals = order.map(([v]) => v);
    return [{ col: c, select: vals.slice(0, -1).map(v => c.values[v]), test: [c.values[vals[vals.length - 1]]],
      label: `${c.label}: ${vals.slice(0, -1).map(v => c.labels[v]).join(", ")} → ${c.labels[vals[vals.length - 1]]}` }];
  });
}

function monthCuts(src: LabSource) {
  const used = [...new Set(src.day)].sort((a, b) => a - b).map(d => src.days[d]);
  const firsts: string[] = [];
  for (const d of used) if (!firsts.length || firsts[firsts.length - 1].slice(0, 7) !== d.slice(0, 7)) firsts.push(d);
  return firsts.slice(1);
}

export function LabSearch({ src, meta, sources, q }: { src: LabSource; meta: LabIndexItem; sources: LabIndexItem[]; q: Q }) {
  const periods = useMemo(() => periodColumns(src), [src]);
  const cuts = useMemo(() => monthCuts(src), [src]);
  const auto = useMemo(() => defaultCut(src), [src]);
  const modelKeys = src.columns.filter(c => c.role === "model").map(c => c.key);
  const holdoutChoices = sources.filter(s => s.source_id !== meta.source_id && modelKeys.every(k => s.columns.some(c => c.key === k)));
  const defaultHold = holdoutChoices.find(s => s.locked_holdout)?.source_id ?? "";

  const splitKey = q.get("s.split") ?? (periods[0] ? `col:${periods[0].col.key}` : "auto");
  const maxConds = Number(q.get("s.k") ?? 2), minN = Number(q.get("s.min") ?? 25);
  const holdId = q.get("s.hold") ?? defaultHold, sort = (q.get("s.sort") ?? "sel") as SortKey, robustOnly = q.get("s.robust") === "1";
  const holdMeta = holdoutChoices.find(s => s.source_id === holdId);
  const { data: hold } = useLabSource(holdMeta?.file ?? null);

  const split: Split = useMemo(() => {
    if (splitKey.startsWith("col:")) {
      const p = periods.find(x => x.col.key === splitKey.slice(4));
      if (p) return { by: "column", key: p.col.key, select: p.select, test: p.test };
    }
    return { by: "date", cut: splitKey.startsWith("date:") ? splitKey.slice(5) : auto };
  }, [splitKey, periods, auto]);

  const [res, setRes] = useState<{ key: string; r: SearchResult; ms: number } | null>(null);
  const runKey = JSON.stringify([meta.source_id, split, maxConds, minN, holdId, !!hold, sort]);
  const waitingHold = !!holdMeta && !hold;
  useEffect(() => {
    if (meta.locked_holdout || waitingHold) return;
    const id = setTimeout(() => {
      const t0 = performance.now();
      const r = search(src, { split, maxConds, minN, minDays: 10, sort, top: 200, holdout: hold ?? null });
      setRes({ key: runKey, r, ms: performance.now() - t0 });
    }, 30);
    return () => clearTimeout(id);
  }, [runKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (meta.locked_holdout) return <Callout>잠금 시험 데이터에서는 조합을 고르지 않습니다. 개발 데이터를 고르고, 이 데이터는 확인용으로 지정하세요.</Callout>;

  const busy = !res || res.key !== runKey;
  const r = res?.r;
  const rows = r ? r.rows.filter(x => !robustOnly || x.robust).slice(0, 60) : [];
  const toManual = (row: SearchRow) => {
    const patch: Record<string, string | null> = { mode: null };
    for (const c of src.columns) {
      const v = row.model[c.key] ?? row.conds[c.key];
      patch[`f.${c.key}`] = v != null ? String(c.values.indexOf(v)) : null;
    }
    q.set(patch);
  };

  const splitOptions = [
    ...periods.map(p => ({ value: `col:${p.col.key}`, label: p.label })),
    { value: "auto", label: `자동: ${auto}부터 확인 기간` },
    ...cuts.filter(c => c !== auto).map(c => ({ value: `date:${c}`, label: `${c}부터 확인 기간` })),
  ];

  return (
    <div className="space-y-4">
      <Panel title="최적 조합 찾기"
        description="모든 모델 값에 조건을 최대 두 개까지 붙여 계산합니다. 앞 기간(선정)에서 순위를 매기고, 시도한 조합 수만큼 보정한 뒤, 같은 조합을 고르는 데 쓰지 않은 뒤 기간(확인)과 잠금 시험에서 다시 봅니다."
        actions={<div className="flex flex-wrap items-center gap-2">
          <SelectBox label="기간 나누기" value={splitKey} onChange={v => q.set({ "s.split": v })} options={splitOptions} className="min-w-[260px]" />
          <SelectBox label="확인 데이터" value={holdId || "-"} onChange={v => q.set({ "s.hold": v === "-" ? "" : v })}
            options={[{ value: "-", label: "잠금 시험: 없음" }, ...holdoutChoices.map(s => ({ value: s.source_id, label: `잠금 시험: ${s.title}` }))]} className="min-w-[220px]" />
        </div>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Segmented label="조건 수" value={String(maxConds)} onChange={v => q.set({ "s.k": v === "2" ? null : v })}
            options={[{ value: "0", label: "모델만" }, { value: "1", label: "조건 1개까지" }, { value: "2", label: "조건 2개까지" }]} />
          <SelectBox label="최소 거래 수" value={String(minN)} onChange={v => q.set({ "s.min": v === "25" ? null : v })}
            options={[15, 25, 40, 60].map(n => ({ value: String(n), label: `선정 기간 ${n}건 이상` }))} />
          <Segmented label="정렬" value={sort} onChange={v => q.set({ "s.sort": v === "sel" ? null : v })}
            options={[{ value: "sel", label: "선정 기간 평균" }, { value: "worst", label: "두 기간 중 낮은 값" }, { value: "q", label: "보정 q" }]} />
          <Chip on={robustOnly} onClick={() => q.set({ "s.robust": robustOnly ? null : "1" })}>두 기간 통과만</Chip>
        </div>
        {sort === "worst" && <Callout tone="info">이 정렬은 확인 기간 결과로 고르므로, 그 뒤로는 잠금 시험만 독립적인 확인입니다.</Callout>}
        {!r ? <Loading label="조합을 계산하는 중" /> : (
          <div className={cx("transition-opacity", busy && "opacity-50")}>
            <StatGrid cols={4}>
              <Stat label="계산한 조합" value={int(r.distinct)} sub={`같은 거래를 가리키는 이름 ${int(r.tried - r.distinct)}개 제외 · ${Math.round(res!.ms)}ms`} />
              <Stat label="선정 기간 유의 (q<0.05)" value={int(r.significant)} sub="시도 수로 보정한 뒤" />
              <Stat label="두 기간 통과" value={int(r.robust)} tone={r.robust ? "pass" : undefined} sub="유의 + 확인 기간 평균 > 0 (10건 이상)" />
              <Stat label="선정 → 확인" value={<span className="text-base">{r.split.select_range?.join(" ~ ") ?? "–"}</span>} sub={`확인 ${r.split.test_range?.join(" ~ ") ?? "없음"}`} />
            </StatGrid>
            {Object.keys(r.skipped_columns).length > 0 && (
              <p className="mb-0 mt-2 text-xs text-ink-3">조건에서 뺀 열: {Object.entries(r.skipped_columns).map(([k, why]) => `${src.columns.find(c => c.key === k)?.label ?? k} (${why === "splits by date" ? "날짜로 나뉨" : why === "one value only" ? "값이 하나" : "달력·기간 열"})`).join(", ")}</p>
            )}
          </div>
        )}
      </Panel>

      {r && (
        <Panel pad={false} title="조합 순위" description="행을 누르면 그 조합이 '직접 조합' 화면에 적용됩니다. 선정 기간 평균이 가장 높은 조합은 대개 가장 과적합된 조합입니다. 확인 기간과 잠금 시험 숫자로 판단하세요.">
          <div className={cx("mt-3 max-h-[calc(100vh-260px)] min-h-[240px] overflow-auto", busy && "opacity-50")}>
            <table className="t-board">
              <caption className="sr-only">모델과 조건 조합 순위. 행을 누르면 직접 조합 화면으로 이동합니다.</caption>
              <thead><tr>
                <th className="w-10"><div className="th">#</div></th>
                <th className="l"><div className="th">모델</div></th>
                <th className="l"><div className="th">조건</div></th>
                <th><div className="th">선정 기간</div></th>
                <th><div className="th"><Tip content="시도한 모든 조합으로 보정한 Benjamini-Hochberg q. 거래일 단위 묶음 표준오차로 평균 > 0을 검정했습니다."><span>보정 q</span></Tip></div></th>
                <th><div className="th">확인 기간</div></th>
                {holdMeta && <th><div className="th">잠금 시험</div></th>}
                <th><div className="th"><Tip content="선정 기간 이익 합 / 손실 합"><span>손익비</span></Tip></div></th>
                <th className="l"><div className="th">판정</div></th>
              </tr></thead>
              <tbody>
                {rows.map((x, i) => (
                  <tr key={i} tabIndex={0} onClick={() => toManual(x)} onKeyDown={e => { if (e.key === "Enter") toManual(x); }}>
                    <td className="text-ink-3">{i + 1}</td>
                    <td className="l min-w-[180px] max-w-[260px] whitespace-normal!">{Object.values(x.model_labels).join(" · ")}</td>
                    <td className="l min-w-[200px] max-w-[360px] whitespace-normal!" title={Object.entries(x.cond_labels).map(([k, v]) => `${src.columns.find(c => c.key === k)?.label}: ${v}`).join(", ")}>
                      {Object.keys(x.cond_labels).length ? Object.entries(x.cond_labels).map(([k, v]) => (
                        <span key={k} className="my-0.5 mr-1 inline-block whitespace-nowrap rounded bg-sunken px-1.5 text-xs leading-[18px] text-ink-2">{src.columns.find(c => c.key === k)?.label}: {v}</span>
                      )) : <span className="text-ink-3">조건 없음</span>}
                      {x.aliases > 0 && <span className="sub"> 외 같은 거래 {x.aliases}개</span>}
                    </td>
                    <StatCell s={x.sel} />
                    <td className={cx(x.sel.q! < 0.05 && "font-semibold text-pass")}>{prob(x.sel.q)}</td>
                    <StatCell s={x.test} />
                    {holdMeta && <StatCell s={x.holdout ?? null} missing={x.holdout === null} />}
                    <td>{x.sel.profit_factor == null ? "–" : num(x.sel.profit_factor)}</td>
                    <td className="l"><Judge row={x} hasHold={!!holdMeta} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rows.length && <Empty title={robustOnly ? "두 기간을 모두 통과한 조합이 없습니다" : "조건에 맞는 조합이 없습니다"}>
              {robustOnly ? "이 데이터에서는 확인 기간까지 유지된 조합이 없다는 뜻입니다." : "최소 거래 수를 낮추거나 기간 나누기를 바꿔 보세요."}</Empty>}
          </div>
        </Panel>
      )}
    </div>
  );
}

function StatCell({ s, missing }: { s: SearchStats | null; missing?: boolean }) {
  if (missing) return <td className="text-ink-3">열 없음</td>;
  if (!s || !s.n) return <td className="text-ink-3">0건</td>;
  return (
    <td className={cx(s.mean! > 0 ? "text-pass" : "text-fail")}>
      {spct(s.mean, 2)} <span className="sub">{int(s.n)}건{s.days != null ? ` · ${s.days}일` : ""}</span>
    </td>
  );
}

function Judge({ row, hasHold }: { row: SearchRow; hasHold: boolean }) {
  const h = row.holdout;
  if (row.robust && hasHold && h && h.n >= 5) return h.mean! > 0 ? <Badge tone="pass">세 구간 양수</Badge> : <Badge tone="warn">잠금 시험에서 음수</Badge>;
  if (row.robust) return <Badge tone="pass">두 기간 통과</Badge>;
  if (row.test && row.test.n >= 10 && row.test.mean! <= 0) return <Badge tone="fail">확인 기간 음수</Badge>;
  if (row.sel.q! < 0.05) return <Badge tone="warn">확인 기간 표본 부족</Badge>;
  return <Badge tone="none">보정 후 불확실</Badge>;
}
