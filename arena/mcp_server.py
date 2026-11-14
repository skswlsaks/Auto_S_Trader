"""Model Arena MCP server (stdio).

Lets a teammate's AI assistant read the shared leaderboard, studies and combination lab, write
results in the Arena Standard, and submit / reproduce them, using the teammate's own AWS
credentials for the shared store (ARENA_REMOTE). Run:

    uv run --directory <repo> --quiet python -m arena.mcp_server

Nothing here prints to stdout (stdout is the MCP channel).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import SCHEMA_VERSION
from .query import SharedData, lab_query

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE_LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SUBMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
REPO = "skswlsaks/Auto_S_Trader"

INSTRUCTIONS = """Model Arena: the team's shared evaluation platform for s_trader trading models.
All metrics are computed by the platform from submitted trade lists, so models and condition
experiments are compared the same way. Comparisons are only fair inside one track and split.

Typical flows
- Look: arena_overview -> arena_list_runs / arena_list_studies -> arena_get_run / arena_study_grid.
- Combine model x conditions: arena_list_lab_sources -> arena_lab_query (filters, group_by). Lab results
  are exploratory (not multiple-testing corrected); report the q-values from studies for decisions.
- Submit a model result: arena_schema -> arena_create_run -> arena_validate_run -> arena_submit_run.
- Submit a condition experiment folder: arena_preview_study -> arena_submit_study.
- Reproduce: arena_reproduce_run.
Rules: never pick conditions or thresholds on a locked holdout; returns are net of costs as
decimals (0.01 = 1%); unclosed trades stay in the denominator; a run_id is immutable once submitted.
"""

server = MCPServer("model-arena", title="Model Arena", instructions=INSTRUCTIONS, version="0.1.0")


def friendly(fn):
    """Turn input/data errors into tool errors whose message reaches the assistant (e.g. valid values)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, KeyError, FileNotFoundError, SystemExit) as e:
            raise ToolError(str(e.args[0]) if e.args else type(e).__name__) from e
    return wrapper
_data: SharedData | None = None


def data() -> SharedData:
    global _data
    if _data is None:
        _data = SharedData()
    return _data


def _remote() -> str:
    r = os.environ.get("ARENA_REMOTE")
    if not r:
        raise ValueError("ARENA_REMOTE is not set (s3://<arena bucket> or a shared folder). Ask the team for the value.")
    return r


KEY_METRICS = ("trades", "mean_return", "mean_return_ci95", "accuracy", "profit_rate_01", "profit_factor", "max_drawdown_pp",
               "sharpe_daily_ann", "cvar5", "payoff_ratio", "positive_month_ratio", "p_value_mean_gt0", "q_value",
               "deflated_sharpe", "random_percentile", "passes_gates")


def _row(r: dict) -> dict:
    m = r["metrics"]
    return {"run_id": r["run"]["run_id"], "family": r["run"]["model"]["family"], "split": r["run"]["split"],
            **{k: m.get(k) for k in KEY_METRICS},
            "reproduction": (r.get("reproduction") or {}).get("verdict") or ("unchecked" if r.get("has_provenance") else "missing")}


# ---------------------------------------------------------------- read tools

@server.tool(annotations=READ)
@friendly
def arena_overview() -> dict:
    """Tracks, evaluation splits, how many runs/studies exist, and when the data was built."""
    idx = data().index()
    splits = {}
    for r in idx["runs"]:
        k = f"{r['run']['track_id']}:{r['run']['split']}"
        splits[k] = splits.get(k, 0) + 1
    return {"store": data().where, "generated_at": idx["generated_at"],
            "tracks": {t: {"title": v["title"], "windows": v["windows"], "gates": v.get("gates")} for t, v in idx["tracks"].items()},
            "runs_per_split": splits, "studies": [{"study_id": s["study_id"], "title": s["title"], "trials": s["trials"],
                                                     "significant_cells": s["significant_cells"], "locked_holdout": s["locked_holdout"]} for s in idx["studies"]],
            "lab_sources": [s["source_id"] for s in idx["lab"]]}


@server.tool(annotations=READ)
@friendly
def arena_list_runs(split: str | None = None, track_id: str | None = None, family: str | None = None,
                    sort_by: str = "mean_return", limit: int = 30) -> dict:
    """Leaderboard rows for one track/split (default: first track, its dev-late split if present).
    family: rule | supervised | rl | baseline. sort_by: any metric key (descending)."""
    idx = data().index()
    track_id = track_id or next(iter(idx["tracks"]))
    if split is None:
        splits = {r["run"]["split"] for r in idx["runs"] if r["run"]["track_id"] == track_id}
        split = "dev-late" if "dev-late" in splits else next(iter(idx["tracks"][track_id]["windows"]))
    rows = [_row(r) for r in idx["runs"] if r["run"]["track_id"] == track_id and r["run"]["split"] == split
            and (family is None or r["run"]["model"]["family"] == family)]
    rows.sort(key=lambda x: (x.get(sort_by) is None, -(x.get(sort_by) or 0) if isinstance(x.get(sort_by), (int, float)) else 0))
    return {"track_id": track_id, "split": split, "sorted_by": sort_by, "count": len(rows), "runs": rows[:limit]}


