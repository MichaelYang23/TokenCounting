from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fixtures import S1, S2, build_projects

from tokencount.claude_code import ApiCall, load_tasks
from tokencount.cli import main
from tokencount.pricing import PriceTable, normalize_model
from tokencount.records import load_salt, record_id
from tokencount.report import percentile
from tokencount.schema import validate_record

CSV, BUILD, README, RENAME = (
    "Add a CSV export to the report page",
    "Why is the build failing?",
    "Summarize the README",
    "Rename the export button",
)
# Expected costs in millionths of a dollar, worked out by hand from prices.json.
COST_CSV = 15040 + 4220  # req_A (1h cache write, thinking) + req_B (old-style 5m write)
COST_BUILD = 2616 + 6444 + 540.3 + 107.7  # req_C, req_D (Opus) + req_E, req_F (Haiku sub-agent)
COST_README = 4090  # Sonnet 5.5
COST_RENAME = 1050  # dated Sonnet 4.5 ID


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tasks = load_tasks([build_projects(Path(cls.tmp.name))])
        cls.by_prompt = {t.prompt_text: t for t in cls.tasks}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_one_task_per_prompt_that_made_calls(self):
        # /clear and the resumed copy of the first prompt made no new calls, so they are dropped.
        self.assertEqual([t.prompt_text for t in self.tasks], [CSV, BUILD, README, RENAME])

    def test_entries_of_one_request_count_once(self):
        task = self.by_prompt[CSV]
        self.assertEqual([c.request_id for c in task.calls], ["req_A", "req_B"])
        self.assertEqual(task.calls[0].thinking, 200)
        self.assertIsNone(task.calls[1].thinking)
        self.assertEqual(dict(task.tools), {"Bash": 1})
        self.assertEqual(task.duration_s, 20)
        self.assertFalse(task.interrupted)

    def test_old_style_cache_total_counts_as_5_minute_write(self):
        call = self.by_prompt[CSV].calls[1]
        self.assertEqual((call.cache_write_5m, call.cache_write_1h), (200, 0))

    def test_subagent_calls_follow_the_tool_call_that_spawned_them(self):
        task = self.by_prompt[BUILD]
        self.assertEqual([c.request_id for c in task.main_calls], ["req_C", "req_D"])  # API error skipped
        # req_F finished after the next prompt; the .meta.json link still puts it here.
        self.assertEqual([c.request_id for c in task.subagent_calls], ["req_E", "req_F"])
        self.assertEqual(task.primary_model, "claude-opus-5-5")
        self.assertEqual(task.effort, "high")
        self.assertTrue(task.interrupted)
        self.assertEqual(dict(task.tools), {"Agent": 1, "mcp__acme_internal__lookup": 1, "Read": 1})

    def test_resumed_session_does_not_double_count(self):
        ids = [c.request_id for t in self.tasks for c in t.calls]
        self.assertEqual(len(ids), len(set(ids)))
        rename = self.by_prompt[RENAME]
        self.assertEqual(rename.session_id, S2)
        self.assertIsNone(rename.effort)


