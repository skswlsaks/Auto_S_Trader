"use client";
import { useMemo } from "react";
import type { MetricDef } from "@/lib/metric-defs";
import { num, pp } from "@/lib/format";
import { axisCommon, baseText, EChart, tooltipStyle, useColors, type EOption } from "./echart";

export interface Item { id: string; label: string; color: string; n?: number }

// ---------- cumulative curve with drawdown and zoom ----------
export function CurveChart({ items, baselines = [], height = 360, drawdown = true }:
  { items: (Item & { curve: [string, number][] })[]; baselines?: { id: string; curve: [string, number][] }[]; height?: number; drawdown?: boolean }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c || !items.length) return null;
    const dd = (curve: [string, number][]) => { let peak = 0; return curve.map(([d, v]) => { peak = Math.max(peak, v); return [d, +(v - peak).toFixed(4)]; }); };
    const grids = drawdown ? [{ left: 48, right: 16, top: 34, height: "58%" }, { left: 48, right: 16, top: "76%", bottom: 54 }] : [{ left: 48, right: 16, top: 34, bottom: 54 }];
    const xAxis = grids.map((_, i) => ({ type: "time", gridIndex: i, ...axisCommon(c), axisLabel: { ...baseText(c), show: i === grids.length - 1, formatter: { year: "{yyyy}", month: "{yy}.{MM}", day: "{MM}.{dd}" } }, splitLine: { show: false } }));
    const yAxis = grids.map((_, i) => ({ type: "value", gridIndex: i, ...axisCommon(c), name: i ? "낙폭 %p" : "누적 %p", nameTextStyle: baseText(c), splitNumber: i ? 2 : 5 }));
    const series: object[] = [
      ...baselines.map((b, k) => ({ name: k ? `기준선 ${k + 1}` : "무작위 기준선", type: "line", showSymbol: false, data: b.curve, xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: c.band, width: 1.5, type: "dashed" }, itemStyle: { color: c.band }, silent: true, z: 1, legendHoverLink: false })),
      ...items.map((it, k) => ({ name: it.label, type: "line", showSymbol: false, data: it.curve, xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { color: it.color, width: 2 }, itemStyle: { color: it.color }, z: 3,
        markLine: k === 0 ? { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.zero, type: "solid", width: 1 }, data: [{ yAxis: 0 }] } : undefined })),
      ...(drawdown ? items.map(it => ({ name: it.label, type: "line", showSymbol: false, data: dd(it.curve), xAxisIndex: 1, yAxisIndex: 1,
        lineStyle: { color: it.color, width: 1 }, areaStyle: { color: it.color, opacity: 0.12 }, itemStyle: { color: it.color } })) : []),
    ];
    return {
      animation: false, grid: grids, xAxis, yAxis, series,
      legend: { top: 0, left: 0, textStyle: { color: c["ink-2"], fontSize: 12 }, icon: "roundRect", itemWidth: 14, itemHeight: 3, data: items.map(i => i.label) },
      tooltip: { trigger: "axis", ...tooltipStyle(c), valueFormatter: (v: number) => pp(v) },
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: c["ink-3"] } },
      dataZoom: [{ type: "inside", xAxisIndex: grids.map((_, i) => i) },
        { type: "slider", xAxisIndex: grids.map((_, i) => i), height: 18, bottom: 8, borderColor: c.rule, fillerColor: "rgba(57,73,201,.12)",
          textStyle: baseText(c), labelFormatter: (v: number) => new Date(v).toISOString().slice(2, 10).replace(/-/g, "."), dataBackground: { lineStyle: { color: c.band }, areaStyle: { color: c.sunken } } }],
    };
  }, [c, items, baselines, drawdown]);
  return option ? <EChart option={option} height={height} ariaLabel="누적 순수익률과 낙폭 비교" /> : null;
}

