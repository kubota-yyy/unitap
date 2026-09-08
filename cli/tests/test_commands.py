import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import commands, transport


class CommandTests(unittest.TestCase):
    def test_compile_recovery_accepts_null_result_and_file_transport(self):
        rejected = {"ok": False, "result": None, "error": {"code": "precondition_failed"}}
        completed = {"ok": True, "result": {"status": "completed"}, "error": None}
        with patch.object(transport, "send_with_retry", side_effect=[rejected, {"ok": True}, completed]) as send:
            with patch.object(transport, "wait_for_connection", return_value={"transportKind": "file", "port": None}):
                result = transport.poll_async_job("localhost", 0, "compile_check", {}, 1000, "/project")
        self.assertIs(result, completed)
        self.assertEqual([call.args[2]["command"] for call in send.call_args_list], ["compile_check", "stop", "compile_check"])

    def test_completed_async_job_accepts_null_error(self):
        completed = {"ok": True, "result": {"status": "completed"}, "error": None}
        with patch.object(transport, "send_with_retry", return_value=completed):
            self.assertIs(transport.poll_async_job("localhost", 0, "compile_check", {}, 1000, "/project"), completed)

    def test_json_error_response_exits_nonzero(self):
        args = argparse.Namespace(json=True, _wait_meta=None)
        resp = {
            "ok": False,
            "error": {
                "code": "unitap_error",
                "message": "failed",
            },
        }

        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()) as stdout:
                commands.print_unitap_response(args, resp)

        self.assertEqual(1, raised.exception.code)
        self.assertIn('"ok": false', stdout.getvalue())
        self.assertIs(args._last_unitap_response, resp)

    def test_compile_check_exits_nonzero_when_compiler_errors_exist(self):
        args = argparse.Namespace(
            project=None,
            timeout=90000,
            max_retries=0,
            focus_unity=False,
            auto_focus_on_stall=False,
            json=True,
            _wait_meta=None,
        )

        with patch.object(
            commands,
            "poll_async_job",
            return_value={
                "ok": True,
                "result": {
                    "compiled": True,
                    "compileStarted": True,
                    "hasErrors": True,
                    "errorCount": 1,
                    "errors": ["Assets/Broken.cs(1,1): error CS1002"],
                    "timedOut": False,
                    "isCompiling": False,
                    "isUpdating": False,
                },
            },
        ):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    commands.do_compile_check(args, 12345)

        self.assertEqual(1, raised.exception.code)
        self.assertFalse(args._last_unitap_response["ok"])
        self.assertEqual("compile_errors", args._last_unitap_response["error"]["code"])

    def test_pending_tool_exec_response_is_not_reported_as_success(self):
        response = {
            "ok": True,
            "result": {
                "_mcp_status": "pending",
                "success": True,
            },
        }

        marked = commands._mark_pending_tool_exec_response(response)

        self.assertTrue(marked["ok"])
        self.assertTrue(marked["result"]["pending"])
        self.assertTrue(marked["result"]["rawSuccess"])
        self.assertFalse(marked["result"]["success"])

    def test_capture_ignores_existing_stale_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "capture.png"
            output_path.write_bytes(b"stale image")
            args = argparse.Namespace(
                project=None,
                output=str(output_path),
                superSize=1,
                timeout=0.01,
                _wait_meta=None,
            )

            with patch.object(
                commands,
                "send_with_retry",
                return_value={"ok": True, "result": {"requested": True}},
            ):
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()) as stdout:
                        commands.do_capture(args, 12345)

            payload = json.loads(stdout.getvalue())
            self.assertEqual(1, raised.exception.code)
            self.assertFalse(payload["success"])
            self.assertEqual("File write timeout", payload["error"])
            self.assertFalse(args._last_unitap_response["ok"])
            self.assertEqual("capture_file_timeout", args._last_unitap_response["error"]["code"])
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
