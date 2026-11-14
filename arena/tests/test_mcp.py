"""MCP server: tool registry, in-process calls, local write/submit flow, and the stdio transport."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from arena import mcp_server, store
from arena.query import SharedData, lab_metrics, lab_query, resolve_filters, select
from arena.store import SITE

HAS_SITE = (SITE / "data" / "index.json").exists()
EXPECTED = {"arena_overview", "arena_list_runs", "arena_get_run", "arena_compare_runs", "arena_list_studies", "arena_study_grid",
            "arena_list_lab_sources", "arena_lab_query", "arena_metric_definitions", "arena_schema", "arena_create_run",
            "arena_validate_run", "arena_submit_run", "arena_preview_study", "arena_submit_study", "arena_request_refresh", "arena_reproduce_run"}


def run(coro):
    return asyncio.run(coro)


def text(result):
    return result.content[0].text


class RegistryTest(unittest.TestCase):
    def test_tools_prompt_resources(self):
        async def go():
            async with Client(mcp_server.server) as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                prompts = [p.name for p in (await c.list_prompts()).prompts]
                res = [str(r.uri) for r in (await c.list_resources()).resources]
                schema = text(await c.call_tool("arena_schema", {}))
                return tools, prompts, res, schema
        tools, prompts, res, schema = run(go())
        self.assertEqual(set(tools), EXPECTED)
        self.assertTrue(tools["arena_list_runs"].annotations.read_only_hint)
        self.assertFalse(tools["arena_submit_run"].annotations.read_only_hint)
        self.assertIn("submit_results", prompts)
        self.assertIn("arena://schema", res)
        self.assertIn("net_return", schema)


@unittest.skipUnless(HAS_SITE, "local dashboard build not present")
class ReadToolsTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ARENA_REMOTE": ""})
        self.env.start()
        mcp_server._data = SharedData(remote=None)

    def tearDown(self):
        self.env.stop()
        mcp_server._data = None

    def test_read_tools_over_local_build(self):
        async def go():
            async with Client(mcp_server.server) as c:
                ov = json.loads(text(await c.call_tool("arena_overview", {})))
                runs = json.loads(text(await c.call_tool("arena_list_runs", {"split": "dev-late", "limit": 5})))
                grid = json.loads(text(await c.call_tool("arena_study_grid", {"study_id": "picks-runner-20261114-v1"})))
                lab = json.loads(text(await c.call_tool("arena_lab_query", {"source_id": "study:entry-gate-20261114-v1",
                                                                              "filters": {"variant": ["A_close"], "gate_ok": ["True"]}, "group_by": ["period"]})))
                return ov, runs, grid, lab
        ov, runs, grid, lab = run(go())
        self.assertIn("kr-selective-daytrade-v0:dev-late", ov["runs_per_split"])
        self.assertEqual((runs["split"], len(runs["runs"])), ("dev-late", 5))
        self.assertGreaterEqual(runs["runs"][0]["mean_return"], runs["runs"][-1]["mean_return"])
        self.assertTrue(grid["cells"])
        self.assertIsNotNone(lab["random_selection_percentile"])
        self.assertIn("학습 후", {g["group"]["period"] for g in lab["groups"]})
        self.assertEqual(lab["applied_filters"]["gate_ok"], ["True"])

    def test_python_lab_matches_study_cells(self):
        data = SharedData(remote=None)
        src, study = data.lab("study:entry-gate-20261114-v1"), data.study("entry-gate-20261114-v1")
        dims = [d["key"] for d in study["dimensions"]]
        for cell in [c for c in study["cells"] if c["base"] and c["n"] >= 5][:40]:
            idx = select(src, resolve_filters(src, {k: [cell["key"][k]] for k in dims}))
            m = lab_metrics(src, idx, bootstrap=False)
            self.assertEqual((m["n"], m["days"]), (cell["n"], cell["days"]))
            self.assertAlmostEqual(m["mean"], cell["mean"], places=7)
            self.assertAlmostEqual(m["max_drawdown_pp"], cell["max_drawdown_pp"], places=4)
            if cell["sharpe_daily_ann"] is not None:
                self.assertAlmostEqual(m["sharpe_daily_ann"], cell["sharpe_daily_ann"], places=4)

    def test_bad_filter_value_lists_options(self):
        src = SharedData(remote=None).lab("study:entry-gate-20261114-v1")
        with self.assertRaises(ValueError) as e:
            lab_query(src, {"variant": ["NOPE"]})
        self.assertIn("A_close", str(e.exception))


class WriteSubmitTest(unittest.TestCase):
    def test_create_validate_submit_into_shared_folder(self):
        tmp = Path(tempfile.mkdtemp())
        trades = [{"symbol": "000001", "entry_at": f"2026-03-0{d}T01:00:00+00:00", "exit_at": f"2026-03-0{d}T05:00:00+00:00",
                   "net_return": r, "complete": True} for d, r in ((2, 0.01), (3, -0.005), (4, 0.0), (5, 0.02))]
        with mock.patch.object(store, "RUNS", tmp / "runs"), mock.patch.dict(os.environ, {"ARENA_REMOTE": str(tmp / "shared")}):
            async def go():
                async with Client(mcp_server.server) as c:
                    created = json.loads(text(await c.call_tool("arena_create_run", {
                        "run_id": "mcp.test.v1", "track_id": "kr-selective-daytrade-v0", "split": "dev", "model_name": "mcp test",
                        "family": "rule", "version": "1", "author": "tester", "trades": trades, "command": ["example-rl"]})))
                    first = json.loads(text(await c.call_tool("arena_submit_run", {"run_id": "mcp.test.v1"})))
                    again = json.loads(text(await c.call_tool("arena_submit_run", {"run_id": "mcp.test.v1"})))
                    return created, first, again
            created, first, again = run(go())
        self.assertTrue(created["ok"], created)
        self.assertAlmostEqual(created["metrics_preview"]["accuracy"], 0.75)
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(again["status"], "unchanged")
        self.assertTrue((tmp / "shared" / "runs" / "mcp.test.v1" / "trades.json").exists())


class StdioTest(unittest.TestCase):
    def test_stdio_transport_lists_tools(self):
        from mcp.client.stdio import StdioServerParameters
        params = StdioServerParameters(command=sys.executable, args=["-m", "arena.mcp_server"], cwd=str(Path(__file__).resolve().parents[2]))

        async def go():
            async with Client(params) as c:
                return {t.name for t in (await c.list_tools()).tools}
        self.assertEqual(run(go()), EXPECTED)


if __name__ == "__main__":
    unittest.main()