// ---------- one metric across items: dot + CI, baseline band, reference line ----------
export function MetricStrip({ def, items, values, cis, band }:
  { def: MetricDef; items: Item[]; values: (number | null)[]; cis: ([number | null, number | null] | null)[]; band: [number, number] | null }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c) return null;
    const ext = [...values, ...cis.flatMap(x => x ?? []), ...(band ?? []), ...(def.ref != null ? [def.ref] : [])].filter((v): v is number => v != null && Number.isFinite(v));
    let lo = Math.min(...ext), hi = Math.max(...ext);
    const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.1 || 1; lo -= pad; hi += pad;
    if (def.share || def.key === "trades") lo = Math.max(0, lo);
    const ys = items.map((_, i) => String(i + 1));
    return {
      animation: false, grid: { left: 22, right: 86, top: 6, bottom: 22 },
      xAxis: { type: "value", min: lo, max: hi, ...axisCommon(c), axisLabel: { ...baseText(c), formatter: (v: number) => def.axis(v) }, splitNumber: 3 },
      yAxis: { type: "category", data: ys, inverse: true, ...axisCommon(c), axisLine: { show: false }, axisLabel: { ...baseText(c), fontWeight: 700, color: c["ink-2"] }, splitLine: { show: false } },
      tooltip: { trigger: "item", ...tooltipStyle(c), formatter: (p: { dataIndex: number; seriesType: string }) => {
        const i = p.dataIndex, ci = cis[i];
        return `<b>${items[i].label}</b><br/>${def.label}: <b>${def.fmt(values[i])}</b>${ci && ci[0] != null ? `<br/>95% 구간: ${def.fmt(ci[0])} … ${def.fmt(ci[1])}` : ""}${band ? `<br/>기준선 범위: ${def.fmt(band[0])} … ${def.fmt(band[1])}` : ""}`;
      } },
      series: [
        { type: "custom", silent: true, renderItem: (_: unknown, api: { value: (i: number) => number; coord: (v: number[]) => number[] }) => {
            const i = api.value(0), ci = cis[i];
            if (!ci || ci[0] == null || ci[1] == null) return null;
            const a = api.coord([ci[0], i]), b = api.coord([ci[1], i]);
            return { type: "line", shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, style: { stroke: items[i].color, lineWidth: 2, lineCap: "round" } };
          }, data: items.map((_, i) => [i]), encode: { x: 0, y: 0 }, z: 2 },
        { type: "scatter", symbolSize: 11, z: 3, data: values.map((v, i) => ({ value: [v, i], itemStyle: { color: items[i].color, borderColor: c.panel, borderWidth: 2 } })),
          markArea: band ? { silent: true, itemStyle: { color: c.band, opacity: 0.45 }, data: [[{ xAxis: band[0] }, { xAxis: band[1] }]] } : undefined,
          markLine: def.ref != null ? { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.zero, type: "solid", width: 1.5 }, data: [{ xAxis: def.ref }] } : undefined },
      ],
    } as EOption;
  }, [c, def, items, values, cis, band]);
  const labels = useMemo(() => {
    const vs = values.filter((v): v is number => v != null);
    const best = def.better && vs.length > 1 ? (def.better > 0 ? Math.max(...vs) : Math.min(...vs)) : null;
    return values.map(v => `${def.fmt(v)}${best != null && v === best ? " ▲" : ""}`);
  }, [def, values]);
  if (!option) return null;
  const h = 28 + items.length * 26;
  return (
    <div className="relative">
      <EChart option={option} height={h} ariaLabel={`${def.label} 비교`} />
      <div className="pointer-events-none absolute right-1 top-[6px] flex flex-col text-right text-xs" style={{ height: h - 28 }}>
        {labels.map((l, i) => <span key={i} className="flex flex-1 items-center justify-end">{l}</span>)}
      </div>
    </div>
  );
}

// ---------- scatter map: every item on two metrics ----------
export function ScatterMap({ points, x, y, onPick, height = 360 }:
  { points: { id: string; label: string; x: number | null; y: number | null; color: string | null; slot?: number; hollow?: boolean }[]; x: MetricDef; y: MetricDef; onPick?: (id: string) => void; height?: number }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c) return null;
    const pts = points.filter(p => p.x != null && p.y != null);
    const mk = (p: typeof pts[number]) => ({ value: [p.x, p.y], id: p.id, name: p.label,
      symbolSize: p.color ? 13 : 9,
      itemStyle: p.color ? { color: p.color, borderColor: c.panel, borderWidth: 2 } : p.hollow ? { color: "transparent", borderColor: c["ink-3"], borderWidth: 1.5 } : { color: c["ink-3"], opacity: 0.5 },
      label: p.slot != null ? { show: true, formatter: String(p.slot + 1), position: "right", color: c.ink, fontWeight: 700 } : undefined });
    return {
      animation: false, grid: { left: 56, right: 18, top: 24, bottom: 40 },
      xAxis: { type: "value", scale: true, name: x.label, nameLocation: "end", nameTextStyle: { ...baseText(c), color: c["ink-2"], align: "right", verticalAlign: "top", padding: [24, 0, 0, 0] }, ...axisCommon(c), axisLabel: { ...baseText(c), formatter: (v: number) => x.axis(v) } },
      yAxis: { type: "value", scale: true, name: y.label, nameTextStyle: { ...baseText(c), color: c["ink-2"], align: "left" }, ...axisCommon(c), axisLabel: { ...baseText(c), formatter: (v: number) => y.axis(v) } },
      tooltip: { trigger: "item", ...tooltipStyle(c), formatter: (p: { data: { name: string; value: number[] } }) => `<b>${p.data.name}</b><br/>${x.label}: ${x.fmt(p.data.value[0])}<br/>${y.label}: ${y.fmt(p.data.value[1])}<br/><span style="color:${c["ink-3"]}">클릭 = 비교 추가/해제</span>` },
      series: [{ type: "scatter", data: [...pts.filter(p => !p.color), ...pts.filter(p => p.color)].map(mk),
        markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.zero, type: "dashed" },
          data: [...(x.ref != null ? [{ xAxis: x.ref }] : []), ...(y.ref != null ? [{ yAxis: y.ref }] : [])] } }],
    };
  }, [c, points, x, y]);
  return option ? <EChart option={option} height={height} ariaLabel="모델 지도" onClick={p => onPick?.((p.data as { id: string }).id)} /> : null;
}

