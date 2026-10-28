import json
import subprocess
import sys
import tempfile
import unittest
import argparse
import io
import os
import signal
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from unitap_pkg import cli, commands, transport, unity
from unitap_pkg.unity import _SAFE_MODE_DIALOG_MATCHER, _STARTUP_DIALOG_DISMISS_SCRIPT


@unittest.skipUnless(sys.platform == "darwin", "AppleScript requires macOS")
class SafeModeDialogTests(unittest.TestCase):
    def test_dialog_detection_with_missing_or_generic_window_titles(self):
        cases = [
            # Unity's native alert may expose no meaningful AX window title.
            ("", ["Ignore", "Quit", "Enter Safe Mode"], True),
            ("Unity", ["Ignore", "Quit", "Enter Safe Mode"], True),
            ("Unity Enter Safe Mode?", ["Ignore", "Quit"], True),
            ("Enter Safe Mode?", ["Quit", "Enter Safe Mode"], False),
            ("Scene changed on disk", ["Reload", "Ignore"], False),
            ("Save the Safe Mode scene?", ["Save", "Ignore"], False),
            ("", ["Ignore"], False),
        ]
        for text, buttons, expected in cases:
            with self.subTest(text=text, buttons=buttons):
                button_list = "{" + ", ".join(json.dumps(b) for b in buttons) + "}"
                source = (
                    _SAFE_MODE_DIALOG_MATCHER
                    + f"\nreturn isSafeModeDialog({json.dumps(text)}, {button_list})\n"
                )
                result = subprocess.run(
                    ["osascript", "-e", source],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(str(expected).lower(), result.stdout.strip())

    def test_watcher_addresses_each_editor_by_unix_id(self):
        # With several Editors open, a stored "application process "Unity"" reference resolves
        # to the first Unity, so a Safe Mode prompt in any other Editor stayed open (2026-10-17).
        script = _STARTUP_DIALOG_DISMISS_SCRIPT
        self.assertIn('unix id of (every process whose name is "Unity")', script)
        self.assertIn("(first process whose unix id is editorPid)", script)
        self.assertNotIn("repeat with p in unityProcs", script)
        self.assertNotIn("every window of p", script)

    def test_complete_watcher_script_compiles(self):
        # Compile only: never click a live Editor's dialogs during this test.
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in (("watcher", _STARTUP_DIALOG_DISMISS_SCRIPT),
                                 ("once", unity._DISMISS_KNOWN_DIALOGS_SCRIPT)):
                with self.subTest(script=name):
                    result = subprocess.run(
                        ["osacompile", "-o", str(Path(tmp) / f"{name}.scpt"), "-e", source],
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)

    def test_empty_scan_runs_without_accessing_editors(self):
        # AppleScript accepts "records" at compile time but treats it as a class at runtime.
        source = unity._KNOWN_DIALOG_HANDLERS.replace(
            'tell application "System Events" to set unityPids to unix id of (every process whose name is "Unity")',
            'set unityPids to {}',
        ) + '\nreturn scanEditors()\n'
        result = subprocess.run(["osascript", "-e", source],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_dialog_record_escapes_json_without_accessing_editors(self):
        source = (unity._KNOWN_DIALOG_HANDLERS
                  + '\nreturn dialogRecord("remaining", 12, "Save \\"scene\\"?" & linefeed & "next", "", "")\n')
        result = subprocess.run(["osascript", "-e", source],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('Save "scene"?\nnext', json.loads(result.stdout)["text"])


class DialogWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.beat = Path(self.tmp.name) / "watch.beat"
        for target, value in (("platform.system", "Darwin"),
                              ("_dialog_watch_beat_path", self.beat)):
            mocker = patch("unitap_pkg.unity." + target, return_value=value)
            mocker.start()
            self.addCleanup(mocker.stop)

    def run_watcher(self):
        with patch.object(unity.subprocess, "run", return_value=Mock(returncode=0, stdout="1235\n")), \
             patch.object(unity.subprocess, "Popen") as start, \
             patch.object(unity.os, "kill") as kill:
            unity.dismiss_startup_dialogs_async()
        return start, kill

    def test_old_beat_restarts_stalled_watcher(self):
        self.beat.touch()
        os.utime(self.beat, (1, 1))
        start, kill = self.run_watcher()
        kill.assert_called_once_with(1235, signal.SIGTERM)
        start.assert_called_once()
        self.assertEqual(str(self.beat), start.call_args.args[0][-1])

    def test_missing_beat_replaces_legacy_watcher(self):
        start, kill = self.run_watcher()
        kill.assert_called_once_with(1235, signal.SIGTERM)
        start.assert_called_once()

    def test_fresh_beat_does_not_restart_watcher(self):
        self.beat.touch()
        start, kill = self.run_watcher()
        start.assert_not_called()
        kill.assert_not_called()

    def test_fresh_beat_without_process_starts_watcher(self):
        self.beat.touch()
        with patch.object(unity.subprocess, "run", return_value=Mock(returncode=1)), \
             patch.object(unity.subprocess, "Popen") as start:
            unity.dismiss_startup_dialogs_async()
        start.assert_called_once()


class DismissDialogsTests(unittest.TestCase):
    def run_once(self, records):
        output = "\n".join(json.dumps(record) for record in records)
        with patch.object(unity.platform, "system", return_value="Darwin"), \
             patch.object(unity.subprocess, "run", return_value=Mock(returncode=0, stdout=output)) as run:
            result = unity.dismiss_known_dialogs_once()
        self.assertEqual(15, run.call_args.kwargs["timeout"])
        return result

    def test_once_imports_dismissed_and_remaining_dialogs(self):
        result = self.run_once([
            {"status": "dismissed", "pid": 27970, "text": "Enter Safe Mode?", "button": "Ignore"},
            {"status": "remaining", "pid": 2, "text": 'Save "scene"?\nUnsaved', "button": ""},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(["Enter Safe Mode? -> Ignore"], result["dismissedDialogs"])
        self.assertIn("compile_check / read_console", result["hint"])
        self.assertEqual(2, result["remainingDialogs"][0]["pid"])

    def test_click_failure_has_blocking_error_and_dialog_details(self):
        record = {"status": "blocked", "pid": 27970, "text": "Enter Safe Mode?",
                  "button": "Ignore", "message": "AppleEvent timed out"}
        result = self.run_once([record])
        self.assertFalse(result["ok"])
        self.assertEqual("editor_dialog_blocking", result["error"]["code"])
        self.assertEqual([record], result["error"]["details"]["dialogs"])

    def test_scan_timeout_is_bounded_and_reported(self):
        with patch.object(unity.platform, "system", return_value="Darwin"), \
             patch.object(unity.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 15)):
            result = unity.dismiss_known_dialogs_once()
        self.assertIn("scanError", result)
        self.assertFalse(result["ok"])
        self.assertEqual("editor_dialog_scan_failed", result["error"]["code"])

    def test_script_failure_is_not_reported_as_success(self):
        with patch.object(unity.platform, "system", return_value="Darwin"), \
             patch.object(unity.subprocess, "run", return_value=Mock(returncode=1, stderr="Access denied")):
            result = unity.dismiss_known_dialogs_once()
        self.assertFalse(result["ok"])
        self.assertEqual("Access denied", result["scanError"])

    def test_launch_can_opt_out_of_safe_mode_dismissal(self):
        with patch.object(unity, "dismiss_known_dialogs_once") as scan:
            unity.check_known_dialogs(argparse.Namespace(no_ignore_compiler_errors=True))
        scan.assert_not_called()

    def test_wait_collects_actions_in_json_and_throttles_scans(self):
        args = argparse.Namespace(json=True)
        scanned = {"ok": True, "dismissedDialogs": ["Enter Safe Mode? -> Ignore"],
                   "hint": "compile_check / read_console", "remainingDialogs": []}
        with patch.object(unity, "dismiss_known_dialogs_once", return_value=scanned) as scan:
            unity.check_known_dialogs(args)
            unity.check_known_dialogs(args)
        scan.assert_called_once()
        with redirect_stdout(io.StringIO()) as output:
            commands.print_unitap_response(args, {"ok": True, "result": {"compiled": True}})
        payload = json.loads(output.getvalue())
        self.assertEqual(scanned["dismissedDialogs"], payload["result"]["dismissedDialogs"])
        self.assertEqual(scanned["hint"], payload["hint"])

    def test_reconnect_stops_immediately_on_failed_click(self):
        args = argparse.Namespace()
        result = self.run_once([{"status": "blocked", "pid": 27970,
                                 "text": "Enter Safe Mode?", "button": "Ignore"}])
        with patch.object(unity, "dismiss_known_dialogs_once", return_value=result), \
             patch.object(transport, "find_heartbeat") as heartbeat, \
             patch.object(transport.time, "sleep") as sleep:
            with self.assertRaises(unity.EditorDialogBlocking):
                transport.wait_for_connection("/project", on_dialog_check=lambda: unity.check_known_dialogs(args))
        heartbeat.assert_not_called()
        sleep.assert_not_called()

    def test_compile_poll_checks_dialogs_between_requests(self):
        check = Mock()
        running = {"ok": True, "result": {"status": "running", "jobId": "compile"}}
        done = {"ok": True, "result": {"status": "completed"}}
        with patch.object(transport, "send_with_retry", return_value=running), \
             patch.object(transport, "send_request_to_current_transport", return_value=done) as send, \
             patch.object(transport.time, "sleep"):
            result = transport.poll_async_job("localhost", 1, "compile_check", {}, 1000, "/project",
                                               on_dialog_check=check)
        self.assertIs(result, done)
        self.assertEqual(2, check.call_count)
        self.assertLessEqual(send.call_args.kwargs["timeout_s"], 5)

    def test_compile_play_recovery_does_not_swallow_blocking_dialog(self):
        response = {"ok": False, "error": {"code": "editor_dialog_blocking", "message": "blocked"}}
        blocked = unity.EditorDialogBlocking(argparse.Namespace(), response)
        playing = {"ok": False, "error": {"code": "precondition_failed"}}
        with patch.object(transport, "_foreign_play_owner", return_value=None), \
             patch.object(transport, "send_with_retry", side_effect=[playing, blocked]), \
             patch.object(transport, "wait_for_connection") as wait:
            with self.assertRaises(unity.EditorDialogBlocking):
                transport.poll_async_job("localhost", 1, "compile_check", {}, 1000, "/project",
                                         on_dialog_check=Mock())
        wait.assert_not_called()

    def test_manual_command_reports_failed_click_without_connecting(self):
        result = self.run_once([{"status": "blocked", "pid": 27970,
                                 "text": "Enter Safe Mode?", "button": "Ignore"}])
        with patch.object(sys, "argv", ["unitap", "--json", "dismiss_dialogs"]), \
             patch.object(cli, "dismiss_known_dialogs_once", return_value=result), \
             patch.object(cli, "find_heartbeat") as heartbeat, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(1, raised.exception.code)
        self.assertEqual("editor_dialog_blocking", json.loads(output.getvalue())["error"]["code"])
        heartbeat.assert_not_called()
