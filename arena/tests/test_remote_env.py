import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from arena import remote
from arena.rl_env import SelectiveReplayEnv
from arena.store import write_run
from arena.tests.test_arena import TRACK, trade


def _runs(tmp: Path, **over):
    meta = {"run_id": "r1", "track_id": "t", "split": "dev", "author": "a", "artifact_hash": "h",
            "model": {"name": "m", "family": "rl", "version": "1"}} | over
    write_run(meta, [trade("2026-01-05", 0.01)], runs_dir=tmp)


class FolderRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.local, self.shared = self.tmp / "local", self.tmp / "shared"
        track = TRACK | {"windows": TRACK["windows"] | {"holdout": {"start": "2026-01-05", "end": "2026-01-09"}},
                         "holdout_submissions_per_model": 1}
        tracks = self.tmp / "tracks"
        tracks.mkdir()
        (tracks / "t.json").write_text(json.dumps(track))
        self.patch = mock.patch.object(remote, "TRACKS", tracks)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_submit_is_immutable_and_pull_round_trips(self):
        _runs(self.local)
        r = remote.submit("r1", str(self.shared), self.local, who="kim")
        self.assertEqual(r["status"], "submitted")
        self.assertEqual(remote.submit("r1", str(self.shared), self.local)["status"], "unchanged")
        _runs(self.local, notes="changed")
        with self.assertRaises(SystemExit):
            remote.submit("r1", str(self.shared), self.local)
        other = self.tmp / "other"
        self.assertEqual(remote.pull(str(self.shared), other), 1)
        self.assertEqual(json.loads((other / "r1" / "submission.json").read_text())["submitted_by"], "kim")

    def test_invalid_run_is_rejected(self):
        _runs(self.local, model={"name": "m", "family": "nope", "version": "1"})
        with self.assertRaises(SystemExit):
            remote.submit("r1", str(self.shared), self.local)
        self.assertEqual(remote.backend(str(self.shared)).list_runs(), [])

    def test_holdout_cap_per_model(self):
        _runs(self.local, run_id="h1", split="holdout")
        _runs(self.local, run_id="h2", split="holdout")
        remote.submit("h1", str(self.shared), self.local)
        with self.assertRaises(SystemExit) as e:
            remote.submit("h2", str(self.shared), self.local)
        self.assertIn("holdout cap", str(e.exception))


def _candidates():
    days = ["2026-01-05"] * 4 + ["2026-01-06"] * 2
    return pd.DataFrame({
        "symbol": list("abcdef"), "day": days,
        "signal_at": [f"{d}T0{i}:00:00+00:00" for i, d in enumerate(days)],
        "entry_price": 100.0, "stop_price": 99.0, "rule_stage": "breakout", "market": "KOSDAQ",
        "daily_strength_score": 0.5, "daily_strength_rank": 1, "momentum20": 0.1, "atr20_pct": 0.03,
        "ma60_slope20": 0.01, "log_turnover20": 20.0, "r_0": [0.01, -0.02, 0.03, 0.05, -0.01, 0.02], "idx_0": range(6),
    })


class ReplayEnvTest(unittest.TestCase):
    def test_take_all_matches_reward_sum_and_daily_cap(self):
        env = SelectiveReplayEnv(_candidates(), "2026-01-05", "2026-01-06", max_per_day=3)
        obs, _ = env.reset()
        self.assertEqual(obs.shape, (env.obs_dim,))
        total, done, steps = 0.0, False, 0
        while not done:
            obs, r, done, trunc, _ = env.step(1)
            total += r
            steps += 1
        self.assertEqual(steps, 6)
        self.assertEqual(len(env.log), 5)  # 4th signal on day 1 is blocked by the 3/day system cap
        self.assertAlmostEqual(total, 0.01 - 0.02 + 0.03 - 0.01 + 0.02)

    def test_eval_env_reuses_train_normalisation(self):
        tr = SelectiveReplayEnv(_candidates(), "2026-01-05", "2026-01-05")
        ev = SelectiveReplayEnv(_candidates(), "2026-01-06", "2026-01-06", norm=tr.norm)
        self.assertIs(ev.norm, tr.norm)
        self.assertTrue(np.isfinite(ev.x).all())


if __name__ == "__main__":
    unittest.main()
