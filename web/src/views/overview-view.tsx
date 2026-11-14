"use client";
import Link from "next/link";
import { Badge, Loading, Panel, Stat, StatGrid } from "@/components/ui";
import { useIndex } from "@/lib/data";
import { int, pct, prob, spct, when } from "@/lib/format";
import { defaultSplit, Headline, inSplit, reproState, SPLIT, verdict } from "./common";

export function OverviewView() {
  const { data: idx, error } = useIndex();
  if (error) return <p className="text-fail">데이터를 불러오지 못했습니다: {error.message}</p>;
  if (!idx) return <Loading />;
  const track = Object.keys(idx.tracks)[0], split = defaultSplit(idx, track);
  const runs = inSplit(idx, track, split), models = runs.filter(r => r.run.model.family !== "baseline");
  const best = [...models].sort((a, b) => (b.metrics.mean_return ?? -9) - (a.metrics.mean_return ?? -9))[0];
  const repro = idx.runs.reduce((acc, r) => { const s = reproState(r); acc[s] = (acc[s] ?? 0) + 1; return acc; }, {} as Record<string, number>);
  const studies = [...idx.studies].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return (
    <>
      <Headline title={verdict(runs)} note={`${idx.tracks[track].title}, ${SPLIT[split] ?? split} 기준. 데이터 생성 ${when(idx.generated_at)}.`} />
      <div className="grid grid-cols-3 gap-4 max-xl:grid-cols-1">
        <Panel title="모델" description="같은 구간·같은 후보에서 플랫폼이 계산한 지표" actions={<Link href={`/runs/?split=${split}`} className="text-[13px]">순위 보기</Link>}>
          <StatGrid cols={2}>
            <Stat label="비교 중인 모델" value={int(models.length)} sub={`기준선 ${runs.length - models.length}개`} />
            <Stat label="보정 후 유의" value={int(models.filter(r => (r.metrics.q_value as number) < 0.05 && (r.metrics.mean_return ?? 0) > 0).length)} sub="q < 0.05" />
            <Stat label="평균이 가장 높은 모델" value={best ? spct(best.metrics.mean_return, 3) : "–"} sub={best?.run.run_id} />
            <Stat label="그 모델의 무작위 대비" value={best && best.metrics.random_percentile != null ? pct(best.metrics.random_percentile, 0) : "–"} sub="같은 청산 방식 무작위 선택 백분위" />
          </StatGrid>
        </Panel>
        <Panel title="조건 실험" description="다른 세션이 돌린 조건 × 모델 격자" actions={<Link href="/studies/" className="text-[13px]">실험 보기</Link>}>
          <ul className="m-0 list-none p-0">
            {studies.slice(0, 4).map(s => (
              <li key={s.study_id} className="border-b border-rule-2 py-2 last:border-b-0">
                <Link href={`/studies/view/?id=${encodeURIComponent(s.study_id)}`} className="font-medium text-ink no-underline hover:underline">{s.title}</Link>
                {s.locked_holdout && <Badge tone="warn">잠금 시험</Badge>}
                <div className="text-xs text-ink-3">조합 {int(s.trials)}개, 보정 후 유의 {int(s.significant_cells)}개, 최고 칸 {s.best_cell ? `${spct(s.best_cell.mean, 2)} (q=${prob(s.best_cell.q_value)})` : "–"}, 갱신 {when(s.updated_at)}</div>
              </li>
            ))}
            {!studies.length && <li className="text-[13px] text-ink-3">아직 없습니다.</li>}
          </ul>
        </Panel>
        <Panel title="재현성" description="기록된 커밋·데이터·명령으로 다시 만들어 같은 결과가 나오는지" actions={<Link href="/lab/" className="text-[13px]">조합 실험실</Link>}>
          <StatGrid cols={2}>
            <Stat label="재현됨" value={int(repro.reproduced ?? 0)} sub={`전체 run ${idx.runs.length}개`} tone={repro.reproduced === idx.runs.length ? "pass" : undefined} />
            <Stat label="확인 필요" value={int((repro.unchecked ?? 0) + (repro.partial ?? 0) + (repro.not_reproduced ?? 0) + (repro.missing ?? 0))} sub="미확인·부분·실패·기록 없음" />
          </StatGrid>
          <p className="mb-0 mt-3 text-[13px] text-ink-2">조합 실험실에서는 모델에 국면·시간대·요일 같은 조건을 바로 걸어 보고, 같은 수의 무작위 선택과 비교할 수 있습니다.</p>
        </Panel>
      </div>
    </>
  );
}
