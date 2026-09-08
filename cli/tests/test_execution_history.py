import argparse
import unittest

from unitap_pkg.execution_history import extract_command_context_from_args, summarize_result_payload


class ExecutionHistoryTests(unittest.TestCase):
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
