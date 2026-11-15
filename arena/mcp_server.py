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

import base64
import functools
import re

from mcp.server.mcpserver import Context, MCPServer
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
- "Which combination is best?": arena_search_combos (rank on one period, check on a later one and
  on the locked holdout via holdout_source_id). Report robust picks with their test/holdout numbers.
- Submit a model result: arena_schema -> arena_create_run -> arena_validate_run -> arena_submit_run.
- Submit a condition experiment folder: arena_preview_study -> arena_submit_study.
- Reproduce: arena_reproduce_run (local server only).
- Identity: arena_whoami. When hosted, submissions are recorded under the signed-in Cognito user.
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


def _slim(st: dict | None) -> dict | None:
    if not st:
        return st
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in st.items() if k != "sum_pp"}


@server.tool(annotations=READ)
@friendly
def arena_search_combos(source_id: str, max_conditions: int = 2, split_date: str | None = None, split_column: str | None = None,
                        select_values: list[str] | None = None, test_values: list[str] | None = None,
                        holdout_source_id: str | None = None, min_trades: int = 25, min_days: int = 10,
                        models: list[str] | None = None, conditions: list[str] | None = None,
                        sort: str = "sel", top: int = 15) -> dict:
    """Find the best model x condition combinations honestly. Tries every model value with up to
    max_conditions (0-2) condition values, ranks them on a SELECTION period, corrects for the number
    tried (BH q over a day-clustered test of mean > 0), then shows each pick on a later TEST period it
    was not chosen on, and optionally on another source (e.g. the locked holdout) matched by values.
    Split: split_date "YYYY-MM-DD" (before = selection, from = test; default: last third of days is
    test) or split_column + select_values + test_values (e.g. "period", ["pre"], ["post"]).
    sort: sel (selection mean) | test | worst (min of both; uses the test period, so then only the
    holdout is independent) | q. robust = q < 0.05 on selection AND test mean > 0 (test n >= 10).
    models/conditions restrict the columns (default: all; calendar and date-splitting columns skipped)."""
    from . import search as sr
    src = data().lab(source_id)
    if src.get("locked_holdout"):
        raise ValueError("this is a locked holdout: search a development source and pass this one as holdout_source_id")
    if split_column:
        if not (select_values and test_values):
            raise ValueError("split_column needs select_values and test_values")
        split = {"by": "column", "key": split_column, "select": select_values, "test": test_values}
    else:
        split = {"by": "date", "cut": split_date} if split_date else None
    hold = data().lab(holdout_source_id) if holdout_source_id else None
    res = sr.search(src, model_keys=models, cond_keys=conditions, max_conds=max_conditions, split=split, min_n=min_trades,
                    min_days=min_days, holdout=hold, sort=sort, top=max(1, min(top, 100)))
    for r in res["rows"]:
        r["sel"], r["test"] = _slim(r["sel"]), _slim(r["test"])
        if "holdout" in r:
            r["holdout"] = _slim(r["holdout"])
        r.pop("model"), r.pop("conds")
        r["model"], r["conditions"] = r.pop("model_labels"), r.pop("cond_labels")
    res["source_id"], res["holdout_source_id"] = source_id, holdout_source_id
    res["read_me"] = ("mean = net return per trade (0.01 = 1%). Judge by robust + test + holdout, not the top selection mean: "
                      "the highest selection means are usually the most overfit. aliases = other labels reaching the same trades.")
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


# ---------------------------------------------------------------- identity / remote mode

REMOTE_MODE = os.environ.get("ARENA_MODE") == "remote"  # running on Bedrock AgentCore behind the Cognito JWT authorizer


