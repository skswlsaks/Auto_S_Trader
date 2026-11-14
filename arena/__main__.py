from __future__ import annotations

import argparse
import functools
import http.server
import json

from .store import SITE


def main():
    p = argparse.ArgumentParser(prog="arena", description="Model Arena prototype")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-selective", help="import selective-daytrade 20261101-v2 as standard runs")
    sub.add_parser("baseline-random", help="random-filter baselines on the same candidate pool")
    sub.add_parser("example-rl", help="demo RL (contextual bandit) submission")
    r = sub.add_parser("train-rl", help="train REINFORCE on early dev, evaluate on dev-late, write run")
    r.add_argument("--seeds", default="1,2,3")
    r.add_argument("--epochs", type=int, default=200)
    r.add_argument("--lr", type=float, default=0.05)
    r.add_argument("--l2", type=float, default=1e-3)
    sub.add_parser("validate", help="validate every run against Arena Standard v0")
    sub.add_parser("build", help="score runs and write dashboard data")
    rp = sub.add_parser("reproduce", help="verify a run can be regenerated (inputs, stored model, retrain at commit)")
    rp.add_argument("run_ids", nargs="+")
    rp.add_argument("--no-retrain", action="store_true", help="only check inputs and the stored model")
    rp.add_argument("--remote", help="also upload the report to the shared store (default: $ARENA_REMOTE if set)")
    sm = sub.add_parser("submit", help="validate and upload local runs to the shared store")
    sm.add_argument("run_ids", nargs="+")
    sm.add_argument("--remote")
    pl = sub.add_parser("pull", help="download all shared runs into the local cache")
    pl.add_argument("--remote")
    pb = sub.add_parser("publish", help="upload the built dashboard to <remote>/site")
    pb.add_argument("--remote")
    pb.add_argument("--data-only", action="store_true", help="upload only site/data (scheduled refresh)")
    ls = sub.add_parser("remote-ls", help="list runs in the shared store")
    ls.add_argument("--remote")
    st = sub.add_parser("study", help="condition x model experiment grids")
    st.add_argument("action", choices=["add", "submit", "build", "list", "watch", "status"])
    st.add_argument("folders", nargs="*", help="experiment folders (read only), e.g. outputs/model-research/entry-gate/20261114-v1")
    st.add_argument("--remote")
    st.add_argument("--since", default="2026-11-14", help="watch: ignore experiments older than this date")
    st.add_argument("--min-age", type=int, default=600, help="watch: seconds trades.parquet must be unchanged")
    st.add_argument("--interval", type=int, default=0, help="watch: repeat every N seconds (0 = run once)")
    st.add_argument("--dry-run", action="store_true")
    st.add_argument("--no-refresh", action="store_true", help="watch: do not request a dashboard refresh")
    s = sub.add_parser("serve", help="serve dashboard")
    s.add_argument("--port", type=int, default=8765)
    a = p.parse_args()

    if a.cmd == "import-selective":
        from .adapters import import_selective
        print("\n".join(import_selective()))
    elif a.cmd == "baseline-random":
        from .adapters import baseline_random
        print("\n".join(baseline_random()))
    elif a.cmd == "example-rl":
        from .examples.bandit_agent import run
        print(run())
    elif a.cmd == "train-rl":
        from .rl_train import train
        for s in a.seeds.split(","):
            path, summary = train(epochs=a.epochs, seed=int(s), lr=a.lr, l2=a.l2)
            print(path.name, json.dumps(summary))
    elif a.cmd == "validate":
        from .build import validate_all
        report = validate_all()
        for k, v in report.items():
            print(("OK   " if not v else "FAIL ") + k + "".join("\n     - " + e for e in v))
        raise SystemExit(1 if any(report.values()) else 0)
    elif a.cmd == "build":
        from .build import build
        d = build()
        print(json.dumps({"runs": len(d["runs"]), "studies": len(d["studies"]), "lab_sources": len(d["lab"]),
                          "rejected": d["rejected"], "site": str(SITE)}, ensure_ascii=False))
    elif a.cmd == "reproduce":
        import os
        from .reproduce import reproduce
        from .store import RUNS
        url = a.remote or os.environ.get("ARENA_REMOTE")
        bad = 0
        for rid in a.run_ids:
            rep = reproduce(rid, retrain=not a.no_retrain)
            mark = {"reproduced": "✓", "partial": "△", "not_reproduced": "✗"}[rep["verdict"]]
            print(f"{mark} {rid}: {rep['verdict']}")
            for c in rep["checks"]:
                print(f"    [{c['status']:4s}] {c['name']}: {c['detail']}")
            if url:
                from .remote import record_reproduction
                latest = sorted((RUNS / rid / "reproductions").glob("*.json"))[-1]
                record_reproduction(rid, latest, url)
            bad += rep["verdict"] == "not_reproduced"
        raise SystemExit(1 if bad else 0)
    elif a.cmd in ("submit", "pull", "publish", "remote-ls"):
        from . import remote as rm
        url = rm.remote_from_env(a.remote)
        if a.cmd == "submit":
            for rid in a.run_ids:
                print(json.dumps(rm.submit(rid, url), ensure_ascii=False))
        elif a.cmd == "pull":
            print(f"pulled {rm.pull(url)} runs from {url}")
        elif a.cmd == "publish":
            rm.publish_site(url, data_only=a.data_only)
            from .store import OUT
            rm.push_conditions(url, OUT / "conditions")
            print(f"published dashboard to {url}/site")
        else:
            b = rm.backend(url)
            for rid in b.list_runs():
                sub_ = b.read_json(rid, "submission.json") or {}
                print(f"{rid:48s} {sub_.get('submitted_by', '?'):20s} {sub_.get('submitted_at', '')}")
    elif a.cmd == "study":
        from . import studies
        if a.action in ("add", "submit"):
            for f in a.folders:
                dest = studies.add_local(f)
                print(f"added {dest.name}")
                if a.action == "submit":
                    from . import remote as rm
                    url = rm.remote_from_env(a.remote)
                    rm.backend(url).upload_dir(dest, f"studies/{dest.name}")
                    print(f"submitted to {url}/studies/{dest.name}")
        elif a.action == "build":
            for s_ in studies.build_all(SITE / "data" / "studies"):
                print(s_["study_id"], s_["n_trades"], "cells", s_["trials"], "significant", s_["significant_cells"])
        elif a.action == "watch":
            import os
            import time
            from .watch import watch_once
            url = a.remote or os.environ.get("ARENA_REMOTE")
            stamp = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
            while True:
                done = watch_once(url, since=a.since, min_age=a.min_age, dry_run=a.dry_run, refresh=not a.no_refresh,
                                  log=lambda m: print(f"[{stamp()}] {m}", flush=True))
                if not done:
                    print(f"[{stamp()}] no new finished experiments", flush=True)
                if not a.interval:
                    break
                time.sleep(a.interval)
        elif a.action == "status":
            from .watch import status_table
            for folder, st_, at in status_table(since=a.since, min_age=a.min_age):
                print(f"{folder:44s} {st_:10s} {at}")
        else:
            for p in sorted(studies.STUDIES.iterdir()) if studies.STUDIES.exists() else []:
                print(p.name)
    elif a.cmd == "serve":
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        print(f"http://localhost:{a.port}")
        http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
