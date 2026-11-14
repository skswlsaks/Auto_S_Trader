import { int, num, pct, pp, prob, spct } from "./format";

/** One definition per metric, shared by tables, small multiples, the scatter map and the lab.
 *  `get` reads both Arena-run metric names and study-cell / lab names. */
export interface MetricDef {
  key: string; label: string; group: Group; better: 1 | -1 | 0; ref?: number;
  fmt: (v: unknown) => string; axis: (v: number) => string; help: string;
  get: (m: Record<string, unknown>) => number | null;
  ci?: (m: Record<string, unknown>) => [number | null, number | null] | null;
  share?: boolean; // 0..1 value
}
export type Group = "핵심" | "수익의 질" | "위험" | "안정성" | "노출" | "유의성";
export const GROUPS: Group[] = ["핵심", "수익의 질", "위험", "안정성", "노출", "유의성"];

const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const pick = (...keys: string[]) => (m: Record<string, unknown>) => { for (const k of keys) { const v = n(m[k]); if (v != null) return v; } return null; };
const ci = (...keys: string[]) => (m: Record<string, unknown>) => { for (const k of keys) if (Array.isArray(m[k])) return m[k] as [number | null, number | null]; return null; };

export const METRICS: MetricDef[] = [
  { key: "mean", label: "거래당 평균 순수익", group: "핵심", better: 1, ref: 0, fmt: v => spct(v, 3), axis: v => spct(v, 1), get: pick("mean_return", "mean"), ci: ci("mean_return_ci95", "mean_ci95"),
    help: "비용을 뺀 거래당 평균 수익률. 막대는 거래일 단위 부트스트랩 95% 구간." },
  { key: "accuracy", label: "매매 정확도", group: "핵심", better: 1, share: true, fmt: v => pct(v), axis: v => pct(v, 0), get: pick("accuracy"), ci: ci("accuracy_ci95"),
    help: "비용 포함 본절 이상(≥0)으로 전량 청산한 거래 / 전체 진입. 본절 0 수익도 성공이라 수익 비율과 함께 본다." },
  { key: "profit_rate_01", label: "수익 비율 (>+0.1%)", group: "핵심", better: 1, share: true, fmt: v => pct(v), axis: v => pct(v, 0), get: pick("profit_rate_01"),
    help: "비용 후 +0.1%를 넘긴 거래 비율. 연구 세션의 profit_share·win_rate와 같은 정의." },
  { key: "profit_factor", label: "Profit factor", group: "핵심", better: 1, ref: 1, fmt: v => num(v), axis: v => num(v, 1), get: pick("profit_factor"),
    help: "이익 합 / 손실 합. 1보다 커야 이익." },
  { key: "max_drawdown_pp", label: "최대 낙폭", group: "위험", better: 1, ref: 0, fmt: v => pp(v), axis: v => num(v, 0), get: pick("max_drawdown_pp"),
    help: "거래당 1단위를 걸었을 때 누적 곡선의 고점 대비 최대 하락(%p). 0에 가까울수록 좋다." },
  { key: "sharpe", label: "Sharpe (연율)", group: "위험", better: 1, ref: 0, fmt: v => num(v), axis: v => num(v, 0), get: pick("sharpe_daily_ann"),
    help: "일별 단위 손익의 평균/표준편차 × √252. 거래 없는 날은 0." },
  { key: "profit_rate", label: "수익 비율 (>0)", group: "수익의 질", better: 1, share: true, fmt: v => pct(v), axis: v => pct(v, 0), get: pick("profit_rate"),
    help: "비용 후 0을 넘긴 거래 / 전체 진입." },
  { key: "payoff_ratio", label: "손익비", group: "수익의 질", better: 1, ref: 1, fmt: v => num(v), axis: v => num(v, 1), get: pick("payoff_ratio"),
    help: "평균 이익 / 평균 손실 크기. 정확도가 낮아도 손익비가 크면 이익일 수 있다." },
  { key: "median", label: "중앙 순수익", group: "수익의 질", better: 1, ref: 0, fmt: v => spct(v, 3), axis: v => spct(v, 1), get: pick("median_return", "median"),
    help: "거래 수익률의 중앙값. 평균이 소수 대박에 끌려갔는지 확인." },
  { key: "mean_ex_top5", label: "상위 5거래 제외 평균", group: "수익의 질", better: 1, ref: 0, fmt: v => spct(v, 3), axis: v => spct(v, 1), get: pick("mean_ex_top5"),
    help: "가장 좋은 5건을 빼고 낸 평균. 크게 떨어지면 소수 거래 의존." },
  { key: "expectancy_r", label: "기대값 (R)", group: "수익의 질", better: 1, ref: 0, fmt: v => num(v, 3), axis: v => num(v, 2), get: pick("expectancy_r"),
    help: "손절 거리를 1R로 본 거래당 평균 손익. 손절가가 있는 거래만." },
  { key: "cvar5", label: "최악 5% 평균", group: "위험", better: 1, ref: 0, fmt: v => spct(v, 2), axis: v => spct(v, 1), get: pick("cvar5"),
    help: "가장 나쁜 5% 거래의 평균(CVaR). 급락 손실 크기." },
  { key: "worst_day_pp", label: "최악의 날", group: "위험", better: 1, ref: 0, fmt: v => pp(v, 2), axis: v => num(v, 1), get: pick("worst_day_pp"),
    help: "하루 손익 합(%p)의 최저값." },
  { key: "max_consecutive_losses", label: "최대 연속 손실", group: "위험", better: -1, fmt: v => int(v), axis: v => String(Math.round(v)), get: pick("max_consecutive_losses"),
    help: "청산 순서로 본 손실 거래의 최장 연속 횟수." },
  { key: "longest_drawdown_days", label: "최장 낙폭 기간", group: "위험", better: -1, fmt: v => (v == null ? "–" : `${int(v)}일`), axis: v => String(Math.round(v)), get: pick("longest_drawdown_days"),
    help: "고점을 회복하지 못한 가장 긴 영업일 수." },
  { key: "ulcer_index", label: "Ulcer index", group: "위험", better: -1, fmt: v => num(v, 1), axis: v => num(v, 0), get: pick("ulcer_index"),
    help: "낙폭 깊이와 기간을 함께 반영한 지표(낙폭 제곱평균의 제곱근, %p). 작을수록 좋다." },
  { key: "sortino", label: "Sortino (연율)", group: "위험", better: 1, ref: 0, fmt: v => num(v), axis: v => num(v, 0), get: pick("sortino_daily_ann"),
    help: "하락 변동성만으로 나눈 Sharpe." },
  { key: "calmar", label: "Calmar", group: "위험", better: 1, ref: 0, fmt: v => num(v), axis: v => num(v, 1), get: pick("calmar"),
    help: "누적 순수익 / 최대 낙폭." },
  { key: "total", label: "누적 순수익", group: "안정성", better: 1, ref: 0, fmt: v => pp(v), axis: v => num(v, 0), get: pick("total_return_pp", "sum_pp"),
    help: "거래당 1단위 손익의 합(%p). 계좌 수익률이 아니다." },
  { key: "positive_month_ratio", label: "플러스 달 비율", group: "안정성", better: 1, share: true, fmt: v => pct(v, 0), axis: v => pct(v, 0), get: pick("positive_month_ratio"),
    help: "거래가 있었던 달 중 손익 합이 플러스인 달의 비율." },
  { key: "half_gap", label: "후반 − 전반 평균", group: "안정성", better: 1, ref: 0, fmt: v => spct(v, 3), axis: v => spct(v, 1),
    get: m => { const a = n(m.mean_first_half), b = n(m.mean_second_half); return a != null && b != null ? b - a : null; },
    help: "거래를 시간 순서로 반으로 나눴을 때 후반 평균 − 전반 평균. 크게 음수면 성과가 식고 있다." },
  { key: "top_symbol_share", label: "최다 종목 비중", group: "안정성", better: -1, share: true, fmt: v => pct(v), axis: v => pct(v, 0), get: pick("top_symbol_share"),
    help: "가장 많이 거래한 한 종목이 전체 거래에서 차지하는 비율." },
  { key: "trades", label: "거래 수", group: "노출", better: 0, fmt: v => int(v), axis: v => String(Math.round(v)), get: pick("trades", "n"),
    help: "표본 크기. 작으면 다른 지표의 신뢰도가 낮다." },
  { key: "days", label: "활동일", group: "노출", better: 0, fmt: v => int(v), axis: v => String(Math.round(v)), get: pick("active_days", "days"),
    help: "거래가 있었던 날 수." },
  { key: "day_ratio", label: "거래일 비율", group: "노출", better: 0, share: true, fmt: v => pct(v, 0), axis: v => pct(v, 0), get: pick("day_ratio"),
    help: "평가 기간 영업일 중 거래가 있었던 날의 비율." },
  { key: "avg_hold_minutes", label: "평균 보유 시간", group: "노출", better: 0, fmt: v => (v == null ? "–" : `${num(v, 0)}분`), axis: v => num(v, 0), get: pick("avg_hold_minutes"),
    help: "진입부터 청산까지 평균 분." },
  { key: "p_value", label: "p (평균 ≤ 0)", group: "유의성", better: -1, fmt: v => prob(v), axis: v => num(v, 2), get: pick("p_value_mean_gt0"),
    help: "거래일 부트스트랩에서 평균이 0 이하로 나온 비율(단측). 작을수록 평균 > 0이 우연일 가능성이 낮다." },
  { key: "q_value", label: "보정 q (BH)", group: "유의성", better: -1, fmt: v => prob(v), axis: v => num(v, 2), get: pick("q_value"),
    help: "같은 구간·실험에서 비교한 모든 후보 수를 반영한 Benjamini–Hochberg 보정값. 0.05 미만이어야 유의." },
  { key: "deflated_sharpe", label: "Deflated Sharpe", group: "유의성", better: 1, share: true, fmt: v => pct(v, 0), axis: v => pct(v, 0), get: pick("deflated_sharpe"),
    help: "여러 후보 중 최고를 골랐다는 점을 보정한 '진짜 Sharpe > 0' 확률(Bailey & López de Prado)." },
  { key: "random_percentile", label: "무작위 대비 백분위", group: "유의성", better: 1, share: true, fmt: v => pct(v, 0), axis: v => pct(v, 0), get: pick("random_percentile"),
    help: "같은 청산 방식으로 모든 후보에 진입한 집합에서 같은 수를 무작위로 고른 1,000회 중 평균이 이 결과보다 낮았던 비율. 95% 이상이어야 고르는 능력이 있다고 볼 수 있다." },
];

export const metricByKey = Object.fromEntries(METRICS.map(m => [m.key, m])) as Record<string, MetricDef>;
export const COMPARE_DEFAULT = ["mean", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp", "cvar5", "positive_month_ratio", "random_percentile"];