// ---------- condition grid heatmap ----------
export interface HeatCell { row: number; col: number; value: number | null; n: number; label: string; sig?: boolean; ciPos?: boolean; id: string }
export function Heatmap({ rows, cols, cells, def, onPick, minN = 30, height }:
  { rows: string[]; cols: string[]; cells: HeatCell[]; def: MetricDef; onPick?: (id: string) => void; minN?: number; height?: number }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c) return null;
    const vals = cells.map(x => x.value).filter((v): v is number => v != null);
    const diverging = def.ref != null && !def.share;
    const ref = def.ref ?? 0;
    const span = Math.max(...vals.map(v => Math.abs(v - ref)), 1e-9);
    const [min, max] = diverging ? [ref - span, ref + span] : [Math.min(...vals), Math.max(...vals)];
    const lowColor = def.better < 0 ? c["div-pos"] : c["div-neg"], highColor = def.better < 0 ? c["div-neg"] : c["div-pos"];
    return {
      animation: false, grid: { left: 8, right: 8, top: 8, bottom: 56, containLabel: true },
      xAxis: { type: "category", data: cols, position: "top", ...axisCommon(c), axisLabel: { ...baseText(c), color: c["ink-2"], interval: 0, width: 110, overflow: "break" }, splitArea: { show: false } },
      yAxis: { type: "category", data: rows, inverse: true, ...axisCommon(c), axisLabel: { ...baseText(c), color: c["ink-2"], width: 190, overflow: "truncate" } },
      visualMap: { min, max, calculable: false, orient: "horizontal", left: "center", bottom: 6, itemHeight: 160, itemWidth: 10,
        text: [def.fmt(max), def.fmt(min)], textStyle: baseText(c),
        inRange: { color: diverging ? [lowColor, c["div-mid"], highColor] : [c["div-mid"], def.better < 0 ? c["div-neg"] : c["series-1"]] } },
      tooltip: { trigger: "item", ...tooltipStyle(c), formatter: (p: { data: { cell: HeatCell } }) => {
        const x = p.data.cell; return `<b>${x.label}</b><br/>${def.label}: <b>${def.fmt(x.value)}</b><br/>거래 ${x.n}건${x.n < minN ? " (표본 부족)" : ""}${x.sig ? "<br/>보정 후 유의 (q<0.05)" : x.ciPos ? "<br/>95% 구간이 0 위 (보정 전)" : ""}<br/><span style="color:${c["ink-3"]}">클릭 = 상세</span>`;
      } },
      series: [{ type: "heatmap", data: cells.map(x => ({ value: [x.col, x.row, x.value], cell: x, id: x.id,
          itemStyle: { opacity: x.n < minN ? 0.35 : 1, borderColor: x.sig ? c.ink : x.ciPos ? c["ink-3"] : c.panel, borderWidth: x.sig ? 2.5 : x.ciPos ? 1.5 : 2 } })),
        label: { show: true, fontFamily: "inherit", formatter: (p: { data: { cell: HeatCell } }) => `{v|${def.fmt(p.data.cell.value)}}\n{n|n=${p.data.cell.n}}`,
          rich: { v: { color: c.ink, fontSize: 12, fontWeight: 600 }, n: { color: c["ink-2"], fontSize: 10 } } },
        emphasis: { itemStyle: { borderColor: c.accent, borderWidth: 2 } } }],
    };
  }, [c, rows, cols, cells, def, minN]);
  return option ? <EChart option={option} height={height ?? Math.max(220, 64 + rows.length * 44)} ariaLabel={`${def.label} 조건 격자`} onClick={p => onPick?.((p.data as { id: string }).id)} /> : null;
}

