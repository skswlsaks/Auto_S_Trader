"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { CurveChart, Heatmap, Histogram, MetricStrip, MonthlyHeatmap, type HeatCell } from "@/components/charts";
import { useColors } from "@/components/charts/echart";
import { Badge, Button, Callout, Empty, Loading, MetricLabel, Panel, SelectBox, Sheet, Stat, StatGrid, Swatch, Tag } from "@/components/ui";
import { useCell, useIndex, useStudy } from "@/lib/data";
import { int, pct, prob, spct, when } from "@/lib/format";
import { METRICS, metricByKey } from "@/lib/metric-defs";
import type { Study, StudyCell } from "@/lib/types";
import { Headline, useQuery } from "./common";

const ALL = "*";
const CELL_METRICS = ["mean", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp", "sharpe", "cvar5", "payoff_ratio", "mean_ex_top5", "positive_month_ratio", "expectancy_r", "q_value", "deflated_sharpe", "p_value", "trades"];

export function StudiesView() {
  const { data: idx } = useIndex();
  if (!idx) return <Loading />;
  const studies = [...idx.studies].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return (
    <>
      <Headline title={studies.length ? `조건 × 모델 조합 실험 ${studies.length}개를 같은 기준으로 비교합니다.` : "아직 올라온 조건 실험이 없습니다."}
        note="실험마다 모든 조건 조합의 지표를 플랫폼이 다시 계산합니다. 조합이 많을수록 우연히 좋아 보이는 칸이 생기므로, 비교한 조합 수로 보정한 결과(q값)를 함께 봅니다." />
      {!studies.length && <Panel><Empty title="실험을 올리는 방법">실험 폴더에서 <code>uv run python -m arena study submit &lt;폴더&gt;</code>를 실행하면 여기에 나타납니다.</Empty></Panel>}
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        {studies.map(s => (
          <Link key={s.study_id} href={`/studies/view/?id=${encodeURIComponent(s.study_id)}`} className="block rounded-lg border border-rule bg-panel p-4 text-ink no-underline hover:border-ink-3">
            <div className="flex flex-wrap items-start gap-2">
              <h2 className="m-0 flex-1 text-[15px] font-semibold">{s.title}</h2>
              {s.locked_holdout ? <Badge tone="warn">잠금 시험</Badge> : <Badge tone="none">개발</Badge>}
            </div>
            <p className="mb-3 mt-1 text-[13px] text-ink-2">{s.purpose}</p>
            <StatGrid cols={4}>
              <Stat label="거래" value={int(s.n_trades)} sub={`${s.first_day} ~ ${s.last_day}`} />
              <Stat label="비교한 조합" value={int(s.trials)} sub={s.dimensions.map(d => `${d.label} ${d.n}`).join(", ")} />
              <Stat label="보정 후 유의" value={int(s.significant_cells)} sub="q < 0.05, 평균 > 0" tone={s.significant_cells ? "pass" : undefined} />
              <Stat label="가장 좋은 칸 (n≥30)" value={s.best_cell ? spct(s.best_cell.mean, 2) : "–"} sub={s.best_cell ? `n=${s.best_cell.n}, q=${prob(s.best_cell.q_value)}` : undefined} />
            </StatGrid>
            <div className="mt-2 text-xs text-ink-3">갱신 {when(s.updated_at)} · {s.study_id}</div>
          </Link>
        ))}
      </div>
    </>
  );
}

const lab = (d: Study["dimensions"][number], v: string) => (v === ALL ? "전체" : d.labels?.[v] ?? v);

export function StudyView() {
  const q = useQuery(), id = q.get("id");
  const { data: s, error } = useStudy(id);
  const colors = useColors();
  const dims = s?.dimensions ?? [];
  const rowsKey = q.get("rows") ?? s?.defaults?.rows ?? dims[0]?.key;
  const colsKey = q.get("cols") ?? s?.defaults?.cols ?? dims[1]?.key;
  const metric = metricByKey[q.get("m") ?? "mean"];
  const [open, setOpen] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);

  const fixed = useMemo(() => {
    const out: Record<string, string> = {};
    for (const d of dims) {
      if (d.key === rowsKey || d.key === colsKey) continue;
      const fromUrl = q.get(`f.${d.key}`), def = s?.defaults?.fixed?.[d.key];
      out[d.key] = fromUrl ?? def ?? (d.aggregate ? ALL : d.values[0]);
    }
    return out;
  }, [dims, rowsKey, colsKey, q, s]);

  const view = useMemo(() => {
    if (!s || !rowsKey) return null;
    const rd = dims.find(d => d.key === rowsKey)!, cd = colsKey ? dims.find(d => d.key === colsKey) : undefined;
    const rvals = [...rd.values, ...(rd.aggregate ? [ALL] : [])], cvals = cd ? [...cd.values, ...(cd.aggregate ? [ALL] : [])] : [ALL];
    const index = new Map(s.cells.map(c => [JSON.stringify(dims.map(d => c.key[d.key])), c]));
    const cells: HeatCell[] = [];
    rvals.forEach((rv, r) => cvals.forEach((cv, k) => {
      const key = dims.map(d => (d.key === rowsKey ? rv : d.key === colsKey ? cv : fixed[d.key]));
      const c = index.get(JSON.stringify(key));
      if (c) cells.push({ row: r, col: k, id: c.id, n: c.n, value: metric.get(c as unknown as Record<string, unknown>), sig: c.significant, ciPos: (c.mean_ci95[0] ?? -1) > 0,
        label: dims.map(d => `${d.label}: ${lab(d, c.key[d.key])}`).join("<br/>") });
    }));
    return { rows: rvals.map(v => lab(rd, v)), cols: cvals.map(v => (cd ? lab(cd, v) : metric.label)), cells };
  }, [s, dims, rowsKey, colsKey, fixed, metric]);

  if (!id) return <Empty title="실험을 고르세요" action={<Link href="/studies/">조건 실험 목록</Link>} />;
  if (error) return <Callout tone="fail">실험을 불러오지 못했습니다 ({error.message}).</Callout>;
  if (!s || !view || !colors) return <Loading />;
  const byId = new Map(s.cells.map(c => [c.id, c]));
  const openCell = open ? byId.get(open) ?? null : null;
  const pickedCells = picked.map(p => byId.get(p)).filter((c): c is StudyCell => !!c);
  const dimOpts = dims.map(d => ({ value: d.key, label: d.label }));
  const labHref = `/lab/?source=${encodeURIComponent(`study:${s.study_id}`)}`;

  return (
    <>
      <div className="mb-1 text-[13px]"><Link href="/studies/" className="text-ink-2 no-underline hover:text-ink">← 조건 실험</Link></div>
      <Headline title={s.title} note={`${s.purpose} 거래 ${int(s.n_trades)}건, ${s.first_day} ~ ${s.last_day}. 갱신 ${when(s.updated_at)}.`}>
        <Link href={labHref} className="inline-flex h-8 items-center rounded bg-accent px-3 text-[13px] font-medium text-white no-underline dark:text-paper">조합 실험실에서 열기</Link>
        {s.has_report && <a href={`/data/studies/${encodeURIComponent(s.study_id)}/REPORT.md`} target="_blank" rel="noopener" className="text-[13px]">원 보고서</a>}
      </Headline>
      {s.locked_holdout && <Callout>잠금 시험 기간({s.holdout?.D_from} ~ {s.holdout?.D_to})입니다. 이 결과를 보고 조건을 고르거나 임계값을 바꾸면 시험의 독립성이 사라집니다. 사전 등록한 조합만 판정에 씁니다.</Callout>}
      <Callout tone="info">이 실험은 조합 {int(s.trials)}개를 비교했습니다. 보정 후에도 유의한 칸(q &lt; 0.05, 평균 &gt; 0)은 {int(s.significant_cells)}개입니다. 굵은 테두리 = 보정 후 유의, 얇은 테두리 = 95% 구간이 0 위(보정 전), 흐린 칸 = 표본 30건 미만.</Callout>

      <Panel pad={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-rule px-4 py-3">
          <label className="flex flex-col gap-1 text-xs text-ink-2">행<SelectBox label="행 차원" value={rowsKey} onChange={v => q.set({ rows: v, cols: v === colsKey ? rowsKey : colsKey })} options={dimOpts} /></label>
          <label className="flex flex-col gap-1 text-xs text-ink-2">열<SelectBox label="열 차원" value={colsKey ?? ""} onChange={v => q.set({ cols: v, rows: v === rowsKey ? colsKey : rowsKey })} options={dimOpts} /></label>
          <label className="flex flex-col gap-1 text-xs text-ink-2">표시 지표<SelectBox label="표시 지표" value={metric.key} onChange={v => q.set({ m: v })} options={CELL_METRICS.map(k => ({ value: k, label: metricByKey[k].label }))} /></label>
          <span className="mx-1 h-8 w-px bg-rule" />
          {dims.filter(d => d.key in fixed).map(d => (
            <label key={d.key} className="flex flex-col gap-1 text-xs text-ink-2">{d.label} 고정
              <SelectBox label={`${d.label} 고정값`} value={fixed[d.key]} onChange={v => q.set({ [`f.${d.key}`]: v })}
                options={[...(d.aggregate ? [{ value: ALL, label: "전체" }] : []), ...d.values.map(v => ({ value: v, label: lab(d, v) }))]} />
            </label>
          ))}
        </div>
        <div className="px-2 py-2">
          {view.cells.length ? <Heatmap rows={view.rows} cols={view.cols} cells={view.cells} def={metric} onPick={setOpen} /> : <Empty title="이 조합에는 칸이 없습니다">고정값을 바꿔 보세요.</Empty>}
        </div>
      </Panel>

      {!!pickedCells.length && (
        <div className="mt-4">
          <Panel title="고른 칸 비교" actions={<Button size="sm" variant="ghost" onClick={() => setPicked([])}>모두 빼기</Button>}>
            <div className="mb-3 flex flex-wrap gap-3 text-[13px]">
              {pickedCells.map((c, i) => <span key={c.id} className="inline-flex items-center gap-1.5"><Swatch color={colors[`series-${i + 1}`]} n={i + 1} />{dims.map(d => lab(d, c.key[d.key])).join(" · ")}
                <button className="text-ink-3 hover:text-ink" aria-label="빼기" onClick={() => setPicked(p => p.filter(x => x !== c.id))}>✕</button></span>)}
            </div>
            <div className="grid grid-cols-4 gap-x-5 gap-y-4 max-xl:grid-cols-2 max-md:grid-cols-1">
              {["mean", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp", "cvar5", "q_value", "trades"].map(k => {
                const d = metricByKey[k];
                return <div key={k}><div className="text-[13px] font-semibold"><MetricLabel def={d} /></div>
                  <MetricStrip def={d} items={pickedCells.map((c, i) => ({ id: c.id, label: dims.map(x => lab(x, c.key[x.key])).join(" · "), color: colors[`series-${i + 1}`] }))}
                    values={pickedCells.map(c => d.get(c as unknown as Record<string, unknown>))} cis={pickedCells.map(c => d.ci?.(c as unknown as Record<string, unknown>) ?? null)} band={null} /></div>;
              })}
            </div>
            <PickedCurves study={s} cells={pickedCells} colors={pickedCells.map((_, i) => colors[`series-${i + 1}`])} />
          </Panel>
        </div>
      )}

      <Sheet open={!!openCell} onOpenChange={o => !o && setOpen(null)} title={openCell ? dims.map(d => lab(d, openCell.key[d.key])).join(" · ") : ""}
        actions={openCell && <Button size="sm" variant={picked.includes(openCell.id) ? "default" : "primary"} onClick={() => setPicked(p => p.includes(openCell.id) ? p.filter(x => x !== openCell.id) : p.length >= 4 ? p : [...p, openCell.id])}>{picked.includes(openCell.id) ? "비교에서 빼기" : "비교에 추가"}</Button>}>
        {openCell && <CellDetailPanel study={s} cell={openCell} color={colors["series-1"]} siblings={s.cells.filter(c => c.id !== openCell.id && dims.every(d => d.key === "period" || c.key[d.key] === openCell.key[d.key]))} />}
      </Sheet>
    </>
  );
}

