import os
import tempfile
import unittest
from types import SimpleNamespace

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
                                    now_fn=clock.time, sleep_fn=clock.sleep)

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


if __name__ == "__main__":
    unittest.main()