@server.tool(annotations=READ)
@friendly
def arena_get_run(run_id: str) -> dict:
    """Full metrics, notes, params, model files, provenance (commit, command, inputs) and the latest reproduction report."""
    d = data().run(run_id)
    m = {k: v for k, v in d["metrics"].items() if k not in ("curve", "return_hist")}
    run = d["run"]
    return {"run": {k: run.get(k) for k in ("run_id", "track_id", "split", "model", "author", "created_at", "notes", "params", "source", "artifacts", "artifact_hash")},
            "metrics": m, "provenance": run.get("provenance"), "reproduction": d.get("reproduction"), "submission": d.get("submission")}


@server.tool(annotations=READ)
@friendly
def arena_compare_runs(run_ids: list[str], metrics: list[str] | None = None) -> dict:
    """Side-by-side metrics for up to 8 runs. Warns when runs come from different splits (unfair comparison)."""
    idx = {r["run"]["run_id"]: r for r in data().index()["runs"]}
    missing = [i for i in run_ids if i not in idx]
    keys = metrics or list(KEY_METRICS)
    rows = [{"run_id": i, **{k: idx[i]["metrics"].get(k) for k in keys}} for i in run_ids[:8] if i in idx]
    splits = {idx[i]["run"]["split"] for i in run_ids if i in idx}
    return {"rows": rows, "missing": missing, "warning": "runs come from different splits" if len(splits) > 1 else None}


@server.tool(annotations=READ)
@friendly
def arena_list_studies() -> dict:
    """Condition x model experiment grids: purpose, dimensions, number of cells compared, significant cells, best cell."""
    return {"studies": data().index()["studies"]}


@server.tool(annotations=READ)
@friendly
def arena_study_grid(study_id: str, rows: str | None = None, cols: str | None = None, metric: str = "mean",
                     fixed: dict[str, str] | None = None) -> dict:
    """One 2-D slice of a study: rows x cols cells for `metric` (mean, accuracy, profit_rate_01, profit_factor,
    max_drawdown_pp, sharpe_daily_ann, cvar5, q_value, n ...). Other dimensions are fixed (use "*" for "all"
    on aggregate dimensions). Each cell includes n, mean, 95% CI, q (BH over all cells) and significance."""
    s = data().study(study_id)
    dims = {d["key"]: d for d in s["dimensions"]}
    dflt = s.get("defaults") or {}
    rows = rows or dflt.get("rows") or s["dimensions"][0]["key"]
    cols = cols if cols is not None else dflt.get("cols")
    fx = {**(dflt.get("fixed") or {}), **(fixed or {})}
    for k, d in dims.items():
        if k not in (rows, cols) and k not in fx:
            fx[k] = "*" if d.get("aggregate") else d["values"][0]
    out = []
    for c in s["cells"]:
        if all(c["key"][k] == v for k, v in fx.items() if k not in (rows, cols)):
            out.append({"row": c["key"][rows], "col": c["key"].get(cols) if cols else None, "n": c["n"], metric: c.get(metric),
                        "mean": c["mean"], "mean_ci95": c["mean_ci95"], "q_value": c["q_value"], "significant": c["significant"]})
    return {"study": s["title"], "locked_holdout": s.get("locked_holdout", False), "trials": s["trials"], "rows": rows, "cols": cols,
            "fixed": {k: v for k, v in fx.items() if k not in (rows, cols)},
            "labels": {k: d.get("labels") for k, d in dims.items()}, "cells": out,
            "note": "q-values correct for every cell in the study; cells with n < 30 are unreliable."}


@server.tool(annotations=READ)
@friendly
def arena_list_lab_sources() -> dict:
    """Datasets for free model x condition combination: their model columns (role=model) and condition columns."""
    out = []
    for s in data().index()["lab"]:
        src = data().lab(s["source_id"])
        out.append({"source_id": s["source_id"], "title": s["title"], "n": s["n"], "locked_holdout": s["locked_holdout"],
                    "period": [s["first_day"], s["last_day"]],
                    "columns": [{"key": c["key"], "label": c["label"], "role": c["role"], "values": dict(zip(c["values"], c["labels"]))} for c in src["columns"]]})
    return {"sources": out}


