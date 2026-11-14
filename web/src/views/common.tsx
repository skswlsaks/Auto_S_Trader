"use client";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { Badge, Tag } from "@/components/ui";
import type { ArenaIndex, RunSummary } from "@/lib/types";

export const FAMILY: Record<string, string> = { rule: "규칙", supervised: "지도학습", rl: "강화학습", baseline: "기준선" };
export const SPLIT: Record<string, string> = { dev: "개발 구간", "dev-late": "개발 후반 구간", holdout: "시험 구간" };

/** URL query state: shareable links reproduce the same screen. */
export function useQuery() {
  const params = useSearchParams(), router = useRouter(), path = usePathname();
  const get = useCallback((k: string) => params.get(k), [params]);
  const set = useCallback((patch: Record<string, string | null | undefined>) => {
    const p = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) (v == null || v === "" ? p.delete(k) : p.set(k, v));
    const qs = p.toString();
    router.replace(qs ? `${path}?${qs}` : path, { scroll: false });
  }, [params, router, path]);
  return { get, set };
}

export function defaultSplit(idx: ArenaIndex, track: string) {
  const ws = Object.keys(idx.tracks[track].windows);
  const count = (s: string) => idx.runs.filter(r => r.run.track_id === track && r.run.split === s).length;
  return ws.includes("dev-late") && count("dev-late") ? "dev-late" : ws[0];
}

export const inSplit = (idx: ArenaIndex, track: string, split: string) => idx.runs.filter(r => r.run.track_id === track && r.run.split === split);
export function baselineBand(runs: RunSummary[], key = "mean_return"): [number, number] | null {
  const v = runs.filter(r => r.run.model.family === "baseline").map(r => r.metrics[key] as number).filter(x => typeof x === "number");
  return v.length ? [Math.min(...v), Math.max(...v)] : null;
}

export function verdict(runs: RunSummary[]): string {
  const models = runs.filter(r => r.run.model.family !== "baseline"), band = baselineBand(runs);
  if (!models.length) return "이 구간에 제출된 모델이 아직 없습니다.";
  const sig = models.filter(r => (r.metrics.q_value as number | null) != null && (r.metrics.q_value as number) < 0.05 && (r.metrics.mean_return ?? 0) > 0).length;
  if (sig) return `${models.length}개 모델 중 ${sig}개가 여러 후보 비교를 보정한 뒤에도 평균 수익이 0보다 큽니다.`;
  if (!band) return `${models.length}개 모델을 비교합니다. 보정 후 유의한 모델은 없습니다.`;
  const sure = models.filter(r => (r.metrics.mean_return_ci95[0] ?? -1) > band[1]).length;
  const above = models.filter(r => (r.metrics.mean_return ?? -1) > band[1]).length;
  if (sure) return `${models.length}개 모델 중 ${sure}개가 신뢰구간까지 무작위 기준선을 앞서지만, 보정 후 유의한 모델은 없습니다.`;
  if (above) return `${models.length}개 모델 중 ${above}개가 평균으로는 기준선보다 높지만, 신뢰구간까지 앞선 모델은 없습니다.`;
  return `${models.length}개 모델 모두 무작위 기준선 범위 안이거나 그보다 낮습니다.`;
}

export const REPRO: Record<string, ["pass" | "warn" | "fail" | "none", string]> = {
  reproduced: ["pass", "재현됨"], partial: ["warn", "부분 확인"], not_reproduced: ["fail", "재현 실패"], unchecked: ["none", "미확인"], missing: ["none", "기록 없음"],
};
export const reproState = (r: Pick<RunSummary, "reproduction" | "has_provenance">) => r.reproduction ? r.reproduction.verdict : r.has_provenance ? "unchecked" : "missing";
export function ReproBadge({ r }: { r: Pick<RunSummary, "reproduction" | "has_provenance"> }) { const [t, l] = REPRO[reproState(r)]; return <Badge tone={t}>{l}</Badge>; }
export function GateBadge({ ok }: { ok: boolean }) { return ok ? <Badge tone="pass">기준 충족</Badge> : <Badge tone="fail">기준 미달</Badge>; }
export function FamilyTag({ family }: { family: string }) { return <Tag tone={family === "rl" ? "accent" : "neutral"}>{FAMILY[family] ?? family}</Tag>; }

export function Headline({ title, note, children }: { title: string; note?: string; children?: React.ReactNode }) {
  return (
    <section className="mb-5 grid grid-cols-[minmax(0,1fr)_auto] items-end gap-x-6 gap-y-2 max-md:grid-cols-1">
      <h1 className="m-0 max-w-[64ch] text-[22px] font-semibold leading-snug tracking-tight">{title}</h1>
      <div className="row-span-2 flex flex-wrap items-center gap-2.5 max-md:row-span-1">{children}</div>
      {note && <p className="m-0 max-w-[90ch] text-[13px] text-ink-2">{note}</p>}
    </section>
  );
}
