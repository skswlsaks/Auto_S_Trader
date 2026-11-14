"""Reproducibility record attached to every run: code version, environment, command, input data.

A run is reproducible when (a) the recorded commit is clean and pushed, (b) the recorded input
files hash the same on the machine reproducing it, and (c) re-running `command` at that commit
yields the same trades and model artifact. `arena reproduce` checks exactly this.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .store import OUTPUTS, PKG

REPO = PKG.parent
PACKAGES = ("numpy", "pandas", "pyarrow", "scikit-learn", "lightgbm")


def _git(*args) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10)
        return r.stdout.rstrip("\n") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=None)
def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_info() -> dict:
    status = _git("status", "--porcelain")
    dirty = [line[3:] for line in (status or "").splitlines() if line.strip()]
    remote = (_git("config", "--get", "remote.origin.url") or "").strip()
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.split(":", 1)[1]
    return {
        "repo": remote.removesuffix(".git") or None,
        "commit": os.environ.get("GITHUB_SHA") or (_git("rev-parse", "HEAD") or "").strip() or None,
        "branch": (_git("rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None,
        "dirty": bool(dirty),
        "dirty_files": dirty[:20],
    }


def env_info() -> dict:
    pkgs = {}
    for p in PACKAGES:
        try:
            pkgs[p] = md.version(p)
        except md.PackageNotFoundError:
            pass
    lock = REPO / "uv.lock"
    return {
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
        "packages": pkgs,
        "uv_lock_sha256": file_sha256(str(lock)) if lock.exists() else None,
    }


def input_info(paths) -> list[dict]:
    out = []
    for p in paths:
        p = Path(p).absolute()  # keep symlinked data roots relative (portable across machines)
        rel = os.path.relpath(p, OUTPUTS.absolute())
        out.append({"path": rel, "sha256": file_sha256(str(p)), "bytes": p.stat().st_size})
    return out


def collect(command: list[str], inputs=()) -> dict:
    info = {"code": code_info(), "env": env_info(), "command": list(command), "inputs": input_info(inputs)}
    if os.environ.get("GITHUB_RUN_ID"):
        info["ci"] = {"github_run_id": os.environ["GITHUB_RUN_ID"]}
    return info


def check_inputs(inputs: list[dict], outputs: Path = OUTPUTS) -> list[dict]:
    """Compare recorded input hashes with the files available here."""
    res = []
    for i in inputs:
        p = outputs / i["path"]
        if not p.exists():
            res.append({**i, "status": "missing"})
        else:
            res.append({**i, "status": "ok" if file_sha256(str(p)) == i["sha256"] else "changed"})
    return res


def python_cmd() -> list[str]:
    return [sys.executable, "-m", "arena"]