function PickedCurves({ study, cells, colors }: { study: Study; cells: StudyCell[]; colors: string[] }) {
  const a = useCell(study.study_id, cells[0]?.id ?? null), b = useCell(study.study_id, cells[1]?.id ?? null), c = useCell(study.study_id, cells[2]?.id ?? null), d = useCell(study.study_id, cells[3]?.id ?? null);
  const det = [a, b, c, d].map(x => x.data);
  const items = cells.map((cell, i) => det[i] && { id: cell.id, label: `${i + 1}`, color: colors[i], curve: det[i]!.curve }).filter(Boolean) as { id: string; label: string; color: string; curve: [string, number][] }[];
  return items.length ? <div className="mt-4"><CurveChart items={items} height={340} /></div> : null;
}

function CellDetailPanel({ study, cell, color, siblings }: { study: Study; cell: StudyCell; color: string; siblings: StudyCell[] }) {
  const { data } = useCell(study.study_id, cell.id);
  const M = cell as unknown as Record<string, unknown>;
  const periodDim = study.dimensions.find(d => d.key === "period");
  return (
    <div className="px-4 pb-6 pt-4">
      {cell.n < 30 && <Callout>표본이 {cell.n}건뿐입니다. 아래 지표의 신뢰도가 낮습니다.</Callout>}
      <StatGrid cols={2}>
        {(["mean", "accuracy", "profit_rate_01", "q_value"] as const).map(k => { const d = metricByKey[k], ci = d.ci?.(M); return <Stat key={k} label={<MetricLabel def={d} />} value={d.fmt(d.get(M))} sub={ci && ci[0] != null ? `95% ${d.fmt(ci[0])} … ${d.fmt(ci[1])}` : k === "q_value" ? `비교한 조합 ${study.trials}개 기준` : undefined} />; })}
      </StatGrid>
      {data ? <>
        <h3 className="mb-1 mt-4 text-[13px] font-semibold">누적 순수익률과 낙폭</h3>
        <CurveChart items={[{ id: cell.id, label: "이 칸", color, curve: data.curve }]} height={300} />
        <h3 className="mb-1 mt-3 text-[13px] font-semibold">거래별 순수익률 분포</h3><Histogram hist={data.return_hist} />
        <h3 className="mb-1 mt-3 text-[13px] font-semibold">월별 손익</h3><MonthlyHeatmap items={[{ label: "월별 %p", monthly: data.monthly_pp }]} />
      </> : <Loading />}
      {periodDim && !!siblings.length && <>
        <h3 className="mb-1 mt-4 text-[13px] font-semibold">같은 조건, 다른 기간</h3>
        <table className="w-full text-[13px]"><tbody>
          {[cell, ...siblings].map(c => <tr key={c.id} className="border-b border-rule-2"><td className="py-1.5">{lab(periodDim, c.key.period)}{c.id === cell.id && <Tag>이 칸</Tag>}</td><td className="text-right">{spct(c.mean, 3)}</td><td className="text-right text-ink-2">n={c.n}</td><td className="text-right text-ink-2">정확도 {pct(c.accuracy)}</td></tr>)}
        </tbody></table>
      </>}
      <h3 className="mb-1 mt-4 text-[13px] font-semibold">전체 지표</h3>
      <table className="w-full text-[13px]"><tbody>
        {METRICS.filter(d => d.get(M) != null).map(d => <tr key={d.key} className="border-b border-rule-2"><td className="py-1.5"><MetricLabel def={d} /></td><td className="py-1.5 text-right font-medium">{d.fmt(d.get(M))}</td></tr>)}
      </tbody></table>
    </div>
  );
}
