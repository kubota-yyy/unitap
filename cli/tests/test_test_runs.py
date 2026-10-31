import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unitap_pkg.test_runs import (
    build_test_run,
    normalize_status,
    record_test_run,
    select_test_runs,
    summarize_test_runs,
    test_runs_path,
)

CLI = Path(__file__).resolve().parents[1] / "unitap.py"


class TestRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "home" / "test-runs.jsonl"
        self.env = patch.dict(os.environ, {"UNITAP_TEST_RUNS_PATH": str(self.log)})
        self.env.start()
        os.environ.pop("UNITAP_TEST_TRIGGER", None)
        os.environ.pop("UNITAP_TEST_PROJECT", None)
        self.project = self.root / "game"
        (self.project / "Assets").mkdir(parents=True)
        (self.project / "ProjectSettings").mkdir()
        (self.project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.0f1\n")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_status_words_from_each_project_are_normalized(self):
        self.assertEqual("passed", normalize_status("done"))
        self.assertEqual("passed", normalize_status("Success"))
        self.assertEqual("cancelled", normalize_status("canceled"))
        self.assertEqual("timeout", normalize_status("cutoff"))
        self.assertIsNone(normalize_status("weird"))

    def test_clone_runs_are_recorded_under_the_source_project(self):
        clone = self.root / "game--nightly"
        (clone / "Library" / "Unitap").mkdir(parents=True)
        (clone / "Library" / "Unitap" / "clone.json").write_text(json.dumps({"source": str(self.project)}))
        entry = build_test_run(project_path=str(clone), kind="qa", suite="NEK/QA/Home", status="done", seconds=12.345)
        self.assertEqual("game", entry["project"])
        self.assertEqual(str(self.project), entry["sourcePath"])
        self.assertTrue(entry["clone"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(12.35, entry["seconds"])
        self.assertIsNotNone(entry["startedAt"])

    def test_total_is_filled_from_parts_and_failures_are_clipped(self):
        entry = build_test_run(project_path=str(self.project), kind="unit", suite=".*", status="failed",
                               passed=8, failed=2, failures=["a" * 400] * 12)
        self.assertEqual(10, entry["total"])
        self.assertEqual(10, len(entry["failures"]))
        self.assertLessEqual(len(entry["failures"][0]), 300)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            build_test_run(project_path=str(self.project), kind="qa", suite="x", status="maybe")
        with self.assertRaises(ValueError):
            build_test_run(project_path=str(self.project), kind="bad kind", suite="x", status="passed")
        with self.assertRaises(ValueError):
            build_test_run(project_path=str(self.project), kind="qa", suite="x", status="passed", trigger="sometimes")

    def test_summary_keeps_the_latest_result_per_suite(self):
        record_test_run(project_path=str(self.project), kind="qa", suite="A", status="failed",
                        completed_at="2026-10-30T01:00:00+00:00", seconds=1)
        record_test_run(project_path=str(self.project), kind="qa", suite="A", status="passed",
                        completed_at="2026-10-30T02:00:00+00:00", seconds=1)
        record_test_run(project_path=str(self.project), kind="qa", suite="B", status="failed",
                        completed_at="2026-10-30T03:00:00+00:00", seconds=1, trigger="nightly")
        rows = select_test_runs(project="game", path=self.log)
        self.assertEqual(3, len(rows))
        qa = summarize_test_runs(rows)["game"]["qa"]
        self.assertEqual(3, qa["runs"])
        self.assertEqual(2, qa["suites"])
        self.assertEqual(1, qa["passingSuites"])
        self.assertEqual(["B"], [item["suite"] for item in qa["failingSuites"]])
        self.assertEqual("nightly", qa["last"]["trigger"])

    def test_cli_record_from_result_json_then_list(self):
        summary = self.root / "summary.json"
        summary.write_text(json.dumps({"kind": "unit", "pattern": ".*", "status": "done", "passed": 830, "failed": 0,
                                       "seconds": 10.5, "failures": []}))
        env = dict(os.environ, UNITAP_ALLOW_FOREIGN_CLI="1")
        recorded = subprocess.run([sys.executable, str(CLI), "--json", "--project", str(self.project), "test_runs", "record",
                                   "--from-json", str(summary), "--trigger", "release"],
                                  capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, recorded.returncode, recorded.stdout + recorded.stderr)
        entry = json.loads(recorded.stdout)["result"]["recorded"]
        self.assertEqual(("unit", ".*", "passed", 830, "release"),
                         (entry["kind"], entry["suite"], entry["status"], entry["total"], entry["trigger"]))
        listed = subprocess.run([sys.executable, str(CLI), "--json", "--project", str(self.project), "test_runs", "list"],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
        self.assertEqual(1, json.loads(listed.stdout)["result"]["count"])
        self.assertEqual(self.log, test_runs_path())

    def test_cli_record_without_kind_fails_cleanly(self):
        env = dict(os.environ, UNITAP_ALLOW_FOREIGN_CLI="1")
        failed = subprocess.run([sys.executable, str(CLI), "--json", "--project", str(self.project), "test_runs", "record",
                                 "--suite", "x", "--status", "passed"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(1, failed.returncode)
        self.assertEqual("invalid_test_run", json.loads(failed.stdout)["error"]["code"])
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