// ---------- distribution ----------
export function Histogram({ hist, height = 170 }: { hist: { edges: number[]; counts: number[] }; height?: number }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c) return null;
    const e = hist.edges.map(v => Math.round(v * 100));
    const labels = hist.counts.map((_, i) => (i === 0 ? `<${e[0]}%` : i === hist.counts.length - 1 ? `≥${e.at(-1)}%` : `${e[i - 1]}~${e[i]}%`));
    const zero = e.indexOf(0);
    return {
      animation: false, grid: { left: 34, right: 8, top: 14, bottom: 24 },
      xAxis: { type: "category", data: labels, ...axisCommon(c), axisLabel: { ...baseText(c), interval: 1 } },
      yAxis: { type: "value", ...axisCommon(c), splitNumber: 3 },
      tooltip: { trigger: "axis", ...tooltipStyle(c), valueFormatter: (v: number) => `${v}건` },
      series: [{ type: "bar", barCategoryGap: "12%", data: hist.counts.map((v, i) => ({ value: v, itemStyle: { color: i <= zero ? c.band : c["series-1"], borderRadius: [3, 3, 0, 0] } })) }],
    };
  }, [c, hist]);
  return option ? <EChart option={option} height={height} ariaLabel="거래별 순수익률 분포" /> : null;
}

// ---------- monthly P&L heatmap (items x months) ----------
export function MonthlyHeatmap({ items }: { items: { label: string; monthly: Record<string, number> }[] }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => {
    if (!c || !items.length) return null;
    const months = [...new Set(items.flatMap(i => Object.keys(i.monthly)))].sort();
    const data = items.flatMap((it, r) => months.map((m, k) => [k, r, it.monthly[m] ?? null]));
    const span = Math.max(...data.map(d => Math.abs((d[2] as number) ?? 0)), 1e-9);
    return {
      animation: false, grid: { left: 8, right: 8, top: 8, bottom: 50, containLabel: true },
      xAxis: { type: "category", data: months.map(m => m.slice(2).replace("-", ".")), ...axisCommon(c) },
      yAxis: { type: "category", data: items.map(i => i.label), inverse: true, ...axisCommon(c), axisLabel: { ...baseText(c), color: c["ink-2"], width: 220, overflow: "truncate" } },
      visualMap: { min: -span, max: span, orient: "horizontal", left: "center", bottom: 4, itemHeight: 160, itemWidth: 10, text: [pp(span), pp(-span)], textStyle: baseText(c),
        inRange: { color: [c["div-neg"], c["div-mid"], c["div-pos"]] } },
      tooltip: { trigger: "item", ...tooltipStyle(c), formatter: (p: { value: [number, number, number | null] }) => `<b>${items[p.value[1]].label}</b><br/>${months[p.value[0]]}: <b>${pp(p.value[2])}</b>` },
      series: [{ type: "heatmap", data, itemStyle: { borderColor: c.panel, borderWidth: 2 },
        label: { show: months.length <= 14, fontSize: 10, color: c.ink, formatter: (p: { value: [number, number, number | null] }) => (p.value[2] == null ? "" : num(p.value[2], 1)) } }],
    };
  }, [c, items]);
  return option ? <EChart option={option} height={70 + items.length * 34} ariaLabel="월별 손익 비교" /> : null;
}

// ---------- training curve ----------
export function TrainCurve({ curve }: { curve: { step: number; value: number }[] }) {
  const c = useColors();
  const option = useMemo<EOption | null>(() => c && ({
    animation: false, grid: { left: 40, right: 10, top: 10, bottom: 24 },
    xAxis: { type: "value", ...axisCommon(c), splitLine: { show: false }, name: "step", nameTextStyle: baseText(c) },
    yAxis: { type: "value", ...axisCommon(c) },
    tooltip: { trigger: "axis", ...tooltipStyle(c) },
    series: [{ type: "line", showSymbol: false, data: curve.map(p => [p.step, p.value]), lineStyle: { color: c["series-1"], width: 2 } }],
  }), [c, curve]);
  return option ? <EChart option={option} height={150} ariaLabel="학습 곡선" /> : null;
}
