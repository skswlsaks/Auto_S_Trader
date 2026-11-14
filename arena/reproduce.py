"""`arena reproduce <run_id>`: prove a stored run can be regenerated.

Checks, in order:
  inputs   recorded input files hash the same here
  artifact (RL softmax policies) the stored model file alone re-generates the same trades
  retrain  the recorded commit, checked out into a temporary git worktree, re-runs the recorded
           command against the same data and yields identical trades, artifact_hash and artifacts
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .provenance import REPO, check_inputs
from .store import OUTPUTS, RUNS, now


def _load(run_dir: Path):
    return json.loads((run_dir / "run.json").read_text()), json.loads((run_dir / "trades.json").read_text())


def _norm(trades):
    return json.dumps(trades, sort_keys=True)


def _artifact_check(run_dir: Path, run: dict, trades: list) -> dict:
    names = {a["name"] for a in run.get("artifacts", [])}
    if "model.json" not in names:
        return {"name": "artifact", "status": "skip", "detail": "재생 가능한 정책 모델(model.json) 없음 — retrain으로 확인"}
    from .rl_train import replay_artifact
    replayed = replay_artifact(run_dir)
    same = _norm(replayed) == _norm(trades)
    return {"name": "artifact", "status": "pass" if same else "fail",
            "detail": f"저장된 모델로 재생: {len(replayed)}건 / 기록 {len(trades)}건" + ("" if same else " — 불일치")}


def _retrain_check(run: dict, trades: list, keep: bool = False) -> dict:
    prov = run["provenance"]
    commit = prov["code"].get("commit")
    if not commit:
        return {"name": "retrain", "status": "skip", "detail": "커밋 기록 없음"}
    if subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode:
        subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "origin"], capture_output=True)
    tmp = Path(tempfile.mkdtemp(prefix="arena-repro-"))
    wt, out = tmp / "code", tmp / "outputs"
    try:
        r = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-q", "--detach", str(wt), commit], capture_output=True, text=True)
        if r.returncode:
            return {"name": "retrain", "status": "fail", "detail": f"커밋 {commit[:8]} checkout 실패: {r.stderr.strip()[:200]}"}
        out.mkdir()
        (out / "model-research").symlink_to(OUTPUTS / "model-research")  # same data, isolated run output
        env = {**os.environ, "ARENA_OUTPUTS": str(out)}
        cmd = ["uv", "run", "--directory", str(wt), "--locked", "--quiet", "python", "-m", "arena", *prov["command"]]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)
        if r.returncode:
            return {"name": "retrain", "status": "fail", "detail": f"명령 실패: {' '.join(prov['command'])}: {(r.stderr or r.stdout).strip()[-300:]}"}
        new_dir = out / "arena" / "runs" / run["run_id"]
        if not new_dir.exists():
            return {"name": "retrain", "status": "fail", "detail": "명령이 같은 run_id를 만들지 않음"}
        new_run, new_trades = _load(new_dir)
        diffs = []
        if _norm(new_trades) != _norm(trades):
            diffs.append(f"거래 {len(new_trades)}건 vs {len(trades)}건 불일치")
        if new_run.get("artifact_hash") != run.get("artifact_hash"):
            diffs.append("artifact_hash 불일치")
        old_a = {a["name"]: a["sha256"] for a in run.get("artifacts", [])}
        new_a = {a["name"]: a["sha256"] for a in new_run.get("artifacts", [])}
        if old_a != new_a:
            diffs.append("모델 파일 해시 불일치")
        return {"name": "retrain", "status": "fail" if diffs else "pass",
                "detail": "; ".join(diffs) or f"커밋 {commit[:8]}에서 `{' '.join(prov['command'])}` 재실행 → 거래·모델 해시 일치"}
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], capture_output=True)
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def reproduce(run_id: str, runs_dir: Path = RUNS, retrain: bool = True) -> dict:
    run_dir = runs_dir / run_id
    if not (run_dir / "run.json").exists():
        raise SystemExit(f"no local run {run_id} (arena pull first)")
    run, trades = _load(run_dir)
    prov = run.get("provenance")
    checks = []
    if not prov:
        checks.append({"name": "provenance", "status": "fail", "detail": "재현 정보가 없는 이전 형식 run"})
    else:
        if prov["code"].get("dirty"):
            checks.append({"name": "clean_commit", "status": "warn",
                           "detail": "생성 당시 커밋되지 않은 변경 포함: " + ", ".join(prov["code"].get("dirty_files", [])[:5])})
        inp = check_inputs(prov.get("inputs", []))
        bad = [i for i in inp if i["status"] != "ok"]
        checks.append({"name": "inputs", "status": "fail" if bad else "pass",
                       "detail": f"입력 {len(inp)}개 해시 일치" if not bad else "; ".join(f"{i['path']}: {i['status']}" for i in bad)})
        checks.append(_artifact_check(run_dir, run, trades))
        if retrain and not bad:
            checks.append(_retrain_check(run, trades))
        elif retrain:
            checks.append({"name": "retrain", "status": "skip", "detail": "입력 데이터가 달라 재학습 비교 생략"})
    statuses = {c["status"] for c in checks}
    verdict = "not_reproduced" if "fail" in statuses else ("reproduced" if "pass" in statuses and not statuses & {"warn"} else "partial")
    report = {"run_id": run_id, "checked_at": now(), "checked_by": getpass.getuser(),
              "commit": (prov or {}).get("code", {}).get("commit"), "verdict": verdict, "checks": checks}
    rdir = run_dir / "reproductions"
    rdir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (rdir / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def latest_report(run_dir: Path):
    rdir = run_dir / "reproductions"
    files = sorted(rdir.glob("*.json")) if rdir.exists() else []
    return json.loads(files[-1].read_text()) if files else None
