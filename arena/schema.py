"""Arena Standard v0 validation for run.json / trades.json / curve.json."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from . import SCHEMA_VERSION
from .metrics import _day

FAMILIES = {"rule", "supervised", "rl", "baseline"}
RUN_REQUIRED = ("schema_version", "run_id", "track_id", "split", "model", "author", "created_at", "artifact_hash")
MODEL_REQUIRED = ("name", "family", "version")
TRADE_REQUIRED = ("symbol", "entry_at", "exit_at", "net_return", "complete")


def _iso(value) -> bool:
    try:
        datetime.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def validate_run(run: dict, trades: list, tracks: dict, curve=None) -> list[str]:
    errors = [f"run.json: missing '{k}'" for k in RUN_REQUIRED if k not in run]
    if errors:
        return errors
    if run["schema_version"] != SCHEMA_VERSION:
        errors.append(f"run.json: schema_version must be {SCHEMA_VERSION}")
    errors += [f"run.json: model missing '{k}'" for k in MODEL_REQUIRED if k not in run["model"]]
    if run["model"].get("family") not in FAMILIES:
        errors.append(f"run.json: model.family must be one of {sorted(FAMILIES)}")
    track = tracks.get(run["track_id"])
    if track is None:
        errors.append(f"run.json: unknown track_id '{run['track_id']}'")
    elif run["split"] not in track.get("windows", {}):
        errors.append(f"run.json: split must be one of {sorted(track['windows'])}")
    if not _iso(run["created_at"]):
        errors.append("run.json: created_at is not ISO-8601")
    if not isinstance(trades, list) or not trades:
        return errors + ["trades.json: must be a non-empty list"]
    window = (track or {}).get("windows", {}).get(run["split"], {})
    for i, t in enumerate(trades):
        missing = [k for k in TRADE_REQUIRED if k not in t]
        if missing:
            errors.append(f"trades[{i}]: missing {missing}")
            continue
        if not _iso(t["entry_at"]):
            errors.append(f"trades[{i}]: entry_at not ISO-8601")
        if t["complete"]:
            if t["exit_at"] is None or not _iso(t["exit_at"]):
                errors.append(f"trades[{i}]: complete trade needs ISO exit_at")
            elif t["exit_at"] < t["entry_at"]:
                errors.append(f"trades[{i}]: exit_at before entry_at")
        r = t["net_return"]
        if r is not None and (not isinstance(r, (int, float)) or math.isnan(r) or abs(r) > 1):
            errors.append(f"trades[{i}]: net_return must be a decimal in [-1, 1] (0.01 = 1%)")
        if t["complete"] and r is None:
            errors.append(f"trades[{i}]: complete trade needs net_return")
        day = _day(t["entry_at"]) if _iso(t["entry_at"]) else ""
        if window.get("start") and day < window["start"] or window.get("end") and day > window["end"]:
            errors.append(f"trades[{i}]: entry {day} outside {run['split']} window")
        if len(errors) > 20:
            return errors + ["... (truncated)"]
    if curve is not None and not (isinstance(curve, list) and all("step" in c and "value" in c for c in curve)):
        errors.append("curve.json: must be a list of {step, value[, metric]}")
    return errors


def load_tracks(path: Path) -> dict:
    return {p.stem: json.loads(p.read_text()) for p in sorted(path.glob("*.json"))}


def load_run_dir(path: Path):
    run = json.loads((path / "run.json").read_text())
    trades = json.loads((path / "trades.json").read_text())
    curve = json.loads((path / "curve.json").read_text()) if (path / "curve.json").exists() else None
    return run, trades, curve
