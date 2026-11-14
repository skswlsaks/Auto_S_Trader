const MINUS = "−";
const sign = (v: number) => (v > 0 ? "+" : v < 0 ? MINUS : "");
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export const pct = (v: unknown, d = 1) => (isNum(v) ? `${(v * 100).toFixed(d)}%` : "–");
export const spct = (v: unknown, d = 2) => (isNum(v) ? `${sign(v)}${Math.abs(v * 100).toFixed(d)}%` : "–");
export const num = (v: unknown, d = 2) => (isNum(v) ? `${v < 0 ? MINUS : ""}${Math.abs(v).toFixed(d)}` : "–");
export const pp = (v: unknown, d = 1) => (isNum(v) ? `${num(v, d)}%p` : "–");
export const int = (v: unknown) => (isNum(v) ? Math.round(v).toLocaleString("ko-KR") : "–");
export const prob = (v: unknown) => (isNum(v) ? (v < 0.001 ? "<0.001" : v.toFixed(3)) : "–");
export const bytes = (n: unknown) => (!isNum(n) ? "–" : n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`);
export const shortHash = (h?: string | null, n = 10) => (h ? h.slice(0, n) : "–");
export const when = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }) : "–";
export const signed = (v: number) => sign(v);
