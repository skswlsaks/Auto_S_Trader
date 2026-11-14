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


def write_run(run: dict, trades: list, curve: list | None = None, runs_dir: Path = RUNS,
              artifacts: dict | None = None, provenance: dict | None = None) -> Path:
    """Write one standard run. `artifacts` maps file name -> bytes or Path (model files, configs);
    each is stored under artifacts/ and listed with its sha256 in run.json."""
    run = {"schema_version": SCHEMA_VERSION, "created_at": now(), **run}
    d = runs_dir / run["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    if artifacts:
        adir = d / "artifacts"
        adir.mkdir(exist_ok=True)
        manifest = []
        for name, src in sorted(artifacts.items()):
            data = Path(src).read_bytes() if isinstance(src, (str, Path)) else src
            (adir / name).write_bytes(data)
            manifest.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        run["artifacts"] = manifest
    if provenance is not None:
        run["provenance"] = provenance
    (d / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
    (d / "trades.json").write_text(json.dumps(trades, ensure_ascii=False))
    if curve is not None:
        (d / "curve.json").write_text(json.dumps(curve))
    return d


def result_sha256(run_dir: Path) -> str:
    """Identity of a run's *result*: trades, curve and the model-defining fields of run.json.
    Excludes evidence/metadata (created_at, notes, author, provenance, artifacts manifest) so a run
    can later be enriched with provenance without changing what was measured."""
    run = json.loads((run_dir / "run.json").read_text())
    core = {k: run.get(k) for k in ("run_id", "track_id", "split", "model", "params", "artifact_hash")}
    h = hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode())
    for name in ("trades.json", "curve.json"):
        p = run_dir / name
        h.update(name.encode() + b"\0" + (json.dumps(json.loads(p.read_text()), sort_keys=True).encode() if p.exists() else b""))
    return h.hexdigest()
