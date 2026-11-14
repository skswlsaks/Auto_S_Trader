"""Shared run store for the team.

Remote = a shared folder (NAS, Google Drive/Dropbox mount, network share) or s3://bucket/prefix
(uses the `aws` CLI with the caller's own credentials). Layout:

    <remote>/runs/<run_id>/{run.json, trades.json, curve.json?, submission.json}
    <remote>/site/{index.html, data.json}          # published dashboard

Rules enforced on submit: schema must pass; a run_id is immutable (re-submit only if identical);
holdout submissions per (track, model.name) are capped by the track.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .schema import load_run_dir, load_tracks, validate_run
from .store import RUNS, SITE, TRACKS, now, result_sha256

FILES = ("run.json", "trades.json", "curve.json")
DIRS = ("artifacts", "reproductions")


def remote_from_env(explicit: str | None = None) -> str:
    r = explicit or os.environ.get("ARENA_REMOTE")
    if not r:
        raise SystemExit("set ARENA_REMOTE (shared folder path or s3://bucket/prefix) or pass --remote")
    return r.rstrip("/")


def content_hash(run_dir: Path) -> str:
    h = hashlib.sha256()
    for f in FILES:
        p = run_dir / f
        if p.exists():
            h.update(f.encode() + b"\0" + p.read_bytes())
    if (run_dir / "artifacts").is_dir():
        for p in sorted((run_dir / "artifacts").iterdir()):
            h.update(b"artifacts/" + p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def submitter() -> str:
    try:
        name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or getpass.getuser()


class Backend:
    def list_runs(self) -> list[str]: ...
    def read_json(self, run_id: str, name: str): ...
    def upload_dir(self, src: Path, dest: str): ...
    def put_file(self, src: Path, dest: str): ...
    def download_runs(self, dest: Path): ...


class FolderBackend(Backend):
    def __init__(self, root: str):
        self.root = Path(root).expanduser()

    def list_runs(self):
        d = self.root / "runs"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def read_json(self, run_id, name):
        p = self.root / "runs" / run_id / name
        return json.loads(p.read_text()) if p.exists() else None

    def upload_dir(self, src, dest):
        target = self.root / dest
        tmp = target.with_name(target.name + ".uploading")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        if target.exists():
            shutil.rmtree(target)
        tmp.rename(target)  # atomic-ish publish: readers never see half a run

    def put_file(self, src, dest):
        target = self.root / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)

    def download_prefix(self, prefix, dest):
        src = self.root / prefix
        if src.exists():
            shutil.copytree(src, dest, dirs_exist_ok=True)

    def download_runs(self, dest):
        for rid in self.list_runs():
            shutil.copytree(self.root / "runs" / rid, dest / rid, dirs_exist_ok=True)


class S3Backend(Backend):
    def __init__(self, url: str):
        self.url = url

    def _aws(self, *args, capture=True):
        r = subprocess.run(["aws", "s3", *args], capture_output=capture, text=True)
        if r.returncode:
            raise SystemExit(f"aws s3 {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout

    def list_runs(self):
        out = subprocess.run(["aws", "s3", "ls", f"{self.url}/runs/"], capture_output=True, text=True)
        return sorted(l.split("PRE ", 1)[1].rstrip("/") for l in out.stdout.splitlines() if "PRE " in l)

    def read_json(self, run_id, name):
        r = subprocess.run(["aws", "s3", "cp", f"{self.url}/runs/{run_id}/{name}", "-"], capture_output=True, text=True)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None

    def upload_dir(self, src, dest):
        self._aws("cp", "--recursive", "--only-show-errors", str(src), f"{self.url}/{dest}")

    def put_file(self, src, dest):
        self._aws("cp", "--only-show-errors", str(src), f"{self.url}/{dest}")

    def download_prefix(self, prefix, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        self._aws("sync", "--only-show-errors", f"{self.url}/{prefix}", str(dest))

    def download_runs(self, dest):
        self._aws("sync", "--only-show-errors", f"{self.url}/runs", str(dest))


class Boto3Backend(Backend):
    """S3 via boto3 (used where the aws CLI is absent, e.g. the AgentCore container)."""

    def __init__(self, url: str):
        import boto3
        self.bucket, _, self.prefix = url[len("s3://"):].partition("/")
        self.prefix = self.prefix.strip("/")
        self.s3 = boto3.client("s3")

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def get_bytes(self, rel: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(rel))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put_bytes(self, rel: str, data: bytes):
        self.s3.put_object(Bucket=self.bucket, Key=self._key(rel), Body=data)

    def list_runs(self):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._key("runs/"), "Delimiter": "/"} | ({"ContinuationToken": token} if token else {})
            r = self.s3.list_objects_v2(**kw)
            out += [p["Prefix"].rstrip("/").rsplit("/", 1)[-1] for p in r.get("CommonPrefixes", [])]
            token = r.get("NextContinuationToken")
            if not token:
                return sorted(out)

    def read_json(self, run_id, name):
        b = self.get_bytes(f"runs/{run_id}/{name}")
        return json.loads(b) if b else None

    def upload_dir(self, src, dest):
        src = Path(src)
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            self.put_bytes(f"{dest}/{f.relative_to(src).as_posix()}", f.read_bytes())

    def put_file(self, src, dest):
        self.put_bytes(dest, Path(src).read_bytes())

    def _download(self, rel_prefix, dest):
        dest = Path(dest)
        pager = self.s3.get_paginator("list_objects_v2")
        base = self._key(rel_prefix).rstrip("/") + "/"
        for page in pager.paginate(Bucket=self.bucket, Prefix=base):
            for o in page.get("Contents", []):
                target = dest / o["Key"][len(base):]
                target.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, o["Key"], str(target))

    def download_prefix(self, prefix, dest):
        self._download(prefix, dest)

    def download_runs(self, dest):
        self._download("runs", dest)


def backend(remote: str) -> Backend:
    if not remote.startswith("s3://"):
        return FolderBackend(remote)
    import shutil as _sh
    if os.environ.get("ARENA_S3_CLIENT") == "boto3" or not _sh.which("aws"):
        return Boto3Backend(remote)
    return S3Backend(remote)


def submit(run_id: str, remote: str, runs_dir: Path = RUNS, who: str | None = None) -> dict:
    src = runs_dir / run_id
    if not src.is_dir():
        raise SystemExit(f"no local run {src}")
    tracks = load_tracks(TRACKS)
    run, trades, curve = load_run_dir(src)
    errors = validate_run(run, trades, tracks, curve)
    if errors:
        raise SystemExit("validation failed:\n  - " + "\n  - ".join(errors))
    if run["run_id"] != run_id:
        raise SystemExit(f"run.json run_id '{run['run_id']}' != folder '{run_id}'")
    b, digest, result = backend(remote), content_hash(src), result_sha256(src)
    existing = b.read_json(run_id, "submission.json")
    if existing:
        same_result = existing.get("result_sha256") == result if existing.get("result_sha256") else _remote_result_matches(b, run_id, src)
        if not same_result:
            raise SystemExit(f"run_id '{run_id}' already submitted by {existing['submitted_by']} with a different result; use a new run_id")
        if existing["content_sha256"] == digest:
            return {"status": "unchanged", **existing}
        # same measured result, more evidence (provenance / model artifacts): allowed, original submitter kept
        receipt = {**existing, "content_sha256": digest, "result_sha256": result,
                   "enriched_by": who or submitter(), "enriched_at": now()}
        _upload(b, src, run_id, receipt)
        return {"status": "enriched", **receipt}
    if run["split"] == "holdout":
        cap = tracks[run["track_id"]].get("holdout_submissions_per_model", 1)
        used = 0
        for rid in b.list_runs():
            other = b.read_json(rid, "run.json")
            if other and other["split"] == "holdout" and other["track_id"] == run["track_id"] and other["model"]["name"] == run["model"]["name"]:
                used += 1
        if used >= cap:
            raise SystemExit(f"holdout cap reached for model '{run['model']['name']}' on {run['track_id']} ({used}/{cap})")
    receipt = {"run_id": run_id, "submitted_by": who or submitter(), "submitted_at": now(),
               "content_sha256": digest, "result_sha256": result}
    _upload(b, src, run_id, receipt)
    return {"status": "submitted", **receipt}


def _remote_result_matches(b: Backend, run_id: str, src: Path) -> bool:
    """Legacy receipts (no result_sha256): compare the measured result directly."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for f in FILES:
            obj = b.read_json(run_id, f)
            if obj is not None:
                (d / f).write_text(json.dumps(obj, ensure_ascii=False))
        return (d / "run.json").exists() and result_sha256(d) == result_sha256(src)


