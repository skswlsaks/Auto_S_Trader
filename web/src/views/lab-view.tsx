"use client";
import Link from "next/link";
import { useDeferredValue, useMemo, useState } from "react";
import { CurveChart, Heatmap, MetricStrip, type HeatCell } from "@/components/charts";
import { useColors } from "@/components/charts/echart";
import { Badge, Button, Callout, cx, Empty, Loading, MetricLabel, Panel, SelectBox, Stat, StatGrid } from "@/components/ui";
import { useIndex, useLabSource } from "@/lib/data";
import { groupBy, metrics, randomPercentile, select, type Filters, type LabMetrics } from "@/lib/engine";
import { int, pct, prob, spct } from "@/lib/format";
import { metricByKey } from "@/lib/metric-defs";
import type { LabSource } from "@/lib/types";
import { Headline, useQuery } from "./common";

const KPI = ["mean", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp", "cvar5", "sharpe", "payoff_ratio"];
const GRID_METRICS = ["mean", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp", "sharpe", "cvar5", "trades"];

function parseFilters(src: LabSource, get: (k: string) => string | null): Filters {
  const f: Filters = {};
  for (const c of src.columns) {
    const raw = get(`f.${c.key}`);
    if (raw != null) f[c.key] = raw.split(".").filter(Boolean).map(Number).filter(i => i >= 0 && i < c.values.length);
    else if (c.role === "model") f[c.key] = [0]; // one model value by default: mixing different strategies is meaningless
  }
  return f;
}

export function LabView() {
  const q = useQuery(), { data: idx } = useIndex(), colors = useColors();
  const sources = idx?.lab ?? [];
  const sourceId = q.get("source") ?? sources.find(s => s.kind === "study" && !s.locked_holdout)?.source_id ?? sources[0]?.source_id;
  const meta = sources.find(s => s.source_id === sourceId);
  const { data: src, error } = useLabSource(meta?.file ?? null);

  const filtersRaw = useMemo(() => (src ? parseFilters(src, q.get) : {}), [src, q]);
  const filters = useDeferredValue(filtersRaw);
  const rowsKey = q.get("rows") ?? "", colsKey = q.get("cols") ?? "";
  const gm = metricByKey[q.get("m") ?? "mean"];

  const result = useMemo(() => {
    if (!src) return null;
    const modelOnly: Filters = Object.fromEntries(src.columns.filter(c => c.role === "model").map(c => [c.key, filters[c.key]]));
    const idxSel = select(src, filters), idxBase = select(src, modelOnly);
    const sel = metrics(src, idxSel), base = metrics(src, idxBase);
    const conditioned = src.columns.some(c => c.role === "condition" && filters[c.key] && filters[c.key]!.length < c.values.length);
    const rand = conditioned ? randomPercentile(src, idxBase, idxSel.length, sel.mean) : null;
    // per-value counts for every chip, given all *other* filters
    const counts: Record<string, number[]> = {};
    for (const c of src.columns) {
      const others = select(src, { ...filters, [c.key]: undefined });
      const n = new Array(c.values.length).fill(0);
      for (const i of others) n[c.codes[i]]++;
      counts[c.key] = n;
    }
    let grid: { rows: string[]; cols: string[]; cells: HeatCell[] } | null = null;
    if (rowsKey) {
      const rc = src.columns.find(c => c.key === rowsKey), cc = colsKey ? src.columns.find(c => c.key === colsKey) : undefined;
      if (rc) {
        const groups = groupBy(src, idxSel, [rowsKey, ...(cc ? [colsKey] : [])]);
        const rv = [...new Set(groups.map(g => g.key[rowsKey]))].sort((a, b) => a - b), cv = cc ? [...new Set(groups.map(g => g.key[colsKey]))].sort((a, b) => a - b) : [0];
        grid = { rows: rv.map(v => rc.labels[v]), cols: cc ? cv.map(v => cc.labels[v]) : [gm.label], cells: groups.map(g => {
          const m = metrics(src, g.idx, { bootstrap: false });
          return { row: rv.indexOf(g.key[rowsKey]), col: cc ? cv.indexOf(g.key[colsKey]) : 0, n: m.n, value: gm.get(m as unknown as Record<string, unknown>),
            id: `${g.key[rowsKey]}|${cc ? g.key[colsKey] : ""}`, label: `${rc.label}: ${rc.labels[g.key[rowsKey]]}${cc ? `<br/>${cc.label}: ${cc.labels[g.key[colsKey]]}` : ""}` };
        }) };
      }
    }
    return { sel, base, rand, conditioned, counts, grid, nSel: idxSel.length, nBase: idxBase.length };
  }, [src, filters, rowsKey, colsKey, gm]);

  if (!idx || !colors) return <Loading />;
  if (!sources.length) return <Panel><Empty title="실험실에 쓸 데이터가 없습니다">조건 실험이나 Arena run이 올라오면 자동으로 생깁니다.</Empty></Panel>;
  if (error) return <Callout tone="fail">데이터를 불러오지 못했습니다 ({error.message}).</Callout>;

  const setFilter = (key: string, codes: number[] | null) => q.set({ [`f.${key}`]: codes == null ? null : codes.join(".") || "." });
  const toggle = (key: string, code: number, nValues: number, single: boolean) => {
    const cur = filters[key] ?? [...Array(nValues).keys()];
    if (single) return setFilter(key, [code]);
    const next = cur.includes(code) ? cur.filter(c => c !== code) : [...cur, code].sort((a, b) => a - b);
    setFilter(key, next.length === nValues ? null : next);
  };

  return (
    <>
      <Headline title="모델과 조건을 직접 조합해 결과를 봅니다."
        note="같은 모델에 조건을 걸었을 때 결과가 어떻게 바뀌는지, 그 차이가 같은 수의 무작위 선택보다 나은지 바로 계산합니다. 사전 등록·보정 전 탐색 결과이므로 결정은 조건 실험의 보정 값과 새 기간 시험으로 확인합니다.">
        <SelectBox label="데이터" value={sourceId ?? ""} onChange={v => q.set({ source: v, rows: null, cols: null, ...Object.fromEntries((src?.columns ?? []).map(c => [`f.${c.key}`, null])) })}
          options={sources.map(s => ({ value: s.source_id, label: `${s.title}${s.locked_holdout ? " (잠금 시험)" : ""}` }))} className="min-w-[280px]" />
        <Button size="sm" variant="ghost" onClick={() => navigator.clipboard.writeText(location.href)}>이 조합 링크 복사</Button>
      </Headline>
      {meta?.locked_holdout && <Callout>잠금 시험 데이터입니다. 여기서 조건을 고르면 시험의 독립성이 사라집니다. 사전 등록한 조합 확인 용도로만 쓰세요.</Callout>}
      {!src || !result ? <Loading label="거래 데이터를 불러오는 중" /> : (
        <div className="grid grid-cols-[340px_minmax(0,1fr)] items-start gap-4 max-lg:grid-cols-1">
          <aside className="sticky top-16 max-h-[calc(100vh-84px)] overflow-auto rounded-lg border border-rule bg-panel max-lg:static max-lg:max-h-none">
            {(["model", "condition"] as const).map(role => (
              <section key={role} className="border-b border-rule px-4 py-3 last:border-b-0">
                <h2 className="m-0 mb-2 text-[13px] font-semibold">{role === "model" ? "모델·규칙" : "조건"}</h2>
                {role === "condition" && <p className="mb-2 mt-0 text-xs text-ink-3">모두 진입 전에 알 수 있는 정보입니다. 칩을 눌러 빼거나 넣습니다.</p>}
                {src.columns.filter(c => c.role === role).map(c => {
                  const allowed = filters[c.key], counts = result.counts[c.key];
                  return (
                    <div key={c.key} className="mb-3">
                      <div className="mb-1 flex items-center gap-2 text-xs text-ink-2"><span className="flex-1 font-medium">{c.label}</span>
                        {role === "condition" && allowed && <button className="text-accent" onClick={() => setFilter(c.key, null)}>모두</button>}
                        {role === "model" && <button className="text-accent" onClick={() => setFilter(c.key, allowed && allowed.length === c.values.length ? [0] : [...Array(c.values.length).keys()])}>{allowed && allowed.length === c.values.length ? "하나만" : "모두"}</button>}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {c.values.map((_, code) => {
                          const on = !allowed || allowed.includes(code);
                          return (
                            <button key={code} aria-pressed={on} onClick={() => toggle(c.key, code, c.values.length, role === "model" && !!allowed && allowed.length === 1)}
                              className={cx("inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs", on ? "border-ink-3 bg-sunken text-ink" : "border-rule text-ink-3 hover:text-ink-2", counts[code] === 0 && "opacity-50")}>
                              {c.labels[code]}<span className="text-ink-3">{int(counts[code])}</span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </section>
            ))}
          </aside>

          <div className="min-w-0 space-y-4">
            <Panel title="결과" description={result.conditioned ? `조건을 건 ${int(result.nSel)}건과 같은 모델의 전체 ${int(result.nBase)}건을 비교합니다.` : `같은 모델의 전체 ${int(result.nBase)}건입니다. 왼쪽에서 조건을 빼 보세요.`}>
              {result.nSel === 0 ? <Empty title="선택한 조합에 거래가 없습니다">조건을 넓혀 보세요.</Empty> : <>
                <StatGrid cols={4}>
                  {KPI.slice(0, 4).map(k => { const d = metricByKey[k], v = d.get(result.sel as unknown as Record<string, unknown>), b = d.get(result.base as unknown as Record<string, unknown>), ci = d.ci?.(result.sel as unknown as Record<string, unknown>);
                    return <Stat key={k} label={<MetricLabel def={d} />} value={d.fmt(v)} sub={result.conditioned ? `조건 없음 ${d.fmt(b)}` : ci && ci[0] != null ? `95% ${d.fmt(ci[0])} … ${d.fmt(ci[1])}` : undefined} />; })}
                </StatGrid>
                <div className="mt-3 flex flex-wrap items-center gap-2 text-[13px]">
                  <Verdict sel={result.sel} rand={result.rand} conditioned={result.conditioned} />
                </div>
                {result.conditioned && (
                  <div className="mt-4 grid grid-cols-4 gap-x-5 gap-y-3 max-xl:grid-cols-2 max-md:grid-cols-1">
                    {KPI.map(k => { const d = metricByKey[k];
                      const items = [{ id: "sel", label: "조건 적용", color: colors["series-1"] }, { id: "base", label: "조건 없음", color: colors["ink-3"] }];
                      return <div key={k}><div className="text-[13px] font-semibold"><MetricLabel def={d} /></div>
                        <MetricStrip def={d} items={items} values={[d.get(result.sel as unknown as Record<string, unknown>), d.get(result.base as unknown as Record<string, unknown>)]}
                          cis={[d.ci?.(result.sel as unknown as Record<string, unknown>) ?? null, d.ci?.(result.base as unknown as Record<string, unknown>) ?? null]} band={null} /></div>; })}
                  </div>
                )}
              </>}
            </Panel>

            {result.nSel > 0 && (
              <Panel title="누적 순수익률과 낙폭" description="거래당 1단위 손익의 누적(%p). 회색 점선 = 같은 모델, 조건 없음.">
                <CurveChart items={[{ id: "sel", label: result.conditioned ? "조건 적용" : "선택", color: colors["series-1"], curve: result.sel.curve }]}
                  baselines={result.conditioned ? [{ id: "base", curve: result.base.curve }] : []} height={340} />
              </Panel>
            )}

            <Panel title="나눠 보기" description="현재 조합을 한두 차원으로 나눠 같은 지표를 비교합니다. 칸을 누르면 그 값으로 좁힙니다. 나눌수록 표본이 작아지고 우연히 좋아 보이는 칸이 늘어납니다."
              actions={<div className="flex flex-wrap gap-2">
                <SelectBox label="행" value={rowsKey || "-"} onChange={v => q.set({ rows: v === "-" ? null : v })} options={[{ value: "-", label: "행: 없음" }, ...src.columns.map(c => ({ value: c.key, label: `행: ${c.label}` }))]} />
                <SelectBox label="열" value={colsKey || "-"} onChange={v => q.set({ cols: v === "-" ? null : v })} options={[{ value: "-", label: "열: 없음" }, ...src.columns.filter(c => c.key !== rowsKey).map(c => ({ value: c.key, label: `열: ${c.label}` }))]} />
                <SelectBox label="지표" value={gm.key} onChange={v => q.set({ m: v })} options={GRID_METRICS.map(k => ({ value: k, label: metricByKey[k].label }))} />
              </div>}>
              {result.grid ? <Heatmap rows={result.grid.rows} cols={result.grid.cols} cells={result.grid.cells} def={gm}
                onPick={id => { const [r, c] = id.split("|"); q.set({ [`f.${rowsKey}`]: r, ...(colsKey && c !== "" ? { [`f.${colsKey}`]: c } : {}), rows: null, cols: null }); }} />
                : <Empty title="나눌 차원을 고르세요">예: 행 = 진입 방식, 열 = 전날 코스닥 국면.</Empty>}
            </Panel>
            {meta?.kind === "study" && <p className="text-xs text-ink-3">보정 값(q)과 셀별 상세는 <Link href={`/studies/view/?id=${encodeURIComponent(src.study_id ?? "")}`}>조건 실험 화면</Link>에서 봅니다.</p>}
          </div>
        </div>
      )}
    </>
  );
}

function Verdict({ sel, rand, conditioned }: { sel: LabMetrics; rand: number | null; conditioned: boolean }) {
  const ciLo = sel.mean_ci95[0];
  return (
    <>
      {sel.mean != null && (ciLo != null && ciLo > 0 ? <Badge tone="pass">95% 구간이 0 위 (보정 전)</Badge> : <Badge tone="none">평균 &gt; 0 확인 안 됨, p={prob(sel.p_value_mean_gt0)}</Badge>)}
      {conditioned && rand != null && (rand >= 0.95 ? <Badge tone="pass">무작위 선택보다 나음 (백분위 {pct(rand, 0)})</Badge>
        : <Badge tone={rand <= 0.05 ? "fail" : "none"}>같은 수 무작위 선택 대비 백분위 {pct(rand, 0)}</Badge>)}
      {sel.n < 30 && <Badge tone="warn">표본 {sel.n}건: 신뢰도 낮음</Badge>}
      <span className="text-ink-3">평균 {spct(sel.mean, 3)}, 거래 {int(sel.n)}건, 활동일 {int(sel.days)}일</span>
    </>
  );
}
