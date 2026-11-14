from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .toss import credential_status


def status(config):
    root = Path(config["data_dir"])
    result = {"credentials": credential_status(config["credentials_file"]), "data_dir": str(root)}
    if (root / "raw.sqlite").exists():
        with closing(sqlite3.connect(f"file:{root / 'raw.sqlite'}?mode=ro", uri=True)) as db:
            result["collection"] = {"candles": db.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
                                     "series_completed": db.execute("SELECT COUNT(*) FROM progress WHERE complete=1").fetchone()[0],
                                     "series_started": db.execute("SELECT COUNT(*) FROM progress").fetchone()[0]}
    if (root / "universe.json").exists():
        result["universe_symbols"] = len(json.loads((root / "universe.json").read_text())["symbols"])
    if (root / "dataset.json").exists():
        manifest = json.loads((root / "dataset.json").read_text())
        result["dataset"] = {k: manifest[k] for k in ("candidate_rows", "filled_rows", "first_day", "last_day", "partial")}
    if (Path(config["runs_dir"]) / "latest.json").exists():
        result["model"] = json.loads((Path(config["runs_dir"]) / "latest.json").read_text())
    pipeline = root.parent / "pipeline-state.json"
    if pipeline.exists():
        state = json.loads(pipeline.read_text())
        try:
            os.kill(state["pid"], 0)
            state["process_alive"] = True
        except ProcessLookupError:
            state["process_alive"] = False
        except PermissionError:
            state["process_alive"] = None
        result["pipeline"] = state
    return result


def main():
    parser = argparse.ArgumentParser(description="Local Toss semiconductor model research; no order API")
    parser.add_argument("command", choices=["status", "probe", "collect", "build", "train", "evaluate", "predict", "serve", "watch", "benchmark", "run"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--run-dir")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "status":
        result = status(config)
    elif args.command in ("probe", "collect"):
        from .data import collect, probe
        result = (probe if args.command == "probe" else collect)(config)
    elif args.command == "build":
        from .features import build
        result = build(config, allow_partial=args.allow_partial)
        result = {k: v for k, v in result.items() if k != "source_files"}
    elif args.command == "train":
        from .train import optimize
        result = optimize(config, diagnostic=args.diagnostic)
    elif args.command in ("evaluate", "predict", "serve", "watch", "benchmark"):
        run = args.run_dir
        if not run:
            run = json.loads((Path(config["runs_dir"]) / "latest.json").read_text())["run_dir"]
        if args.command == "evaluate":
            from .train import evaluate
            result = evaluate(run)
        elif args.command == "predict":
            from .infer import historical_predictions
            result = historical_predictions(run, args.count)
        elif args.command == "benchmark":
            from .infer import benchmark
            result = benchmark(run, args.count)
        elif args.command == "watch":
            from .feed import watch
            watch(run, sys.stdout, once=args.once, as_of=args.as_of)
            return
        else:
            from .infer import serve
            serve(run, sys.stdin, sys.stdout)
            return
    else:
        if args.allow_partial or args.diagnostic:
            raise ValueError("Full run cannot use partial/diagnostic flags")
        from .data import collect
        from .features import build
        from .train import optimize, evaluate
        from .infer import historical_predictions
        collect(config)
        build(config)
        trained = optimize(config)
        evaluated = evaluate(trained["run_dir"])
        result = {"run_dir": trained["run_dir"], "evaluation": evaluated,
                  "inference": historical_predictions(trained["run_dir"])}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, KeyError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
