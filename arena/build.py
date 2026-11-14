from __future__ import annotations

import json
import shutil

from .metrics import score
from .schema import load_run_dir, load_tracks, validate_run
from .store import PKG, RUNS, SITE, TRACKS, now


def validate_all(runs_dir=RUNS) -> dict[str, list[str]]:
    tracks = load_tracks(TRACKS)
    report = {}
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        try:
            run, trades, curve = load_run_dir(d)
            report[d.name] = validate_run(run, trades, tracks, curve)
        except Exception as e:  # noqa: BLE001 - surface any broken submission
            report[d.name] = [f"unreadable: {e}"]
    return report


def build(runs_dir=RUNS, site_dir=SITE) -> dict:
    tracks = load_tracks(TRACKS)
    runs, rejected = [], {}
    for name, errors in validate_all(runs_dir).items():
        if errors:
            rejected[name] = errors
            continue
        run, trades, curve = load_run_dir(runs_dir / name)
        m = score(trades, tracks[run["track_id"]], run["split"])
        receipt = runs_dir / name / "submission.json"
        sub = json.loads(receipt.read_text()) if receipt.exists() else None
        from .reproduce import latest_report
        runs.append({"run": run, "metrics": m, "train_curve": curve, "submission": sub,
                     "reproduction": latest_report(runs_dir / name)})
    import os
    data = {"generated_at": now(), "tracks": tracks, "runs": runs, "rejected": rejected,
            "store": os.environ.get("ARENA_REMOTE")}
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False))
    for f in (PKG / "site").iterdir():
        if f.is_file():
            shutil.copy(f, site_dir / f.name)
    return data
