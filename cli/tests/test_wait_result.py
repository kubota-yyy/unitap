import os
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from unitap_pkg import commands
from unitap_pkg.commands import wait_for_result_file


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_args(tmp, **overrides):
    values = dict(done=os.path.join(tmp, "done.txt"), fail=os.path.join(tmp, "failure.txt"), progress=None, stall=0,
                  require_playing=False, ignore_errors=False, timeout=30.0, poll_interval=1.0)
    values.update(overrides)
    return SimpleNamespace(**values)


def playing(value=True):
    return lambda: {"ok": True, "result": {"isPlaying": value, "isCompiling": False, "isUpdating": False}}


def no_errors(since):
    return {"ok": True, "result": {"entries": []}}


class WaitResultTests(unittest.TestCase):
    def run_wait(self, args, status=None, console=None, clock=None):
        clock = clock or FakeClock()
        return wait_for_result_file(args, 0, status_fn=status or playing(), console_fn=console or no_errors,
                                    now_fn=clock.time, sleep_fn=clock.sleep, monotonic_fn=clock.time)

    def test_done_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp)
            open(args.done, "w").write("ok")
            result = self.run_wait(args)
            self.assertTrue(result["done"])
            self.assertEqual(result["reason"], "done")

    def test_failure_file_returns_its_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp)
            open(args.fail, "w").write("System.Exception: boom")
            result = self.run_wait(args)
            self.assertEqual(result["reason"], "failure_file")
            self.assertIn("boom", result["content"])

    def test_console_error_stops_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_wait(make_args(tmp), console=lambda since: {
                "ok": True, "result": {"entries": [{"type": "Exception", "message": "NullReferenceException", "stackTrace": "at X"}]}})
            self.assertEqual(result["reason"], "console_error")
            self.assertEqual(result["polls"], 1)

    def test_ignore_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp, ignore_errors=True, timeout=3)
            result = self.run_wait(args, console=lambda since: self.fail("console should not be read"))
            self.assertEqual(result["reason"], "timeout")

    def test_play_mode_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            states = iter([True, True, False])
            status = lambda: {"ok": True, "result": {"isPlaying": next(states), "isCompiling": False}}
            result = self.run_wait(make_args(tmp, require_playing=True), status=status)
            self.assertEqual(result["reason"], "play_mode_exited")
            self.assertEqual(result["polls"], 3)

    def test_compiling_is_not_play_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = lambda: {"ok": True, "result": {"isPlaying": False, "isCompiling": True}}
            result = self.run_wait(make_args(tmp, require_playing=True, timeout=3), status=status)
            self.assertEqual(result["reason"], "timeout")

    def test_stalled_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = os.path.join(tmp, "progress.txt")
            open(progress, "w").write("start\nwaiting for coins\n")
            clock = FakeClock()
            os.utime(progress, (clock.now, clock.now))
            result = self.run_wait(make_args(tmp, progress=progress, stall=5), clock=clock)
            self.assertEqual(result["reason"], "stalled")
            self.assertEqual(result["lastProgress"], "waiting for coins")

    def test_timeout_is_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_wait(make_args(tmp, timeout=3))
            self.assertEqual(result["reason"], "timeout")
            self.assertFalse(result["done"])

    def test_stale_markers_do_not_complete_a_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp, after=1000, timeout=3)
            for path in (args.done, args.fail):
                with open(path, "w") as f:
                    f.write("previous run")
                os.utime(path, (999, 999))
            result = self.run_wait(args)
            self.assertEqual("timeout", result["reason"])

    def test_fresh_failure_takes_precedence_over_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp)
            for path in (args.done, args.fail):
                with open(path, "w") as f:
                    f.write("marker")
            self.assertEqual("failure_file", self.run_wait(args)["reason"])

    def test_old_failure_does_not_hide_fresh_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp, after=1000)
            for path, stamp in ((args.fail, 999), (args.done, 1001)):
                with open(path, "w") as f:
                    f.write("marker")
                os.utime(path, (stamp, stamp))
            self.assertTrue(self.run_wait(args)["done"])

    def test_old_progress_is_not_start_evidence_and_deadline_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = os.path.join(tmp, "progress.txt")
            with open(progress, "w") as f:
                f.write("previous run")
            os.utime(progress, (999, 999))
            args = make_args(tmp, after=1000, progress=progress, start_timeout=4, poll_interval=30)
            result = self.run_wait(args)
            self.assertEqual("not_started", result["reason"])
            self.assertEqual(4, result["elapsedSeconds"])
            self.assertFalse(result["progressStarted"])

    def test_fresh_progress_then_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            args = make_args(tmp, after=clock.now, progress=os.path.join(tmp, "progress.txt"), start_timeout=2)

            def sleep(seconds):
                clock.sleep(seconds)
                path = args.progress if clock.now == 1001 else args.done
                with open(path, "w") as f:
                    f.write("current run")
                os.utime(path, (clock.now, clock.now))

            result = wait_for_result_file(args, 0, status_fn=playing(), console_fn=no_errors,
                                         now_fn=clock.time, monotonic_fn=clock.time, sleep_fn=sleep)
            self.assertTrue(result["done"])
            self.assertTrue(result["progressStarted"])

    def test_wall_clock_changes_do_not_extend_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            result = wait_for_result_file(make_args(tmp, timeout=3, poll_interval=10), 0,
                                         status_fn=playing(), console_fn=no_errors,
                                         now_fn=lambda: 100000 - clock.now, monotonic_fn=clock.time, sleep_fn=clock.sleep)
            self.assertEqual("timeout", result["reason"])
            self.assertEqual(3, result["elapsedSeconds"])

    def test_console_query_includes_errors_since_qa_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(commands, "send_unitap_sync", return_value={"ok": True, "result": {}}) as send:
                clock = FakeClock()
                wait_for_result_file(make_args(tmp, after=900, timeout=1), 0, status_fn=playing(),
                                     now_fn=clock.time, monotonic_fn=clock.time, sleep_fn=clock.sleep)
            self.assertEqual("1970-01-01T00:15:00+00:00", send.call_args.args[3]["since"])

    def test_failed_wait_has_false_json_ok_and_nonzero_exit(self):
        for reason in ("timeout", "not_started", "stalled", "failure_file", "console_error", "play_mode_exited"):
            for json_output in (True, False):
                with self.subTest(reason=reason, json=json_output):
                    args = SimpleNamespace(json=json_output)
                    result = {"done": False, "reason": reason, "polls": 1}
                    output = io.StringIO()
                    with patch.object(commands, "wait_for_result_file", return_value=result), \
                            redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                        commands.do_wait_result(args, 0)
                    self.assertEqual(1, raised.exception.code)
                    self.assertFalse(args._last_unitap_response["ok"])
                    self.assertEqual("wait_result_" + reason, args._last_unitap_response["error"]["code"])
                    self.assertEqual(result, args._last_unitap_response["result"])
                    if json_output:
                        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_failed_fsm_wait_also_reports_failure(self):
        args = SimpleNamespace(json=True, gameObject="cat", fsmName="fsm", state="ready", timeout=1, poll_interval=0.2)
        with patch.object(commands, "wait_for_fsm_state", return_value={"matched": False}), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            commands.do_wait_fsm(args, 0)
        self.assertEqual("wait_fsm_timeout", args._last_unitap_response["error"]["code"])


if __name__ == "__main__":
    unittest.main()
