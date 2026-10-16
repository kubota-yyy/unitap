import argparse
import unittest
from unittest.mock import patch

from unitap_pkg.execution_history import build_execution_history_entry, extract_command_context_from_args, summarize_result_payload


class ExecutionHistoryTests(unittest.TestCase):
    def test_nonzero_exit_cannot_be_recorded_as_success(self):
        with patch("unitap_pkg.execution_history.get_execution_history_path", return_value="unused"):
            entry = build_execution_history_entry(project_path="/project", args=argparse.Namespace(command="wait_result"),
                response={"ok": True, "result": {"done": False, "reason": "timeout", "polls": 3, "elapsedSeconds": 2}},
                started_at_epoch=1000, completed_at_epoch=1002, exit_code=1)
        self.assertFalse(entry["ok"])
        self.assertEqual("system_exit", entry["errorCode"])
        self.assertEqual("timeout", entry["resultSummary"]["reason"])
        self.assertFalse(entry["resultSummary"]["done"])

    def test_wait_context_records_markers_and_start_requirements_without_contents(self):
        context = extract_command_context_from_args(argparse.Namespace(command="wait_result", done="/qa/done",
            progress="/qa/progress", after=1000, start_timeout=10, timeout=60))
        self.assertEqual("/qa/done", context["requestParams"]["done"])
        self.assertEqual(10, context["requestParams"]["startTimeout"])
        self.assertEqual(1000, context["requestParams"]["after"])

    def test_launch_context_records_restart_arguments(self):
        args = argparse.Namespace(
            command="launch",
            restart=True,
            force_restart=False,
            no_kill=False,
            kill_project_only=True,
            no_wait=False,
            wait_timeout=300,
            ignore_compiler_errors=False,
            no_ignore_compiler_errors=False,
        )

        context = extract_command_context_from_args(args)

        self.assertEqual("launch", context["command"])
        self.assertEqual(
            {
                "restart": True,
                "forceRestart": False,
                "noKill": False,
                "killProjectOnly": True,
                "killAll": False,
                "noWait": False,
                "waitTimeout": 300,
                "ignoreCompilerErrors": False,
                "noIgnoreCompilerErrors": False,
            },
            context["requestParams"],
        )

    def test_result_summary_keeps_unitap_success_and_pending_fields(self):
        summary = summarize_result_payload(
            {
                "success": False,
                "rawSuccess": True,
                "unitapSuccess": False,
                "pending": True,
                "qualityGateFailed": True,
                "qualityGateReasons": ["runtime_errors"],
            }
        )

        self.assertEqual(
            {
                "qualityGateFailed": True,
                "qualityGateReasons": ["runtime_errors"],
                "rawSuccess": True,
                "success": False,
                "pending": True,
                "unitapSuccess": False,
            },
            summary,
        )


if __name__ == "__main__":
    unittest.main()
