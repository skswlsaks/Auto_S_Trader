"""REINFORCE (softmax-linear policy, running baseline) trained on the early dev window,
evaluated deterministically on dev-late, submitted as an Arena run. numpy only."""
from __future__ import annotations

import numpy as np

from .adapters import TRACK, selective_trades
from .rl_env import DEFAULT_EXITS, FEATURE_NAMES, SelectiveReplayEnv, build_candidates
from .store import sha, write_run

TRAIN = ("2025-10-29", "2026-06-30")
EVAL = ("2026-07-01", "2026-10-28")


def softmax(z):
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def rollout(env, w, rng=None, greedy=False):
    obs, _ = env.reset()
    steps, total, done = [], 0.0, False
    while not done:
        p = softmax(w @ obs)
        a = int(p.argmax()) if greedy else int(rng.choice(len(p), p=p))
        nxt, r, done, _, _ = env.step(a)
        steps.append((obs, a, r, p))
        total += r
        obs = nxt
    return steps, total


def train(epochs=200, lr=0.05, seed=3, exits=DEFAULT_EXITS, l2=1e-3):
    c = build_candidates(exits=exits)
    tr = SelectiveReplayEnv(c, *TRAIN)
    ev = SelectiveReplayEnv(c, *EVAL, norm=tr.norm)
    rng = np.random.default_rng(seed)
    w = np.zeros((tr.n_actions, tr.obs_dim))
    w[0, -1] = 0.0
    baseline, curve = 0.0, []
    for ep in range(1, epochs + 1):
        steps, total = rollout(tr, w, rng)
        grad = np.zeros_like(w)
        for obs, a, r, p in steps:
            adv = (r - baseline) * 100  # contextual-bandit credit: reward is the trade's own outcome
            g = -np.outer(p, obs)
            g[a] += obs
            grad += adv * g
        baseline = 0.9 * baseline + 0.1 * np.mean([s[2] for s in steps])
        w += lr * (grad / len(steps) - l2 * w)
        _, greedy_train = rollout(tr, w, greedy=True)
        curve.append({"step": ep, "value": round(total * 100, 4), "metric": "train_episode_return_pp",
                      "greedy_train_pp": round(greedy_train * 100, 4)})
    _, eval_total = rollout(ev, w, greedy=True)
    params = {"algo": "REINFORCE softmax-linear", "epochs": epochs, "lr": lr, "seed": seed, "l2": l2,
              "train_window": TRAIN, "eval_window": EVAL, "exits": [list(e) for e in exits], "features": FEATURE_NAMES}
    weights = {f: round(float(v), 4) for f, v in zip(FEATURE_NAMES, w[1] - w[0])}
    frame = ev.taken_frame()
    path = write_run({
        "run_id": f"rl.reinforce.s{seed}@late",
        "track_id": TRACK, "split": "dev-late",
        "model": {"name": "REINFORCE take/skip policy", "family": "rl", "version": "v0"},
        "author": "arena rl_train",
        "artifact_hash": sha({"w": w.round(8).tolist(), **params}),
        "params": params | {"take_minus_skip_weights": weights},
        "notes": f"{TRAIN[0]}~{TRAIN[1]} 학습 → {EVAL[0]}~{EVAL[1]} 결정론 평가. 청산은 고정 시스템 주문({exits[0][0]}). "
                 "dev-late는 다른 모델 개발에는 노출된 기간이므로 최종 성능 근거가 아니다.",
    }, selective_trades(frame), curve)
    return path, {"eval_taken": len(frame), "eval_candidates": len(ev.c), "eval_return_pp": round(eval_total * 100, 3)}
