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
    sub.add_parser("validate", help="validate every run against Arena Standard v0")
    sub.add_parser("build", help="score runs and write dashboard data")
    sm = sub.add_parser("submit", help="validate and upload local runs to the shared store")
    sm.add_argument("run_ids", nargs="+")
    sm.add_argument("--remote")
    pl = sub.add_parser("pull", help="download all shared runs into the local cache")
    pl.add_argument("--remote")
    pb = sub.add_parser("publish", help="upload the built dashboard to <remote>/site")
    pb.add_argument("--remote")
    ls = sub.add_parser("remote-ls", help="list runs in the shared store")
    ls.add_argument("--remote")
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
            path, summary = train(epochs=a.epochs, seed=int(s))
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
        print(json.dumps({"runs": len(d["runs"]), "rejected": d["rejected"], "site": str(SITE)}, ensure_ascii=False))
    elif a.cmd in ("submit", "pull", "publish", "remote-ls"):
        from . import remote as rm
        url = rm.remote_from_env(a.remote)
        if a.cmd == "submit":
            for rid in a.run_ids:
                print(json.dumps(rm.submit(rid, url), ensure_ascii=False))
        elif a.cmd == "pull":
            print(f"pulled {rm.pull(url)} runs from {url}")
        elif a.cmd == "publish":
            rm.publish_site(url)
            print(f"published dashboard to {url}/site")
        else:
            b = rm.backend(url)
            for rid in b.list_runs():
                sub_ = b.read_json(rid, "submission.json") or {}
                print(f"{rid:48s} {sub_.get('submitted_by', '?'):20s} {sub_.get('submitted_at', '')}")
    elif a.cmd == "serve":
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
        print(f"http://localhost:{a.port}")
        http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
