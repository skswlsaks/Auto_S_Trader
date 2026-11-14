import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from arena import remote, studies, watch


def _experiment(root: Path, kind="new-exp", version="20261120-v1", nets=(0.01, -0.02, 0.0, 0.03)):
    d = root / kind / version
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": ["a", "b", "c", "d"], "D": ["2026-11-16", "2026-11-16", "2026-11-17", "2026-11-17"],
                  "status": "ok", "net": list(nets), "entry_variant": ["G1", "M1", "G1", "M1"],
                  "policy": "X0_base", "period": ["post", "post", "pre", "post"], "risk_flag": [True, False, False, True]}
                 ).to_parquet(d / "trades.parquet")
    (d / "protocol.json").write_text(json.dumps({"version": "new-exp-v1", "entries": {"G1": "reclaim"}}))
    return d


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root, self.shared = self.tmp / "model-research", self.tmp / "shared"
        self.patches = [mock.patch.object(watch, "ROOT", self.root), mock.patch.object(watch, "STATE", self.tmp / "state.json"),
                        mock.patch.object(studies, "STUDIES", self.tmp / "cache")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_watch(self, **kw):
        return watch.watch_once(str(self.shared), since=None, refresh=False, log=lambda m: None, **kw)

    def test_infers_spec_for_unknown_experiment(self):
        spec = studies.infer_spec(_experiment(self.root))
        self.assertEqual([d["key"] for d in spec["dimensions"]], ["entry_variant", "risk_flag", "period"])
        self.assertTrue(spec["inferred"])
        s = studies.build_study(self.root / "new-exp" / "20261120-v1", self.tmp / "out")
        all_cell = next(c for c in s["cells"] if c["key"] == {"entry_variant": "G1", "risk_flag": "*", "period": "*"})
        self.assertEqual(all_cell["n"], 2)

    def test_submits_finished_once_and_again_after_change(self):
        d = _experiment(self.root)
        old = time.time() - 3600
        os.utime(d / "trades.parquet", (old, old))
        self.assertEqual(self.run_watch(), [], "no result marker yet -> still running")
        (d / "REPORT.md").write_text("done")
        self.assertEqual(self.run_watch(), ["new-exp-20261120-v1"])
        self.assertTrue((self.shared / "studies" / "new-exp-20261120-v1" / "trades.parquet").exists())
        self.assertEqual(self.run_watch(), [], "unchanged -> not resubmitted")
        _experiment(self.root, nets=(0.02, -0.02, 0.0, 0.03))
        os.utime(d / "trades.parquet", (old, old))
        self.assertEqual(self.run_watch(), ["new-exp-20261120-v1"], "changed result -> resubmitted")

    def test_recently_written_results_wait(self):
        d = _experiment(self.root)
        (d / "REPORT.md").write_text("done")
        self.assertEqual(self.run_watch(min_age=600), [])


if __name__ == "__main__":
    unittest.main()