def _upload(b: Backend, src: Path, run_id: str, receipt: dict):
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / run_id
        stage.mkdir()
        for f in FILES:
            if (src / f).exists():
                shutil.copy(src / f, stage / f)
        for d in DIRS:
            if (src / d).is_dir():
                shutil.copytree(src / d, stage / d)
        (stage / "submission.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        b.upload_dir(stage, f"runs/{run_id}")


def record_reproduction(run_id: str, report_path: Path, remote: str):
    backend(remote).put_file(report_path, f"runs/{run_id}/reproductions/{report_path.name}")


def pull(remote: str, runs_dir: Path = RUNS) -> int:
    runs_dir.mkdir(parents=True, exist_ok=True)
    b = backend(remote)
    b.download_runs(runs_dir)
    b.download_prefix("studies", runs_dir.parent / "studies")
    b.download_prefix("conditions", runs_dir.parent / "conditions")
    return len(b.list_runs())


def publish_site(remote: str, site_dir: Path = SITE, data_only: bool = False):
    b = backend(remote)
    if data_only:
        b.upload_dir(site_dir / "data", "site/data")
    else:
        b.upload_dir(site_dir, "site")


def push_conditions(remote: str, cond_dir: Path):
    if cond_dir.exists():
        backend(remote).upload_dir(cond_dir, "conditions")
