"""Demo RL submission: contextual epsilon-greedy bandit that learns take/skip online.

This is a *pipeline demo*, not a research result. It shows how an RL-family model
produces an Arena run: decide with information available at decision time, learn
only from trades already closed (previous days), emit trades.json + curve.json.

    uv run python -m arena example-rl
"""
from __future__ import annotations

import random

from ..adapters import TRACK, load_selective, write_with_late
from ..store import sha


def context(row) -> tuple:
    return (row.rule_stage, row.market, min(int(row.daily_strength_rank), 3))


def run(epsilon=0.1, seed=11, prior_n=5):
    t = load_selective()
    pool = t[(t["mode"] == "current") & (t["policy"] == "after_tp3") & (t["method"] == "system_only")]
    rng = random.Random(seed)
    q: dict[tuple, list[float]] = {}  # ctx -> [sum_reward, count]
    taken, curve, cum = [], [], 0.0
    for day, g in pool.groupby("day", sort=True):
        today = []
        for row in g.itertuples():
            s, n = q.get(context(row), [0.0, 0])
            value = s / (n + prior_n)  # shrink toward 0 => optimistic-neutral start
            explore = rng.random() < epsilon
            if explore or value >= 0:
                today.append(row)
        for row in today:  # learn at end of day: rewards are known only after the trade closes
            reward = row.realized_net_return if row.complete else 0.0
            stats = q.setdefault(context(row), [0.0, 0])
            stats[0] += reward
            stats[1] += 1
            cum += reward
        taken += today
        curve.append({"step": len(curve) + 1, "value": round(cum * 100, 4), "metric": "cum_reward_pp", "day": day})
    frame = pool.loc[[r.Index for r in taken]]
    params = {"epsilon": epsilon, "seed": seed, "prior_n": prior_n, "pool": "current/after_tp3/system_only"}
    return write_with_late({
        "run_id": f"demo.bandit.eps{epsilon}.s{seed}",
        "track_id": TRACK, "split": "dev",
        "model": {"name": "contextual ε-greedy bandit (demo)", "family": "rl", "version": "v0"},
        "author": "arena example",
        "artifact_hash": sha(params), "params": params,
        "notes": "파이프라인 시연용 온라인 학습. 문맥=(규칙 단계, 시장, 일봉 강도 순위). 전날까지 청산된 보상만 학습.",
    }, frame, curve)