class PricingTests(unittest.TestCase):
    prices = PriceTable.load()

    def cost(self, **fields) -> float:
        return sum(self.prices.call_cost(ApiCall(request_id="r", role="main", **fields)).values())

    def test_normalize_model(self):
        for raw in (
            "claude-sonnet-4-5-20250929",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "claude-sonnet-4-5@20250929",
            "claude-sonnet-4-5[1m]",
            "CLAUDE-SONNET-4-5",
        ):
            self.assertEqual(normalize_model(raw), "claude-sonnet-4-5", raw)

    def test_cost_by_category(self):
        call = ApiCall("r", "claude-opus-5-5", "main", input_fresh=10, cache_write_1h=1000, cache_read=5000,
                       output=300, thinking=200)
        parts = self.prices.call_cost(call)
        expected = {"input": 40, "cache_write": 8000, "cache_read": 1000, "output": 2000, "thinking": 4000,
                    "web_search": 0}
        for name, micro_usd in expected.items():
            self.assertAlmostEqual(parts[name], micro_usd / 1e6, msg=name)

    def test_haiku_long_context_rates_apply_to_the_whole_request(self):
        small = self.cost(model="claude-haiku-5-5", input_fresh=1000, cache_read=49_000, output=1000)
        large = self.cost(model="claude-haiku-5-5", input_fresh=1000, cache_read=150_000, output=1000)
        self.assertAlmostEqual(small, (1000 * 0.1 + 49_000 * 0.01 + 1000 * 0.5) / 1e6)
        self.assertAlmostEqual(large, (1000 * 0.5 + 150_000 * 0.05 + 1000 * 2.5) / 1e6)

    def test_fast_mode_and_us_inference(self):
        base = self.cost(model="claude-opus-5-5", input_fresh=1000, cache_read=1000)
        self.assertAlmostEqual(self.cost(model="claude-opus-5-5", input_fresh=1000, cache_read=1000, speed="fast"), 2 * base)
        self.assertAlmostEqual(self.cost(model="claude-opus-5-5", input_fresh=1000, cache_read=1000, inference_geo="us"),
                               1.1 * base)

    def test_unpriced_model_or_tier(self):
        self.assertIsNone(self.prices.call_cost(ApiCall("r", "some-other-model", "main", output=10)))
        self.assertIsNone(self.prices.call_cost(ApiCall("r", "claude-sonnet-4-6", "main", output=10, speed="fast")))

    def test_web_search_fee(self):
        self.assertAlmostEqual(self.cost(model="claude-opus-5-5", web_search_requests=3), 0.03)

    def test_percentile(self):
        self.assertEqual(percentile([3, 1, 2], 50), 2)
        self.assertAlmostEqual(percentile([0, 10], 90), 9)
        self.assertIsNone(percentile([], 50))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = str(build_projects(self.root))
        self.env = mock.patch.dict(os.environ, {"TOKENCOUNT_HOME": str(self.root / "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def export(self, *extra: str) -> list[dict]:
        target = self.root / "export.jsonl"
        code, _, _ = self.run_cli("export", "--path", self.projects, "-o", str(target), *extra)
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli("validate", str(target))[0], 0)
        self.exported_text = target.read_text(encoding="utf-8")
        return [json.loads(line) for line in self.exported_text.splitlines()]

    def test_report_json_has_totals_and_no_prompt_text(self):
        code, out, _ = self.run_cli("report", "--path", self.projects, "--json")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual((summary["tasks"], summary["sessions"]), (4, 2))
        self.assertAlmostEqual(summary["total_cost"], (COST_CSV + COST_BUILD + COST_README + COST_RENAME) / 1e6)
        self.assertEqual(summary["unpriced_calls"], {})
        self.assertNotIn("CSV", out)

    def test_report_text(self):
        code, out, _ = self.run_cli("report", "--path", self.projects)
        self.assertEqual(code, 0)
        self.assertIn("Where the money went", out)
        self.assertIn(CSV, out)
        self.assertNotIn(CSV, self.run_cli("report", "--path", self.projects, "--no-prompts")[1])

    def test_export_is_deidentified_and_valid(self):
        records = self.export()
        self.assertEqual(len(records), 4)
        for secret in (CSV, "secret-project", "/home/dev", S1, S2, "acme", "req_A", "toolu_", "u1"):
            self.assertNotIn(secret, self.exported_text)
        build = next(r for r in records if r["subagent_calls"] == 2)
        self.assertEqual(build["tools"], {"Agent": 1, "Read": 1, "mcp": 1})
        self.assertEqual(build["outcome"], {"label": "unknown", "interrupted": True})
        self.assertEqual([c["role"] for c in build["calls"]], ["main", "subagent", "main", "subagent"])
        self.assertEqual(build["task"]["prompt_chars"], len(BUILD))

    def test_record_ids_are_stable_across_exports(self):
        first = [r["record_id"] for r in self.export()]
        self.assertEqual(first, [r["record_id"] for r in self.export()])

    def test_label_then_export_only_labeled(self):
        salt = load_salt()
        rid = next(record_id(t, salt) for t in load_tasks([Path(self.projects)]) if t.prompt_text == CSV)
        code, _, _ = self.run_cli("label", rid[:6], "--path", self.projects, "--outcome", "success",
                                  "--category", "feature", "--description", "add CSV export to a web app")
        self.assertEqual(code, 0)
        records = self.export("--only-labeled")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_id"], rid)
        self.assertEqual(records[0]["outcome"]["label"], "success")
        self.assertEqual(records[0]["task"]["category"], "feature")
        self.assertIsNone(records[0]["task"]["prompt"])

    def test_prompt_text_only_when_opted_in(self):
        records = self.export("--with-prompt")
        self.assertIn(CSV, [r["task"]["prompt"] for r in records])

    def test_validate_reports_bad_records(self):
        record = self.export()[0]
        record.pop("calls")
        record["outcome"]["label"] = "great"
        record["extra"] = 1
        errors = " | ".join(validate_record(record))
        for fragment in ("'calls'", "'great'", "'extra'"):
            self.assertIn(fragment, errors)


if __name__ == "__main__":
    unittest.main()
