import json
import tempfile
import unittest
from pathlib import Path

from arena.provenance import check_inputs, collect, input_info
from arena.store import result_sha256, write_run
from arena.tests.test_arena import trade


class ProvenanceTest(unittest.TestCase):
    def test_collect_records_code_env_command(self):
        p = collect(["train-rl", "--seeds", "1"])
        self.assertEqual(p["command"], ["train-rl", "--seeds", "1"])
        self.assertIn("python", p["env"])
        self.assertIn("dirty", p["code"])
        self.assertTrue(p["code"]["commit"] is None or len(p["code"]["commit"]) == 40)
        self.assertNotIn("gitignore", p["code"]["dirty_files"])  # leading status column preserved

    def test_input_hash_detects_changed_and_missing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            f = out / "d.parquet"
            f.write_bytes(b"v1")
            import arena.provenance as pv
            old = pv.OUTPUTS
            pv.OUTPUTS = out
            try:
                rec = input_info([f])
            finally:
                pv.OUTPUTS = old
            self.assertEqual(check_inputs(rec, out)[0]["status"], "ok")
            g = out / "d2.parquet"
            g.write_bytes(b"v2")  # new file, different content, same recorded name below
            changed = [{**rec[0], "path": "d2.parquet"}]
            self.assertEqual(check_inputs(changed, out)[0]["status"], "changed")
            self.assertEqual(check_inputs([{**rec[0], "path": "gone"}], out)[0]["status"], "missing")

    def test_result_hash_ignores_evidence_but_not_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"run_id": "r", "track_id": "t", "split": "dev", "author": "a", "artifact_hash": "h",
                    "model": {"name": "m", "family": "rl", "version": "1"}}
            a = write_run(meta, [trade("2026-01-05", 0.01)], runs_dir=Path(tmp) / "a")
            b = write_run(meta | {"notes": "x"}, [trade("2026-01-05", 0.01)], runs_dir=Path(tmp) / "b",
                          artifacts={"m.json": b"{}"}, provenance={"command": ["x"]})
            c = write_run(meta, [trade("2026-01-05", 0.02)], runs_dir=Path(tmp) / "c")
            self.assertEqual(result_sha256(a), result_sha256(b))
            self.assertNotEqual(result_sha256(a), result_sha256(c))
            self.assertEqual(json.loads((b / "run.json").read_text())["artifacts"][0]["bytes"], 2)


if __name__ == "__main__":
    unittest.main()
