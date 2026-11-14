"use client";
/* ECharts base: tree-shaken modules, canvas renderer, resize-aware, theme-aware (CSS variables). */
import { BarChart, CustomChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef, useState } from "react";

echarts.use([LineChart, ScatterChart, BarChart, HeatmapChart, CustomChart, GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, VisualMapComponent, MarkLineComponent, MarkAreaComponent, CanvasRenderer]);

export type EOption = echarts.EChartsCoreOption;

export function EChart({ option, height = 280, onClick, ariaLabel }: { option: EOption; height?: number; onClick?: (p: { data?: unknown; seriesName?: string; name?: string; value?: unknown }) => void; ariaLabel: string }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const click = useRef(onClick);
  click.current = onClick;
  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    c.on("click", p => click.current?.(p as never));
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
  }, []);
  useEffect(() => { chart.current?.setOption(option, { notMerge: true }); }, [option]);
  return <div ref={el} role="img" aria-label={ariaLabel} style={{ height, width: "100%" }} />;
}

/** Resolved design-token colours; updates when the theme attribute changes. */
export interface Colors { [k: string]: string }
const VARS = ["paper", "panel", "sunken", "ink", "ink-2", "ink-3", "rule", "rule-2", "accent", "band", "zero", "pass", "fail", "warn",
  "series-1", "series-2", "series-3", "series-4", "div-neg", "div-mid", "div-pos"];

function read(): Colors {
  const cs = getComputedStyle(document.documentElement);
  return Object.fromEntries(VARS.map(v => [v, cs.getPropertyValue(`--${v}`).trim()]));
}
export function useColors(): Colors | null {
  const [c, setC] = useState<Colors | null>(null);
  useEffect(() => {
    setC(read());
    const mo = new MutationObserver(() => setC(read()));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, []);
  return c;
}
export const SERIES = ["series-1", "series-2", "series-3", "series-4"];

export const baseText = (c: Colors) => ({ color: c["ink-3"], fontFamily: "inherit", fontSize: 11 });
export const tooltipStyle = (c: Colors) => ({
  backgroundColor: c.panel, borderColor: c.rule, borderWidth: 1, padding: [7, 10],
  textStyle: { color: c.ink, fontSize: 12, fontFamily: "inherit" }, extraCssText: "box-shadow:0 6px 20px rgba(24,35,58,.14);border-radius:4px;",
});
export const axisCommon = (c: Colors) => ({
  axisLine: { lineStyle: { color: c.rule } }, axisTick: { show: false }, axisLabel: baseText(c),
  splitLine: { lineStyle: { color: c["rule-2"] } },
});
