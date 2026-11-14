from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .metrics import score
from .metrics_ext import bh_qvalues, deflated_sharpe, random_percentile
from .schema import load_run_dir, load_tracks, validate_run
from .store import PKG, RUNS, SITE, TRACKS, now

WEB_OUT = PKG.parent / "web" / "out"  # Next.js static export, when built
SUMMARY_DROP = ("curve", "return_hist", "exit_reasons", "monthly_pp")


def validate_all(runs_dir=RUNS) -> dict[str, list[str]]:
    tracks = load_tracks(TRACKS)
    report = {}
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.exists() else []:
        try:
            run, trades, curve = load_run_dir(d)
            report[d.name] = validate_run(run, trades, tracks, curve)
        except Exception as e:  # noqa: BLE001 - surface any broken submission
            report[d.name] = [f"unreadable: {e}"]
    return report


def _spark(curve, points=80):
    if len(curve) <= points:
        return [c[1] for c in curve]
    step = (len(curve) - 1) / (points - 1)
    return [curve[round(i * step)][1] for i in range(points)]


def _significance(runs: list[dict], trades_by_id: dict, tracks: dict):
    """Per (track, split): BH q-values over model runs, deflated Sharpe, random-selection percentile."""
    groups: dict[tuple, list] = {}
    for r in runs:
        groups.setdefault((r["run"]["track_id"], r["run"]["split"]), []).append(r)
    for (track_id, split), rs in groups.items():
        models = [r for r in rs if r["run"]["model"]["family"] != "baseline"]
        q = bh_qvalues({r["run"]["run_id"]: r["metrics"].get("p_value_mean_gt0") for r in models})
        srs = [r["metrics"].get("sr_daily") for r in models]
        bp = tracks[track_id].get("baseline_pool") or {}

        def pool_for(run):
            if "template" not in bp:
                return bp.get(split)
            mdl = run["model"]
            short = {"system_only": "sys", "system_bearish_transfer_model": "bear"}.get(mdl.get("exit_method"))
            vals = {"exit_policy": mdl.get("exit_policy") or bp["default"]["exit_policy"],
                    "exit_short": short or bp["default"]["exit_short"], "suffix": bp.get("suffix", {}).get(split, "")}
            return bp["template"].format(**vals)

        for r in rs:
            m = r["metrics"]
            pool_id = pool_for(r["run"])
            pool = [t["net_return"] for t in trades_by_id.get(pool_id, []) if t.get("complete") and t.get("net_return") is not None]
            m["q_value"] = q.get(r["run"]["run_id"])
            m["trials"] = len(models)
            m["deflated_sharpe"] = deflated_sharpe(m.get("sr_daily"), m.get("t_days"), m.get("skew_daily"), m.get("kurt_daily"), srs)
            is_pool = pool_id == r["run"]["run_id"]  # the take-all reference itself
            m["random_percentile"] = random_percentile(m.get("mean_return"), pool, m.get("closed", 0)) if pool and not is_pool else None
            m["random_pool"] = pool_id if pool else None


def build(runs_dir=RUNS, site_dir=SITE) -> dict:
    tracks = load_tracks(TRACKS)
    runs, rejected, trades_by_id = [], {}, {}
    from .reproduce import latest_report
    for name, errors in validate_all(runs_dir).items():
        if errors:
            rejected[name] = errors
            continue
        run, trades, curve = load_run_dir(runs_dir / name)
        trades_by_id[run["run_id"]] = trades
        m = score(trades, tracks[run["track_id"]], run["split"])
        receipt = runs_dir / name / "submission.json"
        sub = json.loads(receipt.read_text()) if receipt.exists() else None
        runs.append({"run": run, "metrics": m, "train_curve": curve, "submission": sub,
                     "reproduction": latest_report(runs_dir / name)})
    _significance(runs, trades_by_id, tracks)
    generated = now()
    store = os.environ.get("ARENA_REMOTE")
    site_dir.mkdir(parents=True, exist_ok=True)
    data_dir = site_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    (data_dir / "runs").mkdir(parents=True)

    # runs: light index for lists/leaderboards, one detail file per run
    index_runs = []
    for r in runs:
        m = r["metrics"]
        index_runs.append({"run": {k: v for k, v in r["run"].items() if k not in ("provenance",)},
                           "metrics": {k: v for k, v in m.items() if k not in SUMMARY_DROP} | {"spark": _spark(m["curve"])},
                           "reproduction": {"verdict": r["reproduction"]["verdict"], "checked_at": r["reproduction"]["checked_at"]} if r["reproduction"] else None,
                           "has_provenance": bool(r["run"].get("provenance")),
                           "submission": r["submission"]})
        (data_dir / "runs" / f"{r['run']['run_id']}.json").write_text(json.dumps(r, ensure_ascii=False))

    # studies (condition grids) and lab sources (flexible combinations)
    from . import lab, studies
    lab.update_conditions()
    study_index = studies.build_all(data_dir / "studies")
    sources = []
    for s in study_index:
        folder = studies.STUDIES / s["study_id"]
        sources.append(lab.study_source(folder, studies.load_spec(folder)))
    for track_id in tracks:
        for split in tracks[track_id]["windows"]:
            members = [(r["run"], trades_by_id[r["run"]["run_id"]]) for r in runs
                       if r["run"]["track_id"] == track_id and r["run"]["split"] == split]
            src = lab.runs_source(track_id, split, members) if members else None
            if src:
                sources.append(src)
    lab_index = lab.write_sources(data_dir / "lab", sources)

    index = {"generated_at": generated, "store": store, "tracks": tracks, "runs": index_runs, "rejected": rejected,
             "studies": study_index, "lab": lab_index}
    (data_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False))

    # app shell: the Next.js static export (web/out), when built. Data-only refreshes skip it.
    if WEB_OUT.exists():
        for item in WEB_OUT.iterdir():
            if item.name == "data":  # dev symlink copied by `next build`; the fresh data is already in place
                continue
            dest = site_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy(item, dest)
    return index