def caller(ctx: Context | None) -> dict | None:
    """Identity from the bearer token. In remote mode AgentCore has already verified the token
    (issuer, audience, signature, expiry) before the request reaches this container."""
    if ctx is None:
        return None
    try:
        headers = ctx.headers or {}
    except (ValueError, AttributeError):
        return None
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        payload = auth.split()[1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    return {"email": claims.get("email"), "sub": claims.get("sub"), "name": claims.get("name") or claims.get("cognito:username")}


def _who(ctx) -> str | None:
    c = caller(ctx)
    return (c or {}).get("email")


def _user_key(ctx) -> str:
    c = caller(ctx) or {}
    return re.sub(r"[^A-Za-z0-9@._-]", "_", c.get("email") or c.get("sub") or "anonymous")


def _draft_prefix(ctx, run_id: str) -> str:
    return f"drafts/{_user_key(ctx)}/{run_id}"


def _ensure_local(run_id: str, ctx) -> Path:
    """Remote mode: requests may land on different instances, so drafts live in the store."""
    from . import remote, store
    d = store.RUNS / run_id
    if (d / "run.json").exists() or not REMOTE_MODE:
        return d
    remote.backend(_remote()).download_prefix(_draft_prefix(ctx, run_id), d)
    return d


def _no_paths(**paths):
    used = [k for k, v in paths.items() if v]
    if REMOTE_MODE and used:
        raise ValueError(f"{', '.join(used)}: the hosted server cannot read files on your computer. Send the content instead "
                         "(the Model Arena proxy converts local paths automatically).")


# ---------------------------------------------------------------- write / submit tools

def _load_trades(trades, trades_path, trades_file_b64, trades_format) -> list[dict]:
    if trades is not None:
        return trades
    import io
    import pandas as pd
    if trades_file_b64:
        raw, fmt = base64.b64decode(trades_file_b64), (trades_format or "parquet").lower().lstrip(".")
        if fmt == "json":
            return json.loads(raw)
        df = pd.read_parquet(io.BytesIO(raw)) if fmt == "parquet" else pd.read_csv(io.BytesIO(raw))
    elif trades_path:
        p = Path(trades_path).expanduser()
        if p.suffix == ".json":
            return json.loads(p.read_text())
        df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
    else:
        raise ValueError("give trades (list), trades_path (.json/.csv/.parquet) or trades_file_b64 + trades_format")
    return df.astype(object).where(df.notna(), None).to_dict("records")


@server.tool(annotations=WRITE_LOCAL)
@friendly
def arena_create_run(run_id: str, track_id: str, split: str, model_name: str, family: str, version: str,
                     author: str | None = None, trades: list[dict] | None = None, trades_path: str | None = None,
                     trades_file_b64: str | None = None, trades_format: str | None = None, params: dict | None = None,
                     notes: str | None = None, command: list[str] | None = None, inputs: list[str] | None = None,
                     inputs_info: list[dict] | None = None, artifacts: list[str] | None = None,
                     artifacts_b64: dict[str, str] | None = None, code: dict | None = None,
                     artifact_hash: str | None = None, curve: list[dict] | None = None, ctx: Context = None) -> dict:
    """Create a standard run (a draft until arena_submit_run) and validate it.
    Trades: a list, or a file (trades_path locally; trades_file_b64 + trades_format when hosted).
    command: the `arena` sub-command that regenerates it; inputs / inputs_info: data files used (path or
    precomputed {path, sha256, bytes}); artifacts / artifacts_b64: model files stored with the run;
    code: {repo, commit, branch, dirty} of the code that produced it. Provenance is recorded."""
    from . import remote, store
    from .provenance import collect
    from .store import sha, write_run
    _no_paths(trades_path=trades_path, inputs=inputs, artifacts=artifacts)
    tr = _load_trades(trades, trades_path, trades_file_b64, trades_format)
    arts: dict = {Path(a).name: Path(a).expanduser() for a in (artifacts or [])}
    arts |= {Path(n).name: base64.b64decode(v) for n, v in (artifacts_b64 or {}).items()}
    author = author or _who(ctx) or "unknown"
    meta = {"run_id": run_id, "track_id": track_id, "split": split, "author": author,
            "model": {"name": model_name, "family": family, "version": version},
            "artifact_hash": artifact_hash or sha({"params": params, "artifacts": sorted(arts), "trades": len(tr)})}
    if params:
        meta["params"] = params
    if notes:
        meta["notes"] = notes
    prov = collect(command or ["(created via MCP; no regenerate command)"], [Path(i).expanduser() for i in (inputs or [])])
    if inputs_info:
        prov["inputs"] = list(prov["inputs"]) + inputs_info
    if code:
        prov["code"] = {**prov["code"], **{k: code[k] for k in ("repo", "commit", "branch", "dirty", "dirty_files") if k in code}}
    if REMOTE_MODE:
        prov["created_via"] = {"server": "bedrock-agentcore", "caller": _who(ctx)}
    path = write_run(meta, tr, curve, runs_dir=store.RUNS, artifacts=arts or None, provenance=prov)
    if REMOTE_MODE:
        remote.backend(_remote()).upload_dir(path, _draft_prefix(ctx, run_id))
    return {"path": str(path) if not REMOTE_MODE else f"draft {_draft_prefix(ctx, run_id)}", **arena_validate_run(run_id, ctx)}


@server.tool(annotations=READ)
@friendly
def arena_validate_run(run_id: str, ctx: Context = None) -> dict:
    """Check a draft/local run against the Arena Standard and preview its platform metrics."""
    from . import store
    from .metrics import score
    from .schema import load_run_dir, load_tracks, validate_run
    d = _ensure_local(run_id, ctx)
    if not (d / "run.json").exists():
        return {"ok": False, "errors": [f"no draft or local run '{run_id}'. Create it with arena_create_run first."]}
    tracks = load_tracks(store.TRACKS)
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
def arena_submit_run(run_id: str, ctx: Context = None) -> dict:
    """Validate and upload a run to the shared store. Immutable: a different result under the same
    run_id is rejected; the same result with more evidence is accepted as 'enriched'.
    When hosted, the submitter is the signed-in Cognito user."""
    from . import remote, store
    _ensure_local(run_id, ctx)
    try:
        return remote.submit(run_id, _remote(), store.RUNS, who=_who(ctx))
    except SystemExit as e:
        return {"status": "rejected", "reason": str(e)}


def _study_folder(folder: str | None, files_b64: dict[str, str] | None, study_name: str | None) -> Path:
    """A study folder from a local path, or rebuilt from uploaded files (hosted mode)."""
    if files_b64:
        import tempfile
        kind, _, version = (study_name or "").strip("/").partition("/")
        if not kind or not version:
            raise ValueError("study_name must be '<kind>/<version>', e.g. 'entry-gate/20261114-v1'")
        root = Path(tempfile.mkdtemp(prefix="arena-study-")) / kind / version
        root.mkdir(parents=True)
        for name, b64 in files_b64.items():
            (root / Path(name).name).write_bytes(base64.b64decode(b64))
        return root
    _no_paths(folder=folder)
    if not folder:
        raise ValueError("give folder (local) or files_b64 + study_name")
    return Path(folder).expanduser()


@server.tool(annotations=READ)
@friendly
def arena_preview_study(folder: str | None = None, files_b64: dict[str, str] | None = None, study_name: str | None = None) -> dict:
    """Interpret an experiment folder (study.json, built-in adapter or inference) and compute its grid
    without uploading. Locally pass folder; when hosted the proxy sends files_b64 + study_name ('<kind>/<version>')."""
    import tempfile
    from .studies import build_study, load_spec
    f = _study_folder(folder, files_b64, study_name)
    spec = load_spec(f)
    with tempfile.TemporaryDirectory() as tmp:
        s = build_study(f, Path(tmp))
    best = sorted((c for c in s["cells"] if c["n"] >= 30), key=lambda c: -c["mean"])[:5]
    return {"study_id": spec["study_id"], "how": "study.json" if (f / "study.json").exists() else ("inferred" if spec.get("inferred") else "adapter"),
            "dimensions": [{"key": d["key"], "label": d["label"], "aggregate": d.get("aggregate"), "values": d["values"]} for d in s["dimensions"]],
            "n_trades": s["n_trades"], "cells": s["trials"], "significant_cells": s["significant_cells"],
            "top_cells_n30": [{"key": c["key"], "n": c["n"], "mean": c["mean"], "q_value": c["q_value"]} for c in best],
            "locked_holdout": s.get("locked_holdout", False)}


@server.tool(annotations=SUBMIT)
@friendly
def arena_submit_study(folder: str | None = None, files_b64: dict[str, str] | None = None, study_name: str | None = None,
                       request_refresh: bool = True, ctx: Context = None) -> dict:
    """Upload an experiment's study inputs to the shared store (a local folder is never modified).
    The dashboard shows it after the next refresh."""
    from . import remote
    from .studies import add_local
    dest = add_local(_study_folder(folder, files_b64, study_name))
    who = _who(ctx)
    if who:
        spec = json.loads((dest / "study.json").read_text())
        spec["submitted_by"] = who
        (dest / "study.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    remote.backend(_remote()).upload_dir(dest, f"studies/{dest.name}")
    out = {"status": "submitted", "study_id": dest.name, "submitted_by": who}
    if request_refresh:
        out["refresh"] = arena_request_refresh()
    return out


@server.tool(annotations=SUBMIT)
@friendly
def arena_request_refresh() -> str:
    """Ask for a dashboard rebuild now (local: needs the `gh` CLI). The dashboard also refreshes every 10 minutes."""
    if REMOTE_MODE or not shutil.which("gh"):
        return "the dashboard refreshes automatically within about 10 minutes"
    r = subprocess.run(["gh", "workflow", "run", "refresh.yml", "--repo", REPO], capture_output=True, text=True)
    return "refresh requested (about 1-2 minutes)" if r.returncode == 0 else f"could not request refresh ({r.stderr.strip()[:160]}); it refreshes within about 10 minutes"


@server.tool(annotations=SUBMIT)
@friendly
def arena_reproduce_run(run_id: str, retrain: bool = False, record: bool = True) -> dict:
    """Check that a run can be regenerated: input data hashes, replay of the stored model, and (retrain=true)
    re-running the recorded command at the recorded commit. Downloads the run from the store if missing.
    record=true uploads the report to the shared store. Needs the research data and the git checkout,
    so it runs only with the local (stdio) server."""
    if REMOTE_MODE:
        return {"verdict": "unavailable", "reason": "reproduction needs the research data and the git checkout; run the local "
                "server (uv run python -m arena.mcp_server) or `uv run python -m arena reproduce <run_id>` on a machine with the data"}
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


@server.tool(annotations=READ)
@friendly
def arena_whoami(ctx: Context = None) -> dict:
    """Who the server thinks you are (hosted: the signed-in Cognito user) and which store it uses."""
    return {"mode": "hosted (Bedrock AgentCore)" if REMOTE_MODE else "local", "caller": caller(ctx), "store": data().where}


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
    import sys
    if REMOTE_MODE or "--http" in sys.argv:
        from mcp.server.transport_security import TransportSecuritySettings
        # AgentCore MCP contract: stateless streamable HTTP on 0.0.0.0:8000/mcp; the Host header is the AWS endpoint
        server.run("streamable-http", host="0.0.0.0", port=int(os.environ.get("ARENA_PORT", "8000")), streamable_http_path="/mcp", stateless_http=True,
                   json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
