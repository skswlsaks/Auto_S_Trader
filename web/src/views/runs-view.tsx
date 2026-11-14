"use client";
import { createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type SortingState } from "@tanstack/react-table";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ForestAxis, ForestCell, Sparkline } from "@/components/charts/inline";
import { Callout, Chip, cx, Empty, Loading, MetricLabel, Panel, Segmented, SelectBox, Swatch } from "@/components/ui";
import { useCompare } from "@/lib/compare";
import { useIndex } from "@/lib/data";
import { GROUPS, METRICS, type Group } from "@/lib/metric-defs";
import type { RunSummary } from "@/lib/types";
import { baselineBand, defaultSplit, FAMILY, FamilyTag, GateBadge, Headline, inSplit, reproState, ReproBadge, SPLIT, useQuery, verdict } from "./common";

const ch = createColumnHelper<RunSummary>();

export function RunsView() {
  const { data: idx, error } = useIndex();
  const q = useQuery(), router = useRouter(), cmp = useCompare();
  const [sorting, setSorting] = useState<SortingState>([{ id: "mean", desc: true }]);
  const [fams, setFams] = useState(new Set(Object.keys(FAMILY)));
  const [gatesOnly, setGatesOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [warn, setWarn] = useState<string | null>(null);

  const track = idx ? (q.get("track") && idx.tracks[q.get("track")!] ? q.get("track")! : Object.keys(idx.tracks)[0]) : "";
  const split = idx ? (q.get("split") && idx.tracks[track].windows[q.get("split")!] ? q.get("split")! : defaultSplit(idx, track)) : "";
  const group = (q.get("view") as Group) || "핵심";
  const all = useMemo(() => (idx ? inSplit(idx, track, split) : []), [idx, track, split]);
  const band = useMemo(() => baselineBand(all), [all]);
  const rows = useMemo(() => all.filter(r => fams.has(r.run.model.family) && (!gatesOnly || r.metrics.passes_gates)
    && (!search || r.run.run_id.toLowerCase().includes(search.toLowerCase()))), [all, fams, gatesOnly, search]);
  const domain = useMemo<[number, number]>(() => {
    const v = rows.flatMap(r => [...r.metrics.mean_return_ci95, r.metrics.mean_return]).concat(band ?? [], [0]).filter((x): x is number => typeof x === "number");
    const lo = Math.min(...v), hi = Math.max(...v), pad = (hi - lo) * 0.04 || 0.001;
    return [lo - pad, hi + pad];
  }, [rows, band]);

  const columns = useMemo(() => {
    const defs = METRICS.filter(m => m.group === group && m.key !== "mean");
    return [
      ch.display({ id: "select", header: () => <span className="sr-only">비교</span>, cell: ({ row }) => (
        <input type="checkbox" className="h-4 w-4 cursor-pointer accent-[var(--accent)]" checked={cmp.has(row.original.run.run_id)} aria-label={`${row.original.run.run_id} 비교에 추가`}
          onClick={e => e.stopPropagation()} onChange={() => { if (!cmp.toggle(row.original.run.run_id)) setWarn("비교는 4개까지입니다. 하나를 빼고 추가하세요."); }} />) }),
      ch.accessor(r => r.run.run_id, { id: "run", header: "모델 (run)", cell: ({ row }) => {
        const id = row.original.run.run_id, s = cmp.slot(id);
        return <div className="flex min-w-[240px] items-center gap-2">{s != null && <Swatch color={`var(--series-${s + 1})`} n={s + 1} />}<span className="max-w-[320px] truncate font-medium" title={id}>{id}</span><FamilyTag family={row.original.run.model.family} /></div>;
      } }),
      ch.accessor(r => r.metrics.mean_return, { id: "mean", header: () => <MetricLabel def={METRICS[0]} />, cell: ({ getValue }) => <span className="font-semibold">{METRICS[0].fmt(getValue())}</span> }),
      ch.display({ id: "forest", header: () => <div className="px-2 pt-1.5 text-center"><div>95% 구간 · 기준선</div><ForestAxis domain={domain} /></div>, cell: ({ row }) => (
        <ForestCell mean={row.original.metrics.mean_return} ci={row.original.metrics.mean_return_ci95} band={band} domain={domain} color={cmp.color(row.original.run.run_id) ?? undefined} />) }),
      ...defs.map(d => ch.accessor(r => d.get(r.metrics), { id: d.key, header: () => <MetricLabel def={d} />, sortDescFirst: d.better >= 0,
        cell: ({ getValue, row }) => {
          const v = getValue() as number | null, ci = d.ci?.(row.original.metrics);
          return <>{d.fmt(v)}{ci && ci[0] != null && <div className="sub">{d.fmt(ci[0])}–{d.fmt(ci[1])}</div>}</>;
        } })),
      ch.display({ id: "spark", header: "누적", cell: ({ row }) => <Sparkline values={row.original.metrics.spark} color={cmp.color(row.original.run.run_id) ?? undefined} /> }),
      ch.accessor(r => ["missing", "unchecked", "not_reproduced", "partial", "reproduced"].indexOf(reproState(r)), { id: "repro", header: "재현", cell: ({ row }) => <ReproBadge r={row.original} /> }),
      ch.accessor(r => (r.metrics.passes_gates ? 1 : 0), { id: "gates", header: "기준", cell: ({ row }) => <GateBadge ok={row.original.metrics.passes_gates} /> }),
    ];
  }, [group, domain, band, cmp]);

  const table = useReactTable({ data: rows, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), sortDescFirst: true });

  if (error) return <Callout tone="fail">데이터를 불러오지 못했습니다: {error.message}</Callout>;
  if (!idx) return <Loading />;
  const t = idx.tracks[track], w = t.windows[split];

  return (
    <>
      <Headline title={verdict(all)} note={`${SPLIT[split] ?? split} ${w.start ?? "?"} ~ ${w.end ?? "미정"}. ${w.note ?? ""}${split === "holdout" ? "" : " 개발에 이미 노출된 기간이므로 최종 성능 근거로 쓰지 않습니다."}`}>
        {Object.keys(idx.tracks).length > 1 && <SelectBox label="트랙" value={track} onChange={v => q.set({ track: v, split: null })} options={Object.values(idx.tracks).map(x => ({ value: x.track_id, label: x.title }))} />}
        <Segmented label="평가 구간" value={split} onChange={v => q.set({ split: v })}
          options={Object.keys(t.windows).map(s => ({ value: s, label: <>{s}<span className="ml-1 font-normal text-ink-3">{inSplit(idx, track, s).length}</span></> }))} />
      </Headline>
      {warn && <Callout>{warn} <button className="ml-2 underline" onClick={() => setWarn(null)}>닫기</button></Callout>}
      <Panel pad={false}>
        <div className="flex flex-wrap items-center gap-2 border-b border-rule px-3.5 py-3">
          <Segmented label="지표 묶음" value={group} onChange={v => q.set({ view: v === "핵심" ? null : v })} options={GROUPS.map(g => ({ value: g, label: g }))} />
          <span className="mx-1 h-5 w-px bg-rule" />
          {Object.entries(FAMILY).map(([k, l]) => (
            <Chip key={k} on={fams.has(k)} onClick={() => setFams(s => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; })}>
              {l} {all.filter(r => r.run.model.family === k).length}
            </Chip>
          ))}
          <Chip on={gatesOnly} onClick={() => setGatesOnly(v => !v)}>기준 충족만</Chip>
          <div className="flex-1" />
          <input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="run 이름으로 찾기" aria-label="run 검색"
            className="h-8 w-56 rounded border border-rule bg-panel px-2.5 text-[13px] placeholder:text-ink-3 max-md:w-full" />
        </div>
        <div className="max-h-[calc(100vh-300px)] min-h-[260px] overflow-auto">
          <table className="t-board">
            <caption className="sr-only">모델 순위표. 행을 누르면 상세로 이동하고, 체크하면 비교 목록에 추가됩니다.</caption>
            <thead>{table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>{hg.headers.map(h => {
                const sorted = h.column.getIsSorted(), canSort = h.column.getCanSort() && !["select", "forest", "spark"].includes(h.column.id);
                return (
                  <th key={h.id} className={cx(h.column.id === "run" && "l", h.column.id === "select" && "w-9")} aria-sort={sorted ? (sorted === "asc" ? "ascending" : "descending") : undefined}>
                    {canSort ? <button onClick={h.column.getToggleSortingHandler()}>{flexRender(h.column.columnDef.header, h.getContext())}{sorted ? (sorted === "asc" ? " ↑" : " ↓") : ""}</button>
                      : <div className="th">{flexRender(h.column.columnDef.header, h.getContext())}</div>}
                  </th>
                );
              })}</tr>
            ))}</thead>
            <tbody>
              {table.getRowModel().rows.map(row => (
                <tr key={row.id} tabIndex={0} onClick={() => router.push(`/run/?id=${encodeURIComponent(row.original.run.run_id)}`)}
                  onKeyDown={e => { if (e.key === "Enter") router.push(`/run/?id=${encodeURIComponent(row.original.run.run_id)}`); if (e.key === " ") { e.preventDefault(); cmp.toggle(row.original.run.run_id); } }}>
                  {row.getVisibleCells().map(c => <td key={c.id} className={cx(c.column.id === "run" && "l", c.column.id === "forest" && "px-2")}>{flexRender(c.column.columnDef.cell, c.getContext())}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <Empty title="조건에 맞는 모델이 없습니다">필터를 줄이거나 다른 구간을 선택하세요.</Empty>}
        </div>
        <div className="flex flex-wrap items-center gap-4 border-t border-rule px-3.5 py-2.5 text-xs text-ink-3">
          <span><span className="mr-1.5 inline-block h-2.5 w-5 rounded-sm bg-band align-[-1px]" />무작위 기준선 평균 수익 범위</span>
          <span><span className="mr-1.5 inline-block h-0.5 w-5 bg-ink-2 align-[3px]" />거래당 평균 순수익 95% 구간</span>
          <span>지표 이름에 마우스를 올리면 정의가 보입니다. Space = 비교 추가</span>
          <span className="ml-auto">{rows.length}개 표시</span>
        </div>
      </Panel>
    </>
  );
}
