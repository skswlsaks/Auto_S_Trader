"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { CurveChart, MetricStrip, MonthlyHeatmap, ScatterMap } from "@/components/charts";
import { useColors } from "@/components/charts/echart";
import { Button, Empty, Loading, MetricLabel, Panel, Segmented, SelectBox, Swatch } from "@/components/ui";
import { useCompare } from "@/lib/compare";
import { useIndex, useRun } from "@/lib/data";
import { GROUPS, METRICS, metricByKey, type Group } from "@/lib/metric-defs";
import type { RunDetail } from "@/lib/types";
import { FamilyTag, inSplit, useQuery } from "./common";

export function CompareView() {
  const q = useQuery(), cmp = useCompare(), { data: idx } = useIndex(), colors = useColors();
  const urlIds = q.get("ids");
  // the URL wins (shared links); otherwise the compare list
  useEffect(() => {
    if (urlIds) { const ids = urlIds.split(",").filter(Boolean); if (ids.join() !== cmp.list.map(e => e.id).join()) cmp.set(ids); }
  }, [urlIds]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const ids = cmp.list.map(e => e.id).join(","); if (ids !== (urlIds ?? "")) q.set({ ids: ids || null }); }, [cmp.list]); // eslint-disable-line react-hooks/exhaustive-deps

  const ids = cmp.list.map(e => e.id);
  const r0 = useRun(ids[0] ?? null), r1 = useRun(ids[1] ?? null), r2 = useRun(ids[2] ?? null), r3 = useRun(ids[3] ?? null);
  const details = [r0, r1, r2, r3].map(x => x.data).filter((x): x is RunDetail => !!x);
  const [group, setGroup] = useState<Group>((q.get("g") as Group) || "핵심");
  const [sx, setSx] = useState(q.get("x") ?? "max_drawdown_pp"), [sy, setSy] = useState(q.get("y") ?? "mean");

  const first = idx?.runs.find(r => r.run.run_id === ids[0]);
  const peers = useMemo(() => (idx && first ? inSplit(idx, first.run.track_id, first.run.split) : []), [idx, first]);
  const baseIds = peers.filter(r => r.run.model.family === "baseline").map(r => r.run.run_id).slice(0, 5);
  const b0 = useRun(baseIds[0] ?? null), b1 = useRun(baseIds[1] ?? null), b2 = useRun(baseIds[2] ?? null);
  const baseCurves = [b0, b1, b2].map(x => x.data).filter((x): x is RunDetail => !!x?.metrics.curve && !ids.includes(x.run.run_id)).map(x => ({ id: x.run.run_id, curve: x.metrics.curve! }));

  if (!idx || !colors) return <Loading />;
  if (!ids.length) return (
    <Panel><Empty title="비교할 모델을 고르세요" action={<Link href="/runs/">모델 순위로 가기</Link>}>모델 순위에서 체크하거나, 아래 모델 지도에서 점을 누르면 비교 목록에 추가됩니다.</Empty></Panel>
  );
  const splits = new Set(ids.map(id => idx.runs.find(r => r.run.run_id === id)?.run.split));
  const items = cmp.list.map(e => ({ id: e.id, label: `${e.slot + 1}. ${e.id}`, color: `var(--series-${e.slot + 1})` }));
  const resolved = items.map(it => ({ ...it, color: colors[`series-${cmp.slot(it.id)! + 1}`] }));
  const summaries = ids.map(id => idx.runs.find(r => r.run.run_id === id));

  return (
    <>
      <header className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="m-0 mr-2 text-[22px] font-semibold tracking-tight">모델 비교</h1>
        {cmp.list.map(e => {
          const s = idx.runs.find(r => r.run.run_id === e.id);
          return (
            <span key={e.id} className="inline-flex items-center gap-2 text-[13px] font-medium">
              <Swatch color={`var(--series-${e.slot + 1})`} n={e.slot + 1} />
              <Link href={`/run/?id=${encodeURIComponent(e.id)}`} className="text-ink no-underline hover:underline">{e.id}</Link>
              {s && <FamilyTag family={s.run.model.family} />}
              <button className="text-ink-3 hover:text-ink" aria-label={`${e.id} 비교에서 빼기`} onClick={() => cmp.toggle(e.id)}>✕</button>
            </span>
          );
        })}
        <Button variant="ghost" size="sm" onClick={cmp.clear}>모두 빼기</Button>
      </header>
      {splits.size > 1 && <p className="mb-3 rounded-r border-l-[3px] border-warn bg-warn-soft px-3 py-2 text-[13px]">서로 다른 평가 구간의 run이 섞여 있습니다. 같은 구간끼리 비교해야 공정합니다.</p>}

      <Panel title="지표별 비교" description="회색 띠 = 같은 구간 무작위 기준선 범위, 세로선 = 기준값, 가는 선 = 95% 구간, ▲ = 고른 모델 중 가장 좋은 값."
        actions={<Segmented label="지표 묶음" value={group} onChange={v => { setGroup(v); q.set({ g: v === "핵심" ? null : v }); }} options={GROUPS.map(g => ({ value: g, label: g }))} />}>
        <div className="grid grid-cols-4 gap-x-5 gap-y-4 max-xl:grid-cols-2 max-md:grid-cols-1">
          {METRICS.filter(d => d.group === group).map(d => {
            const vals = summaries.map(s => (s ? d.get(s.metrics as unknown as Record<string, unknown>) : null));
            const cis = summaries.map(s => (s && d.ci ? d.ci(s.metrics as unknown as Record<string, unknown>) : null));
            const bvals = peers.filter(r => r.run.model.family === "baseline").map(r => d.get(r.metrics as unknown as Record<string, unknown>)).filter((v): v is number => v != null);
            return (
              <div key={d.key} className="min-w-0">
                <div className="text-[13px] font-semibold"><MetricLabel def={d} /></div>
                <MetricStrip def={d} items={resolved} values={vals} cis={cis} band={bvals.length ? [Math.min(...bvals), Math.max(...bvals)] : null} />
              </div>
            );
          })}
        </div>
      </Panel>

      <div className="mt-4 grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-4 max-xl:grid-cols-1">
        <Panel title="누적 순수익률과 낙폭" description="거래당 1단위 손익의 누적(%p)과 고점 대비 낙폭. 휠·아래 막대로 기간을 확대합니다. 회색 점선 = 무작위 기준선.">
          {details.length ? <CurveChart items={details.map(d => ({ id: d.run.run_id, label: `${cmp.slot(d.run.run_id)! + 1}. ${d.run.run_id}`, color: resolved.find(r => r.id === d.run.run_id)!.color, curve: d.metrics.curve! }))} baselines={baseCurves} height={420} /> : <Loading />}
        </Panel>
        <Panel title="모델 지도" description="같은 구간의 모든 run을 두 지표로 배치합니다. 점을 누르면 비교 목록에 추가하거나 뺍니다. 빈 원 = 무작위 기준선."
          actions={<div className="flex flex-wrap gap-2"><SelectBox label="가로 지표" value={sx} onChange={v => { setSx(v); q.set({ x: v }); }} options={METRICS.map(m => ({ value: m.key, label: m.label }))} />
            <SelectBox label="세로 지표" value={sy} onChange={v => { setSy(v); q.set({ y: v }); }} options={METRICS.map(m => ({ value: m.key, label: m.label }))} /></div>}>
          <ScatterMap x={metricByKey[sx]} y={metricByKey[sy]} onPick={id => cmp.toggle(id)}
            points={peers.map(r => ({ id: r.run.run_id, label: r.run.run_id, x: metricByKey[sx].get(r.metrics as unknown as Record<string, unknown>), y: metricByKey[sy].get(r.metrics as unknown as Record<string, unknown>),
              color: cmp.has(r.run.run_id) ? resolved.find(i => i.id === r.run.run_id)?.color ?? null : null, slot: cmp.slot(r.run.run_id) ?? undefined, hollow: r.run.model.family === "baseline" }))} />
        </Panel>
      </div>

      <div className="mt-4">
        <Panel title="월별 손익" description="월별 단위 손익 합(%p). 한두 달에 성과가 몰렸는지 확인합니다.">
          {details.length ? <MonthlyHeatmap items={details.map(d => ({ label: `${cmp.slot(d.run.run_id)! + 1}. ${d.run.run_id}`, monthly: d.metrics.monthly_pp ?? {} }))} /> : <Loading />}
        </Panel>
      </div>
    </>
  );
}
