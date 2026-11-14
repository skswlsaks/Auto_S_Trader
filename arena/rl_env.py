"""Gym-style replay environment over the selective-daytrade candidate stream.

API mirrors gymnasium (reset() -> (obs, info), step(a) -> (obs, reward, terminated, truncated, info))
without requiring the gymnasium package. If gymnasium is installed, `as_gymnasium()` wraps it.

One step = one intraday entry signal, in time order. Actions:
  0 = skip, k>=1 = take with the k-th pre-approved fixed exit config (default: only after_tp3/system_only).
The model never edits protection rules (DECISIONS D12); it only chooses among frozen exit configs.
Observation uses information known at the signal time only: D-close daily features, the signal
time of day, stop distance, rule stage, market and how many entries were already taken today.
Reward = realized net return of the chosen config (0 if skipped or unclosed), matching the platform's
closed-trade total so the training signal and the leaderboard agree.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .adapters import SELECTIVE, load_selective

DAILY = ["daily_strength_score", "daily_strength_rank", "momentum20", "atr20_pct", "ma60_slope20", "log_turnover20"]
STAGES = ["observing", "breakout", "pullback", "confirmed"]
DEFAULT_EXITS = (("after_tp3", "system_only"),)


def build_candidates(mode="current", exits=DEFAULT_EXITS) -> pd.DataFrame:
    """One row per entry signal with reward columns r_0..r_{k-1} for each exit config."""
    t = load_selective()
    t = t[t["mode"] == mode]
    sel = pd.read_parquet(SELECTIVE / "selection.parquet")
    sel = sel[["symbol", "day", "momentum20", "atr20_pct", "ma60_slope20", "log_turnover20"]].drop_duplicates(["symbol", "day"])
    key = ["symbol", "day", "signal_at"]
    base = None
    for k, (policy, method) in enumerate(exits):
        g = t[(t["policy"] == policy) & (t["method"] == method)].copy()
        g[f"r_{k}"] = np.where(g["complete"], g["realized_net_return"], 0.0)  # = platform total (closed only)
        g[f"idx_{k}"] = g.index
        cols = key + [f"r_{k}", f"idx_{k}"]
        base = g if base is None else base.merge(g[cols], on=key, how="inner")
    base = base.merge(sel, on=["symbol", "day"], how="left")
    return base.sort_values(["signal_at", "symbol"]).reset_index(drop=True)


def _raw_features(c: pd.DataFrame) -> np.ndarray:
    ts = pd.to_datetime(c["signal_at"], utc=True).dt.tz_convert("Asia/Seoul")
    minute = (ts.dt.hour * 60 + ts.dt.minute - 9 * 60) / 390.0
    stop = (c["entry_price"] - c["stop_price"]) / c["entry_price"]
    cols = [c[f].astype(float).fillna(0.0) for f in DAILY] + [minute, stop.astype(float)]
    cols += [(c["rule_stage"] == s).astype(float) for s in STAGES]
    cols += [(c["market"] == "KOSDAQ").astype(float)]
    return np.column_stack(cols)


FEATURE_NAMES = DAILY + ["signal_minute", "stop_distance"] + [f"stage_{s}" for s in STAGES] + ["kosdaq", "taken_today"]


class SelectiveReplayEnv:
    def __init__(self, candidates: pd.DataFrame, start: str, end: str, norm=None, max_per_day=3):
        c = candidates[(candidates["day"] >= start) & (candidates["day"] <= end)].reset_index(drop=True)
        if c.empty:
            raise ValueError(f"no candidates in {start}..{end}")
        self.c, self.max_per_day = c, max_per_day
        raw = _raw_features(c)
        self.norm = norm or (raw.mean(0), raw.std(0) + 1e-9)  # fit on train env only, pass to eval env
        self.x = (raw - self.norm[0]) / self.norm[1]
        self.rewards = c[[k for k in c.columns if k.startswith("r_")]].to_numpy(float)
        self.n_actions = 1 + self.rewards.shape[1]
        self.obs_dim = self.x.shape[1] + 1

    def _obs(self):
        return np.append(self.x[self.i], self.taken_today / self.max_per_day)

    def reset(self, seed=None):
        self.i, self.taken_today, self.day, self.log = 0, 0, self.c.at[0, "day"], []
        return self._obs(), {"day": self.day}

    def step(self, action: int):
        if not 0 <= action < self.n_actions:
            raise ValueError("invalid action")
        if self.taken_today >= self.max_per_day:
            action = 0  # system cap, not the agent's choice
        reward = 0.0 if action == 0 else float(self.rewards[self.i, action - 1])
        if action:
            self.taken_today += 1
            self.log.append((self.i, action - 1))
        self.i += 1
        done = self.i >= len(self.c)
        if not done and self.c.at[self.i, "day"] != self.day:
            self.day, self.taken_today = self.c.at[self.i, "day"], 0
        obs = np.zeros(self.obs_dim) if done else self._obs()
        return obs, reward, done, False, {"day": self.day}

    def taken_frame(self) -> pd.DataFrame:
        """Source trade rows for the actions taken (for an Arena submission)."""
        t = load_selective()
        return t.loc[[int(self.c.at[i, f"idx_{k}"]) for i, k in self.log]]


def as_gymnasium(env: SelectiveReplayEnv):  # pragma: no cover - optional dependency
    import gymnasium as gym

    class Wrapped(gym.Env):
        observation_space = gym.spaces.Box(-math.inf, math.inf, (env.obs_dim,), np.float64)
        action_space = gym.spaces.Discrete(env.n_actions)

        def reset(self, *, seed=None, options=None):
            return env.reset(seed)

        def step(self, a):
            return env.step(int(a))

    return Wrapped()