@server.tool(annotations=READ)
@friendly
def arena_lab_query(source_id: str, filters: dict[str, Any] | None = None, group_by: list[str] | None = None) -> dict:
    """Combine a model with conditions and compute results now. filters: {column: [values or labels]}
    (model columns default to their first value). Returns the selection vs the same model without
    conditions, the percentile against 1,000 same-size random draws, and optional groups.
    Exploratory: no multiple-testing correction. Conditions are all known before entry."""
    src = data().lab(source_id)
    res = lab_query(src, filters, group_by)
    if src.get("locked_holdout"):
        res["warning"] = "locked holdout: do not choose conditions or thresholds from this data"
    return res


METRIC_HELP = {
    "mean_return": "비용 후 거래당 평균 수익률(소수). mean_return_ci95 = 거래일 단위 부트스트랩 95% 구간",
    "accuracy": "비용 포함 본절 이상(>=0)으로 전량 청산한 거래 / 전체 진입. 본절 0도 성공",
    "profit_rate_01": "비용 후 +0.1% 초과 거래 비율(연구 세션의 profit_share/win_rate)",
    "profit_rate": "비용 후 0 초과 거래 비율", "profit_factor": "이익 합 / 손실 합", "payoff_ratio": "평균 이익 / 평균 손실 크기",
    "max_drawdown_pp": "거래당 1단위 누적 곡선의 최대 낙폭(%p)", "sharpe_daily_ann": "일별 단위 손익 평균/표준편차 x sqrt(252)",
    "cvar5": "최악 5% 거래 평균", "worst_day_pp": "하루 손익 합 최저(%p)", "max_consecutive_losses": "최대 연속 손실 횟수",
    "longest_drawdown_days": "고점 미회복 최장 영업일", "ulcer_index": "낙폭 제곱평균의 제곱근(%p)", "calmar": "누적/최대 낙폭",
    "sortino_daily_ann": "하락 변동성 기준 Sharpe", "positive_month_ratio": "플러스 달 비율", "mean_ex_top5": "상위 5거래 제외 평균",
    "top_symbol_share": "최다 종목 거래 비중", "expectancy_r": "손절 거리 1R 기준 기대값", "p_value_mean_gt0": "평균<=0 부트스트랩 비율(단측)",
    "q_value": "같은 구간·실험의 모든 후보로 보정한 Benjamini-Hochberg q (<0.05 유의)",
    "deflated_sharpe": "여러 후보 중 최고를 고른 것을 보정한 P(진짜 Sharpe>0)",
    "random_percentile": "같은 청산 방식으로 모든 후보에 진입한 집합에서 같은 수를 무작위로 고른 1,000회 대비 백분위(>=0.95면 고르는 능력)",
}


@server.tool(annotations=READ)
@friendly
def arena_metric_definitions() -> dict:
    """What each metric means and which direction is better."""
    return METRIC_HELP


SCHEMA_DOC = f"""Arena Standard ({SCHEMA_VERSION})

Run folder: runs/<run_id>/
  run.json     required: schema_version, run_id, track_id, split (a key of the track windows), model.name,
               model.family (rule|supervised|rl|baseline), model.version, author, created_at, artifact_hash.
               optional: notes, params, source. Written by write_run: provenance (commit, env, command, inputs), artifacts.
  trades.json  list of trades. required: symbol, entry_at (ISO-8601 with timezone), exit_at (null if unclosed),
               net_return (net of costs, decimal 0.01 = 1%), complete (bool). optional: exit_reason, quantity,
               entry_price, exit_price, stop_price, signal_at, ctx.
  curve.json   optional training curve: [{{step, value, metric}}]
  artifacts/   optional model files (weights, configs)
Rules: entries must fall inside the split window (KST dates); unclosed trades count as failures for accuracy;
a submitted run_id is immutable (same result + more evidence = enrich, different result = new run_id).

Study folder (condition x model grid), read only by the platform:
  trades.parquet  one row per simulated trade with a return column (net), a date column (D), status (ok rows used)
                  and condition/model columns (entry_variant, policy, scope, gate_ok, risk_flag, period, kind ...)
  protocol.json   entries/exits descriptions, version; REPORT.md or summary.csv/json marks the run as finished
  study.json      optional explicit spec: {{"schema":"arena-study/v0","study_id","title","purpose",
                  "measure":{{"return","date","filter":{{"status":"ok"}}}},
                  "dimensions":[{{"key","label","aggregate":bool,"labels":{{value:label}}}}],
                  "defaults":{{"rows","cols","fixed":{{}}}},"locked_holdout":bool}}
  aggregate=true only for dimensions that partition one strategy's trades (gate on/off, period, risk flag).
"""


