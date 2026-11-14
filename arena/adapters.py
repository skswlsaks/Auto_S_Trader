"""Convert existing experiment outputs into Arena Standard runs."""
from __future__ import annotations

import json
import math
import random

import pandas as pd

from .store import OUTPUTS, sha, write_run

SELECTIVE = OUTPUTS / "model-research" / "selective-daytrade" / "20261101-v2"
TRACK = "kr-selective-daytrade-v0"
METHOD_FAMILY = {"system_only": ("rule", "sys"), "system_bearish_transfer_model": ("supervised", "bear")}


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def selective_trades(frame: pd.DataFrame) -> list[dict]:
    out = []
    for r in frame.itertuples():
        out.append({
            "symbol": r.symbol,
            "signal_at": r.signal_at,
            "entry_at": r.entry_at,
            "exit_at": r.exit_bar_end if r.complete else None,
            "entry_price": _num(r.entry_price),
            "quantity": int(r.quantity),
            "net_return": _num(r.realized_net_return if r.complete else r.marked_net_return),
            "complete": bool(r.complete),
            "exit_reason": r.exit_reason if r.complete else "unclosed",
            "ctx": {"stage": r.rule_stage, "market": r.market, "rank": int(r.daily_strength_rank)},
        })
    return out


def load_selective() -> pd.DataFrame:
    return pd.read_parquet(SELECTIVE / "trades.parquet").sort_values(["entry_at", "symbol"])


LATE = ("2026-07-01", "2026-10-28")


def write_with_late(meta: dict, frame: pd.DataFrame, curve=None) -> list[str]:
    """Write the full-dev run and its dev-late slice (same trades, same model) for fair comparison."""
    write_run(meta, selective_trades(frame), curve)
    late = frame[(frame["day"] >= LATE[0]) & (frame["day"] <= LATE[1])]
    write_run(meta | {"run_id": meta["run_id"] + "@late", "split": "dev-late",
                      "notes": meta.get("notes", "") + " [dev-late 구간 슬라이스]"}, selective_trades(late))
    return [meta["run_id"], meta["run_id"] + "@late"]


def import_selective() -> list[str]:
    t = load_selective()
    protocol = json.loads((SELECTIVE / "protocol.json").read_text())
    ids = []
    for (mode, policy, method), g in t.groupby(["mode", "policy", "method"]):
        family, short = METHOD_FAMILY[method]
        run_id = f"sel-v2.{mode}.{policy}.{short}"
        ids += write_with_late({
            "run_id": run_id,
            "track_id": TRACK,
            "split": "dev",
            "model": {"name": f"selective {mode} / {short}", "family": family, "version": "20261101-v2",
                      "entry_mode": mode, "exit_policy": policy, "exit_method": method},
            "author": "s_trader research (imported)",
            "artifact_hash": sha({k: protocol.get(k) for k in ("source_sha256", "up_model", "down_model")} | {"g": run_id}),
            "source": "outputs/" + str(SELECTIVE.relative_to(OUTPUTS)),
            "notes": f"{protocol['entry_modes'].get(mode, mode)} · {protocol['policies'].get(policy, policy)}. 회고 재생, walk-forward 아님.",
        }, g)
    return ids


def baseline_random(seeds=(1, 2, 3, 4, 5), keep=0.5) -> list[str]:
    """Random filter over the broadest candidate pool (current / after_tp3 / system_only)."""
    t = load_selective()
    pool = t[(t["mode"] == "current") & (t["policy"] == "after_tp3") & (t["method"] == "system_only")]
    ids = []
    for s in seeds:
        rng = random.Random(s)
        g = pool[[rng.random() < keep for _ in range(len(pool))]]
        run_id = f"baseline.random{int(keep * 100)}.s{s}"
        ids += write_with_late({
            "run_id": run_id, "track_id": TRACK, "split": "dev",
            "model": {"name": f"random {int(keep * 100)}% filter (seed {s})", "family": "baseline", "version": "v0"},
            "author": "arena", "artifact_hash": sha({"seed": s, "keep": keep, "pool": "current/after_tp3/system_only"}),
            "params": {"seed": s, "keep": keep, "pool": "current/after_tp3/system_only"},
            "notes": "같은 후보 집합에서 무작위로 고른 기준선. 거르는 정책(RL 포함)은 이 분포를 넘어야 의미가 있다.",
        }, g)
    return ids
