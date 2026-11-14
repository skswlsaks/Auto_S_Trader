"""Hosted mode end to end: the MCP server over stateless HTTP (as on AgentCore) + the teammate proxy.

A fake bearer token stands in for the Cognito ID token (AgentCore verifies the real one before the
request reaches the container). Checks identity, content upload of local files, drafts surviving an
instance change, submitter recorded from the token, and the hosted-mode guard on reproduce.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
PROXY = REPO / "tools" / "arena_mcp_proxy.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()


class HostedModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.port = _free_port()
        for d in ("shared", "out", "home"):
            (cls.tmp / d).mkdir()
        env = {**os.environ, "ARENA_MODE": "remote", "ARENA_REMOTE": str(cls.tmp / "shared"), "ARENA_OUTPUTS": str(cls.tmp / "out"),
               "ARENA_PORT": str(cls.port)}
        cls.server = subprocess.Popen([sys.executable, "-m", "arena.mcp_server"], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 30
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", cls.port)) == 0:
                    break
            time.sleep(0.2)
        else:
            cls.server.kill()
            raise RuntimeError("server did not start: " + cls.server.stderr.read().decode()[-500:])
        token = f"{_b64({'alg': 'none'})}.{_b64({'email': 'teammate@example.com', 'sub': 'u-1', 'exp': int(time.time()) + 3600})}.sig"
        home = cls.tmp / "home"
        (home / "config.json").write_text(json.dumps({"url": f"http://127.0.0.1:{cls.port}/mcp", "domain": "unused", "client_id": "unused"}))
        (home / "credentials.json").write_text(json.dumps({"id_token": token, "refresh_token": "r", "expires_at": time.time() + 3600,
                                                          "email": "teammate@example.com"}))
        cls.trades = cls.tmp / "my_trades.parquet"
        pd.DataFrame([{"symbol": "000001", "entry_at": f"2026-03-0{d}T01:00:00+00:00", "exit_at": f"2026-03-0{d}T05:00:00+00:00",
                       "net_return": r, "complete": True} for d, r in ((2, 0.01), (3, -0.005), (4, 0.0), (5, 0.02))]).to_parquet(cls.trades)
        study = cls.tmp / "studies-src" / "new-exp" / "20261120-v1"
        study.mkdir(parents=True)
        pd.DataFrame({"symbol": list("abcd"), "D": ["2026-11-16", "2026-11-16", "2026-11-17", "2026-11-17"], "status": "ok",
                      "net": [0.01, -0.02, 0.0, 0.03], "entry_variant": ["G1", "M1", "G1", "M1"], "policy": "X0_base",
                      "period": ["post", "post", "pre", "post"]}).to_parquet(study / "trades.parquet")
        (study / "REPORT.md").write_text("done")
        cls.study = study

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def proxy(self, *messages):
        env = {**os.environ, "ARENA_MCP_HOME": str(self.tmp / "home")}
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        lines = "\n".join(json.dumps(m) for m in (init, *messages)) + "\n"
        out = subprocess.run([sys.executable, str(PROXY), "serve"], input=lines, capture_output=True, text=True, env=env, timeout=120)
        res = {}
        for line in out.stdout.splitlines():
            m = json.loads(line)
            if m.get("id"):
                res[m["id"]] = json.loads(m["result"]["content"][0]["text"]) if "result" in m else m["error"]
        return res

    def call(self, i, name, args):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}

    def test_hosted_flow(self):
        r = self.proxy(self.call(1, "arena_whoami", {}),
                       self.call(2, "arena_create_run", {"run_id": "team.test.v1", "track_id": "kr-selective-daytrade-v0", "split": "dev",
                                                         "model_name": "t", "family": "rule", "version": "1", "trades_path": str(self.trades)}))
        self.assertEqual(r[1]["caller"]["email"], "teammate@example.com")
        self.assertTrue(r[2]["ok"], r[2])
        self.assertAlmostEqual(r[2]["metrics_preview"]["accuracy"], 0.75)
        shutil.rmtree(self.tmp / "out" / "arena" / "runs")  # the next request lands on a fresh instance
        r = self.proxy(self.call(3, "arena_submit_run", {"run_id": "team.test.v1"}),
                       self.call(4, "arena_preview_study", {"folder": str(self.study)}),
                       self.call(5, "arena_reproduce_run", {"run_id": "team.test.v1"}),
                       self.call(6, "arena_submit_study", {"folder": str(self.study)}))
        self.assertEqual((r[3]["status"], r[3]["submitted_by"]), ("submitted", "teammate@example.com"))
        self.assertEqual(r[4]["study_id"], "new-exp-20261120-v1")
        self.assertEqual(r[5]["verdict"], "unavailable")
        self.assertEqual(r[6]["submitted_by"], "teammate@example.com")
        run = json.loads((self.tmp / "shared" / "runs" / "team.test.v1" / "run.json").read_text())
        self.assertEqual(run["author"], "teammate@example.com")
        self.assertEqual(run["provenance"]["created_via"]["caller"], "teammate@example.com")
        self.assertTrue((self.tmp / "shared" / "studies" / "new-exp-20261120-v1" / "trades.parquet").exists())

    def test_local_paths_rejected_without_proxy(self):
        import urllib.request
        body = json.dumps(self.call(9, "arena_create_run", {"run_id": "x", "track_id": "kr-selective-daytrade-v0", "split": "dev",
                                                            "model_name": "t", "family": "rule", "version": "1", "trades_path": "/etc/hosts"})).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        msg = json.loads(urllib.request.urlopen(req, timeout=30).read())
        self.assertTrue(msg["result"]["isError"])
        self.assertIn("cannot read files on your computer", msg["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