@server.tool(annotations=READ)
@friendly
def arena_schema() -> str:
    """The Arena Standard: run.json / trades.json / study.json formats and rules. Read before creating results."""
    return SCHEMA_DOC


@server.resource("arena://schema", name="Arena Standard", mime_type="text/plain")
def schema_resource() -> str:
    return SCHEMA_DOC


@server.resource("arena://metrics", name="Metric definitions", mime_type="application/json")
def metrics_resource() -> str:
    return json.dumps(METRIC_HELP, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- write / submit tools

def _load_trades(trades: list[dict] | None, trades_path: str | None) -> list[dict]:
    if trades is not None:
        return trades
    if not trades_path:
        raise ValueError("give trades (list) or trades_path (.json, .csv or .parquet)")
    p = Path(trades_path).expanduser()
    if p.suffix == ".json":
        return json.loads(p.read_text())
    import pandas as pd
    df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict("records")


@server.tool(annotations=WRITE_LOCAL)
@friendly
def arena_create_run(run_id: str, track_id: str, split: str, model_name: str, family: str, version: str, author: str,
                     trades: list[dict] | None = None, trades_path: str | None = None, params: dict | None = None,
                     notes: str | None = None, command: list[str] | None = None, inputs: list[str] | None = None,
                     artifacts: list[str] | None = None, artifact_hash: str | None = None, curve: list[dict] | None = None) -> dict:
    """Write a standard run into the local Arena cache (outputs/arena/runs/<run_id>) and validate it.
    command: the `arena` sub-command that regenerates it (for reproduce); inputs: data files used;
    artifacts: model files to store with the run. Provenance (commit, env, input hashes) is recorded."""
    from . import store
    from .provenance import collect
    from .store import sha, write_run
    RUNS = store.RUNS
    tr = _load_trades(trades, trades_path)
    arts = {Path(a).name: Path(a).expanduser() for a in (artifacts or [])}
    meta = {"run_id": run_id, "track_id": track_id, "split": split, "author": author,
            "model": {"name": model_name, "family": family, "version": version},
            "artifact_hash": artifact_hash or sha({"params": params, "artifacts": sorted(arts), "trades": len(tr)})}
    if params:
        meta["params"] = params
    if notes:
        meta["notes"] = notes
    path = write_run(meta, tr, curve, runs_dir=RUNS, artifacts=arts or None,
                     provenance=collect(command or ["(created via MCP; no regenerate command)"], [Path(i).expanduser() for i in (inputs or [])]))
    return {"path": str(path), **arena_validate_run(run_id)}


@server.tool(annotations=READ)
@friendly
def arena_validate_run(run_id: str) -> dict:
    """Check a local run against the Arena Standard and preview its platform metrics."""
    from . import store
    from .metrics import score
    from .schema import load_run_dir, load_tracks, validate_run
    d = store.RUNS / run_id
    TRACKS = store.TRACKS
    if not (d / "run.json").exists():
        return {"ok": False, "errors": [f"no local run {d}"]}
    tracks = load_tracks(TRACKS)
    run, trades, curve = load_run_dir(d)
    errors = validate_run(run, trades, tracks, curve)
    out = {"ok": not errors, "errors": errors}
    if not errors:
        m = score(trades, tracks[run["track_id"]], run["split"])
        out["metrics_preview"] = {k: m.get(k) for k in KEY_METRICS if k in m}
        out["provenance_dirty"] = (run.get("provenance") or {}).get("code", {}).get("dirty")
    return out


@server.tool(annotations=SUBMIT)
@friendly
def arena_submit_run(run_id: str) -> dict:
    """Validate and upload a local run to the shared store. Immutable: a different result under the same
    run_id is rejected; the same result with more evidence is accepted as 'enriched'."""
    from . import remote, store
    try:
        return remote.submit(run_id, _remote(), store.RUNS)
    except SystemExit as e:
        return {"status": "rejected", "reason": str(e)}


@server.tool(annotations=READ)
@friendly
def arena_preview_study(folder: str) -> dict:
    """Interpret an experiment folder (study.json, built-in adapter or inference) and compute its grid locally,
    without uploading. Use to check dimensions and numbers before arena_submit_study."""
    import tempfile
    from .studies import build_study, load_spec
    spec = load_spec(Path(folder).expanduser())
    with tempfile.TemporaryDirectory() as tmp:
        s = build_study(Path(folder).expanduser(), Path(tmp))
    best = sorted((c for c in s["cells"] if c["n"] >= 30), key=lambda c: -c["mean"])[:5]
    return {"study_id": spec["study_id"], "how": "study.json" if (Path(folder).expanduser() / "study.json").exists() else ("inferred" if spec.get("inferred") else "adapter"),
            "dimensions": [{"key": d["key"], "label": d["label"], "aggregate": d.get("aggregate"), "values": d["values"]} for d in s["dimensions"]],
            "n_trades": s["n_trades"], "cells": s["trials"], "significant_cells": s["significant_cells"],
            "top_cells_n30": [{"key": c["key"], "n": c["n"], "mean": c["mean"], "q_value": c["q_value"]} for c in best],
            "locked_holdout": s.get("locked_holdout", False)}


@server.tool(annotations=SUBMIT)
@friendly
def arena_submit_study(folder: str, request_refresh: bool = True) -> dict:
    """Upload an experiment folder's study inputs to the shared store (the folder itself is not modified).
    The dashboard shows it after the next refresh (requested now when the GitHub CLI is available, else within 30 min)."""
    from . import remote
    from .studies import add_local
    dest = add_local(Path(folder).expanduser())
    remote.backend(_remote()).upload_dir(dest, f"studies/{dest.name}")
    out = {"status": "submitted", "study_id": dest.name}
    if request_refresh:
        out["refresh"] = arena_request_refresh()
    return out


@server.tool(annotations=SUBMIT)
@friendly
def arena_request_refresh() -> str:
    """Ask GitHub Actions to rebuild dashboard data now (needs the `gh` CLI with access; otherwise it refreshes every 30 minutes)."""
    if not shutil.which("gh"):
        return "gh CLI not found: the dashboard refreshes automatically within 30 minutes"
    r = subprocess.run(["gh", "workflow", "run", "refresh.yml", "--repo", REPO], capture_output=True, text=True)
    return "refresh requested (about 1-2 minutes)" if r.returncode == 0 else f"could not request refresh ({r.stderr.strip()[:160]}); it refreshes within 30 minutes"


@server.tool(annotations=SUBMIT)
@friendly
def arena_reproduce_run(run_id: str, retrain: bool = False, record: bool = True) -> dict:
    """Check that a run can be regenerated: input data hashes, replay of the stored model, and (retrain=true)
    re-running the recorded command at the recorded commit. Downloads the run from the store if missing.
    record=true uploads the report to the shared store."""
    from . import remote, store
    from .reproduce import reproduce
    RUNS = store.RUNS
    if not (RUNS / run_id / "run.json").exists():
        b = remote.backend(_remote())
        tmp = RUNS / run_id
        for name in ("run.json", "trades.json", "curve.json"):
            obj = b.read_json(run_id, name)
            if obj is not None:
                tmp.mkdir(parents=True, exist_ok=True)
                (tmp / name).write_text(json.dumps(obj, ensure_ascii=False))
        if not (tmp / "run.json").exists():
            return {"verdict": "error", "reason": f"run {run_id} not found locally or in the store"}
    rep = reproduce(run_id, runs_dir=RUNS, retrain=retrain)
    if record and os.environ.get("ARENA_REMOTE"):
        latest = sorted((RUNS / run_id / "reproductions").glob("*.json"))[-1]
        remote.record_reproduction(run_id, latest, os.environ["ARENA_REMOTE"])
    return rep


# ---------------------------------------------------------------- prompt

@server.prompt(title="Submit results to Model Arena")
def submit_results(kind: str = "run") -> str:
    """Step-by-step guide for submitting a model run or a condition experiment."""
    if kind == "study":
        return ("Submit a condition x model experiment to Model Arena.\n"
                "1. Make sure the experiment folder has trades.parquet, protocol.json and REPORT.md (or summary.csv).\n"
                "2. Call arena_preview_study(folder) and check the dimensions, labels and top cells with me.\n"
                "3. If a dimension is wrong, write study.json in the folder (see arena_schema) and preview again.\n"
                "4. Call arena_submit_study(folder). Report the study_id and that the dashboard updates shortly.\n"
                "Never tune on a locked holdout folder.")
    return ("Submit a model result to Model Arena.\n"
            "1. Call arena_schema and arena_overview; choose the track and split the run belongs to.\n"
            "2. Build the trade list (net of costs, decimals, ISO times with timezone, unclosed trades kept).\n"
            "3. Call arena_create_run with a new run_id, the regenerate command, input files and model files.\n"
            "4. Fix any errors from arena_validate_run, show me the metrics preview, then call arena_submit_run.\n"
            "5. Optionally arena_reproduce_run(run_id) and compare with peers via arena_list_runs.")


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
