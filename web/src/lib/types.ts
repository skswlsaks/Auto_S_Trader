export type Family = "rule" | "supervised" | "rl" | "baseline";

export interface Metrics {
  trades: number; closed: number; unclosed: number; active_days: number;
  accuracy: number | null; accuracy_ci95: [number | null, number | null];
  win_rate: number | null; mean_return: number | null; mean_return_ci95: [number | null, number | null];
  median_return: number | null; mean_win: number | null; mean_loss: number | null;
  profit_factor: number | null; worst_trade: number | null; best_trade: number | null;
  total_return_pp: number; max_drawdown_pp: number; sharpe_daily_ann: number | null;
  gates: Record<string, boolean>; passes_gates: boolean;
  curve?: [string, number][]; spark?: number[];
  return_hist?: { edges: number[]; counts: number[] }; exit_reasons?: Record<string, number>;
  monthly_pp?: Record<string, number>;
  [k: string]: unknown;
}

export interface Run {
  run_id: string; track_id: string; split: string; created_at: string; author: string; artifact_hash: string;
  model: { name: string; family: Family; version: string; [k: string]: unknown };
  params?: Record<string, unknown>; notes?: string; source?: string;
  artifacts?: { name: string; sha256: string; bytes: number }[];
  provenance?: Provenance;
}

export interface Provenance {
  code: { repo: string | null; commit: string | null; branch: string | null; dirty: boolean; dirty_files: string[] };
  env: { python: string; platform: string; packages: Record<string, string>; uv_lock_sha256: string | null };
  command: string[]; inputs: { path: string; sha256: string; bytes: number }[];
}

export interface Reproduction {
  run_id: string; checked_at: string; checked_by: string; commit: string | null;
  verdict: "reproduced" | "partial" | "not_reproduced";
  checks: { name: string; status: "pass" | "fail" | "warn" | "skip"; detail: string }[];
}

export interface Submission { submitted_by: string; submitted_at: string; enriched_by?: string; enriched_at?: string }

export interface RunSummary {
  run: Run; metrics: Metrics; has_provenance: boolean; submission: Submission | null;
  reproduction: { verdict: Reproduction["verdict"]; checked_at: string } | null;
}
export interface RunDetail { run: Run; metrics: Metrics; train_curve: { step: number; value: number; metric?: string }[] | null; submission: Submission | null; reproduction: Reproduction | null }

export interface Track {
  track_id: string; title: string; universe: string; decision_rule: string;
  windows: Record<string, { start: string | null; end: string | null; note?: string }>;
  gates: Record<string, number>; gates_by_split?: Record<string, Record<string, number>>;
}

export interface StudyIndexItem {
  study_id: string; title: string; purpose: string; updated_at: string; built_at: string; n_trades: number; n_days: number;
  first_day: string; last_day: string; trials: number; significant_cells: number; source: string; locked_holdout: boolean;
  best_cell: { id: string; key: Record<string, string>; mean: number; n: number; q_value: number | null } | null;
  dimensions: { key: string; label: string; n: number }[];
}

export interface StudyDimension { key: string; label: string; aggregate: boolean; values: string[]; labels?: Record<string, string> }
export interface StudyCell {
  id: string; key: Record<string, string>; base: boolean; n: number; days: number; mean: number; median: number;
  mean_ci95: [number | null, number | null]; accuracy: number; profit_rate: number | null; profit_rate_01: number | null;
  profit_factor: number | null; sum_pp: number; max_drawdown_pp: number | null; sharpe_daily_ann: number | null;
  q_value: number | null; significant: boolean; deflated_sharpe: number | null; p_value_mean_gt0: number | null;
  [k: string]: unknown;
}
export interface Study extends Omit<StudyIndexItem, "dimensions"> {
  dimensions: StudyDimension[]; cells: StudyCell[]; notes?: string | null;
  defaults?: { rows: string; cols: string; fixed: Record<string, string> };
  holdout?: { D_from: string; D_to: string; note: string }; has_report: boolean;
}
export interface CellDetail { curve: [string, number][]; monthly_pp: Record<string, number>; return_hist: { edges: number[]; counts: number[] }; exit_reasons?: Record<string, number> }

export interface LabColumn { key: string; label: string; role: "model" | "condition"; help?: string; values: string[]; labels: string[]; codes: number[] }
export interface LabSource { source_id: string; title: string; kind: "study" | "runs"; n: number; days: string[]; day: number[]; ret: number[]; columns: LabColumn[]; locked_holdout: boolean; study_id?: string; track_id?: string; split?: string }
export interface LabIndexItem { source_id: string; file: string; title: string; kind: "study" | "runs"; n: number; locked_holdout: boolean; first_day: string; last_day: string; columns: { key: string; label: string; role: string; n: number }[] }

export interface ArenaIndex {
  generated_at: string; store: string | null; tracks: Record<string, Track>; runs: RunSummary[];
  rejected: Record<string, string[]>; studies: StudyIndexItem[]; lab: LabIndexItem[];
}
