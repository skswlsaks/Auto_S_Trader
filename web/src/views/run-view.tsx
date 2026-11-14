"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { CurveChart, Histogram, MonthlyHeatmap, TrainCurve } from "@/components/charts";
import { useColors } from "@/components/charts/echart";
import { Badge, Button, Callout, CopyField, Empty, KV, Loading, MetricLabel, Panel, Stat, StatGrid, Tabs, TabPanel } from "@/components/ui";
import { useCompare } from "@/lib/compare";
import { useIndex, useRun } from "@/lib/data";
import { bytes, shortHash, when } from "@/lib/format";
import { METRICS, metricByKey, type Group } from "@/lib/metric-defs";
import type { RunDetail } from "@/lib/types";
import { FamilyTag, GateBadge, REPRO, ReproBadge, SPLIT, useQuery } from "./common";

const CHECK_NAMES: Record<string, string> = { inputs: "입력 데이터", artifact: "저장된 모델로 재생", retrain: "커밋에서 다시 학습", clean_commit: "커밋 상태", provenance: "재현 정보" };

function MetricTable({ m, group }: { m: Record<string, unknown>; group: Group }) {
  return (
    <table className="w-full border-collapse text-[13px]">
      <tbody>
        {METRICS.filter(d => d.group === group).map(d => {
          const ci = d.ci?.(m);
          return (
            <tr key={d.key} className="border-b border-rule-2">
              <td className="py-2 pr-3"><MetricLabel def={d} /></td>
              <td className="py-2 text-right font-semibold">{d.fmt(d.get(m))}</td>
              <td className="w-[40%] py-2 pl-3 text-right text-xs text-ink-3">{ci && ci[0] != null ? `95% ${d.fmt(ci[0])} … ${d.fmt(ci[1])}` : ""}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Repro({ d, store }: { d: RunDetail; store: string | null }) {
  const { run, reproduction: rep } = d, p = run.provenance, id = run.run_id;
  return (
    <>
      {rep ? (
        <>
          <p className="mb-2 text-[13px] text-ink-2"><Badge tone={REPRO[rep.verdict][0]}>{REPRO[rep.verdict][1]}</Badge> {when(rep.checked_at)}, {rep.checked_by} 확인</p>
          <ul className="m-0 mb-1 list-none p-0">
            {rep.checks.map(c => (
              <li key={c.name} className="grid grid-cols-[22px_minmax(0,1fr)] gap-2 border-b border-rule-2 py-2 text-[13px]">
                <span className={{ pass: "text-pass", fail: "text-fail", warn: "text-warn", skip: "text-ink-3" }[c.status] + " text-center font-bold"}>{{ pass: "✓", fail: "✕", warn: "!", skip: "–" }[c.status]}</span>
                <div><div className="font-semibold">{CHECK_NAMES[c.name] ?? c.name}</div><div className="text-ink-2">{c.detail}</div></div>
              </li>
            ))}
          </ul>
        </>
      ) : p ? <Callout tone="info">아직 재현을 확인하지 않았습니다. 아래 명령으로 확인하면 결과가 여기에 표시됩니다.</Callout> : null}
      {!p ? <Callout>이 run에는 재현 정보가 없습니다. 같은 명령으로 다시 만들어 제출하면 코드 버전, 데이터 해시, 모델 파일이 함께 기록됩니다.</Callout> : (
        <>
          <h3 className="mb-1 mt-4 text-[13px] font-semibold">재현 확인</h3>
          <CopyField text={`uv run python -m arena reproduce ${id}`} />
          <h3 className="mb-2 mt-4 text-[13px] font-semibold">코드</h3>
          {p.code.dirty && <Callout>만들 당시 커밋되지 않은 변경이 있었습니다: {p.code.dirty_files.slice(0, 4).join(", ")}. 커밋만으로는 같은 결과가 나오지 않을 수 있습니다.</Callout>}
          <KV items={[["커밋", p.code.repo && p.code.commit ? <a href={`${p.code.repo}/commit/${p.code.commit}`} target="_blank" rel="noopener">{p.code.commit.slice(0, 10)}</a> : shortHash(p.code.commit)], ["브랜치", p.code.branch ?? "–"]]} />
          <h3 className="mb-1 mt-4 text-[13px] font-semibold">다시 만드는 명령</h3>
          {p.code.commit && <CopyField text={`git checkout ${p.code.commit}`} />}
          <CopyField text={`uv run python -m arena ${p.command.join(" ")}`} />
          {!!run.artifacts?.length && (
            <>
              <h3 className="mb-1 mt-4 text-[13px] font-semibold">모델 파일</h3>
              <ul className="m-0 list-none p-0 text-[13px]">{run.artifacts.map(a => <li key={a.name} className="flex justify-between gap-3 border-b border-rule-2 py-1.5"><span>{a.name}</span><span className="text-xs text-ink-3">{bytes(a.bytes)}, {shortHash(a.sha256)}</span></li>)}</ul>
              {store && <CopyField text={`aws s3 cp ${store}/runs/${id}/artifacts/ ./${id}/ --recursive`} />}
            </>
          )}
          <h3 className="mb-1 mt-4 text-[13px] font-semibold">입력 데이터</h3>
          <ul className="m-0 list-none p-0 text-[13px]">{p.inputs.map(i => <li key={i.path} className="flex justify-between gap-3 border-b border-rule-2 py-1.5"><span className="truncate">{i.path.split("/").slice(-2).join("/")}</span><span className="flex-none text-xs text-ink-3">{bytes(i.bytes)}, {shortHash(i.sha256)}</span></li>)}</ul>
          <h3 className="mb-2 mt-4 text-[13px] font-semibold">실행 환경</h3>
          <KV items={[["Python", `${p.env.python} (${p.env.platform})`], ["패키지", Object.entries(p.env.packages).map(([k, v]) => `${k} ${v}`).join(", ")], ["uv.lock", shortHash(p.env.uv_lock_sha256)]]} />
        </>
      )}
    </>
  );
}

export function RunView() {
  const q = useQuery(), id = q.get("id"), cmp = useCompare(), colors = useColors();
  const { data, error } = useRun(id), { data: idx } = useIndex();
  const [tab, setTab] = useState(q.get("tab") ?? "perf");
  const baselines = useMemo(() => !idx || !data ? [] : idx.runs.filter(r => r.run.track_id === data.run.track_id && r.run.split === data.run.split && r.run.model.family === "baseline").map(r => r.run.run_id), [idx, data]);
  const baseDetails = useBaselineCurves(baselines);
  if (!id) return <Empty title="run을 고르세요" action={<Link href="/runs/">모델 순위로</Link>} />;
  if (error) return <Callout tone="fail">이 run을 불러오지 못했습니다 ({error.message}). 이름을 확인하거나 <Link href="/runs/">모델 순위</Link>에서 고르세요.</Callout>;
  if (!data || !colors) return <Loading />;
  const { run, metrics: m } = data, M = m as unknown as Record<string, unknown>;
  const color = colors[`series-${(cmp.slot(run.run_id) ?? 0) + 1}`];
  return (
    <>
      <div className="mb-1 text-[13px]"><Link href={`/runs/?split=${run.split}`} className="text-ink-2 no-underline hover:text-ink">← 모델 순위</Link></div>
      <header className="mb-4 flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="m-0 break-all text-[22px] font-semibold tracking-tight">{run.run_id}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-1.5"><FamilyTag family={run.model.family} /><Badge tone="none">{SPLIT[run.split] ?? run.split}</Badge><GateBadge ok={m.passes_gates} /><ReproBadge r={{ reproduction: data.reproduction ? { verdict: data.reproduction.verdict, checked_at: data.reproduction.checked_at } : null, has_provenance: !!run.provenance }} /></div>
          {run.notes && <p className="mb-0 mt-2 max-w-[90ch] text-[13px] text-ink-2">{run.notes}</p>}
        </div>
        <Button variant={cmp.has(run.run_id) ? "default" : "primary"} onClick={() => cmp.toggle(run.run_id)}>{cmp.has(run.run_id) ? "비교에서 빼기" : "비교에 추가"}</Button>
      </header>
      <StatGrid cols={4}>
        {(["mean", "accuracy", "profit_rate_01", "max_drawdown_pp"] as const).map(k => {
          const d = metricByKey[k], ci = d.ci?.(M);
          return <Stat key={k} label={<MetricLabel def={d} />} value={d.fmt(d.get(M))} sub={ci && ci[0] != null ? `95% ${d.fmt(ci[0])} … ${d.fmt(ci[1])}` : k === "max_drawdown_pp" ? `거래 ${m.trades}건, 활동일 ${m.active_days}일` : undefined} />;
        })}
      </StatGrid>
      <div className="mt-4 grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-4 max-xl:grid-cols-1">
        <Panel title="누적 순수익률과 낙폭" description="거래당 1단위를 청산일에 더한 값(%p). 회색 점선은 같은 구간 무작위 기준선. 아래 막대로 기간을 확대합니다.">
          {m.curve && <CurveChart items={[{ id: run.run_id, label: run.run_id, color, curve: m.curve }]} baselines={baseDetails} height={380} />}
        </Panel>
        <Panel pad={false}>
          <Tabs value={tab} onChange={v => { setTab(v); q.set({ tab: v === "perf" ? null : v }); }}
            tabs={[{ value: "perf", label: "성과" }, { value: "risk", label: "위험" }, { value: "stable", label: "안정성" }, { value: "sig", label: "유의성" }, { value: "repro", label: "재현" }, { value: "info", label: "정보" }]}>
            <TabPanel value="perf">
              <MetricTable m={M} group="핵심" /><div className="h-3" /><MetricTable m={M} group="수익의 질" />
              <h3 className="mb-1 mt-4 text-[13px] font-semibold">거래별 순수익률 분포</h3>
              {m.return_hist && <Histogram hist={m.return_hist} />}
              <h3 className="mb-1 mt-3 text-[13px] font-semibold">청산 사유</h3>
              <table className="w-full text-[13px]"><tbody>{Object.entries(m.exit_reasons ?? {}).map(([k, v]) => (
                <tr key={k} className="border-b border-rule-2"><td className="py-1.5">{k}</td><td className="w-[40%]"><span className="block h-1.5 rounded bg-ink-3/50" style={{ width: `${(v / Math.max(...Object.values(m.exit_reasons ?? { a: 1 }))) * 100}%` }} /></td><td className="py-1.5 text-right text-ink-2">{v}건</td></tr>))}</tbody></table>
              {data.train_curve && <><h3 className="mb-1 mt-4 text-[13px] font-semibold">학습 곡선</h3><TrainCurve curve={data.train_curve} /></>}
            </TabPanel>
            <TabPanel value="risk"><MetricTable m={M} group="위험" /></TabPanel>
            <TabPanel value="stable">
              <MetricTable m={M} group="안정성" />
              <h3 className="mb-1 mt-4 text-[13px] font-semibold">월별 손익</h3>
              {m.monthly_pp && <MonthlyHeatmap items={[{ label: "월별 %p", monthly: m.monthly_pp }]} />}
              <div className="h-3" /><MetricTable m={M} group="노출" />
            </TabPanel>
            <TabPanel value="sig">
              <Callout tone="info">같은 구간에서 비교한 모델 {String(M.trials ?? "–")}개를 함께 고려한 값입니다. 여러 후보 중 가장 좋아 보이는 것을 고르면 우연히 좋은 결과를 고를 수 있어서 보정합니다.</Callout>
              <MetricTable m={M} group="유의성" />
              {typeof M.random_pool === "string" && <p className="mt-2 text-xs text-ink-3">무작위 비교 대상: {M.random_pool as string}</p>}
            </TabPanel>
            <TabPanel value="repro"><Repro d={data} store={idx?.store ?? null} /></TabPanel>
            <TabPanel value="info">
              <KV items={[["모델", `${run.model.name} (${run.model.version})`], ["작성자", run.author],
                ["제출", data.submission ? `${data.submission.submitted_by}, ${when(data.submission.submitted_at)}${data.submission.enriched_at ? ` (재현 정보 추가 ${when(data.submission.enriched_at)})` : ""}` : "공유 저장소에 제출되지 않음"],
                ["생성", when(run.created_at)], ...(run.source ? [["원 실험", run.source] as [string, string]] : []), ["모델 해시", shortHash(run.artifact_hash)]]} />
              {run.params && <><h3 className="mb-2 mt-4 text-[13px] font-semibold">설정</h3>
                <KV items={Object.entries(run.params).filter(([k]) => k !== "take_minus_skip_weights").map(([k, v]) => [k, Array.isArray(v) && v.length > 6 ? `${v.length}개: ${v.slice(0, 6).join(", ")} …` : typeof v === "object" ? JSON.stringify(v) : String(v)])} /></>}
              <h3 className="mb-2 mt-4 text-[13px] font-semibold">판정 기준</h3>
              <KV items={Object.entries(m.gates).map(([k, v]) => [k, v ? "충족" : "미달"])} />
            </TabPanel>
          </Tabs>
        </Panel>
      </div>
    </>
  );
}

/** Baseline curves for the chart background (loaded lazily, at most five). */
function useBaselineCurves(ids: string[]) {
  const a = useRun(ids[0] ?? null), b = useRun(ids[1] ?? null), c = useRun(ids[2] ?? null), d = useRun(ids[3] ?? null), e = useRun(ids[4] ?? null);
  return useMemo(() => [a, b, c, d, e].map(x => x.data).filter((x): x is RunDetail => !!x?.metrics.curve).map(x => ({ id: x.run.run_id, curve: x.metrics.curve! })), [a.data, b.data, c.data, d.data, e.data]);
}
