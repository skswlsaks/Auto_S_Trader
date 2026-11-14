from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION

PKG = Path(__file__).resolve().parent
TRACKS = PKG / "tracks"


def _outputs() -> Path:
    """Local data root (never in Git). ARENA_OUTPUTS wins; else the first existing outputs/ next to
    or above the code (repo-root layout or the s_trader workspace layout)."""
    if os.environ.get("ARENA_OUTPUTS"):
        return Path(os.environ["ARENA_OUTPUTS"]).expanduser().resolve()
    for base in (PKG.parent, PKG.parents[1]):
        if (base / "outputs").is_dir():
            return base / "outputs"
    return PKG.parent / "outputs"


OUTPUTS = _outputs()
OUT = OUTPUTS / "arena"
RUNS = OUT / "runs"
SITE = OUT / "site"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def write_run(run: dict, trades: list, curve: list | None = None, runs_dir: Path = RUNS) -> Path:
    run = {"schema_version": SCHEMA_VERSION, "created_at": now(), **run}
    d = runs_dir / run["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
    (d / "trades.json").write_text(json.dumps(trades, ensure_ascii=False))
    if curve is not None:
        (d / "curve.json").write_text(json.dumps(curve))
    return d
