"""`arena study watch`: submit finished experiments automatically.

Scans outputs/model-research/<kind>/<version>/ (read only). A folder is *finished* when its
trades.parquet has not changed for `min_age` seconds and a result marker exists (REPORT.md,
summary.csv/json or results.json). New or changed finished studies are copied into the Arena
cache, uploaded to <remote>/studies/, and a dashboard refresh is requested (GitHub Actions).
State (content hashes already submitted) lives in outputs/arena/watch-state.json.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import OUT, OUTPUTS, now

ROOT = OUTPUTS / "model-research"
STATE = OUT / "watch-state.json"
MARKERS = ("REPORT.md", "summary.csv", "summary.json", "results.json")
REPO = "skswlsaks/Auto_S_Trader"


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for name in ("trades.parquet", "protocol.json", "study.json", "holdout-lock.json"):
        p = folder / name
        if p.exists():
            h.update(name.encode() + b"\0")
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def candidates(root: Path | None = None, since: str | None = None, min_age: int = 600):
    """Yield (folder, status) for experiment folders. status: ready | running | old"""
    root = root or ROOT
    cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() if since else 0
    for trades in sorted(root.glob("*/*/trades.parquet")):
        folder = trades.parent
        if folder.name.endswith("-incomplete"):
            continue
        mtime = trades.stat().st_mtime
        if mtime < cutoff:
            yield folder, "old"
        elif time.time() - mtime < min_age or not any((folder / m).exists() for m in MARKERS):
            yield folder, "running"
        else:
            yield folder, "ready"


def _load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def watch_once(remote: str | None, since: str | None = "2026-11-14", min_age: int = 600,
               dry_run: bool = False, refresh: bool = True, log=print) -> list[str]:
    from . import studies
    state, submitted = _load_state(), []
    for folder, status in candidates(since=since, min_age=min_age):
        if status != "ready":
            continue
        try:
            spec = studies.load_spec(folder)
        except SystemExit as e:
            log(f"skip {folder.name}: {e}")
            continue
        sid, digest = spec["study_id"], _digest(folder)
        if state.get(sid, {}).get("digest") == digest:
            continue
        log(f"{'would submit' if dry_run else 'submit'} {sid} ({'study.json' if (folder / 'study.json').exists() else 'adapter' if not spec.get('inferred') else 'inferred'})")
        if dry_run:
            submitted.append(sid)
            continue
        dest = studies.add_local(folder)
        if remote:
            from . import remote as rm
            rm.backend(remote).upload_dir(dest, f"studies/{dest.name}")
        state[sid] = {"digest": digest, "submitted_at": now(), "source": f"{folder.parent.name}/{folder.name}"}
        submitted.append(sid)
    if submitted and not dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        if refresh and remote and shutil.which("gh"):
            r = subprocess.run(["gh", "workflow", "run", "refresh.yml", "--repo", REPO], capture_output=True, text=True)
            log("requested dashboard refresh" if r.returncode == 0 else f"refresh request failed: {r.stderr.strip()[:200]}")
    return submitted


def status_table(since: str | None = "2026-11-14", min_age: int = 600) -> list[tuple[str, str, str]]:
    from . import studies
    state, rows = _load_state(), []
    for folder, status in candidates(since=since, min_age=min_age):
        try:
            sid = studies.load_spec(folder)["study_id"]
        except SystemExit:
            rows.append((f"{folder.parent.name}/{folder.name}", "not_study", "-"))
            continue
        done = state.get(sid, {})
        label = status if status != "ready" else ("submitted" if done.get("digest") == _digest(folder) else "pending")
        rows.append((f"{folder.parent.name}/{folder.name}", label, done.get("submitted_at", "-")))
    return rows
