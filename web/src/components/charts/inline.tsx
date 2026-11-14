/* Tiny inline SVG marks for table cells (no chart instance per row). */
export function Sparkline({ values, color = "var(--ink-3)", w = 92, h = 24 }: { values?: number[]; color?: string; w?: number; h?: number }) {
  if (!values || values.length < 2) return null;
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const x = (i: number) => 2 + (i * (w - 6)) / (values.length - 1);
  const y = (v: number) => h - 3 - ((v - lo) / ((hi - lo) || 1)) * (h - 6);
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <line x1={2} x2={w - 2} y1={y(0)} y2={y(0)} stroke="var(--rule)" />
      <path d={values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("")} fill="none" stroke={color} strokeWidth={1.5} />
      <circle cx={x(values.length - 1)} cy={y(values.at(-1)!)} r={2.5} fill={color} />
    </svg>
  );
}

/** One leaderboard row of the forest plot: dot = mean, line = 95% CI, band = random-baseline range. */
export function ForestCell({ mean, ci, band, domain, color = "var(--ink-2)", w = 200, h = 26 }:
  { mean: number | null; ci: [number | null, number | null]; band: [number, number] | null; domain: [number, number]; color?: string; w?: number; h?: number }) {
  const x = (v: number) => 6 + ((v - domain[0]) / ((domain[1] - domain[0]) || 1)) * (w - 12);
  const cy = h / 2;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      {band && <rect x={x(band[0])} y={3} width={Math.max(2, x(band[1]) - x(band[0]))} height={h - 6} rx={2} fill="var(--band)" opacity={0.55} />}
      <line x1={x(0)} x2={x(0)} y1={1} y2={h - 1} stroke="var(--zero)" strokeDasharray="2 2" />
      {ci[0] != null && ci[1] != null && <line x1={x(ci[0])} x2={x(ci[1])} y1={cy} y2={cy} stroke={color} strokeWidth={2} strokeLinecap="round" />}
      {mean != null && <circle cx={x(mean)} cy={cy} r={4.5} fill={color} stroke="var(--panel)" strokeWidth={2} />}
    </svg>
  );
}

export function ForestAxis({ domain, w = 200 }: { domain: [number, number]; w?: number }) {
  const x = (v: number) => 6 + ((v - domain[0]) / ((domain[1] - domain[0]) || 1)) * (w - 12);
  const span = (domain[1] - domain[0]) * 100, step = [0.1, 0.2, 0.25, 0.5, 1, 2, 5].find(s => span / s <= 4) ?? 5;
  const ticks: number[] = [];
  for (let v = Math.ceil((domain[0] * 100) / step) * step; v <= domain[1] * 100 + 1e-9; v += step) ticks.push(+v.toFixed(4));
  return (
    <svg width={w} height={14} viewBox={`0 0 ${w} 14`} aria-hidden="true">
      {ticks.map(t => <text key={t} x={x(t / 100)} y={11} textAnchor="middle" fill="var(--ink-3)" fontSize={11}>{t === 0 ? "0" : `${t > 0 ? "+" : "−"}${Math.abs(t)}%`}</text>)}
    </svg>
  );
}
