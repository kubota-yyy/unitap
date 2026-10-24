import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import cli, editor_slots as slots, session_commands, unity


def memory(gib=48, free=50, errors=None):
    return {"physicalBytes": gib * 1024 ** 3, "freePercent": free, "minFreePercent": 15,
            "swap": {"totalBytes": 42 * 1024 ** 3, "usedBytes": 41 * 1024 ** 3,
                     "freeBytes": 1024 ** 3}, "errors": errors or []}


def editor(pid=1, project="/other"):
    return {"pid": pid, "projectPath": project, "command": "/x/Editor/Unity -projectPath " + project}


class EditorSlotTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(slots, "_slot_directory", return_value=directory))
        stack.enter_context(patch.dict(os.environ))
        os.environ.pop("UNITAP_MAX_EDITORS", None)
        self.memory = stack.enter_context(patch.object(slots, "read_memory_status", return_value=memory()))
        self.processes = stack.enter_context(patch.object(slots, "list_unity_processes", return_value=[]))
        self.alive = stack.enter_context(patch.object(slots, "_process_exists", return_value=True))
        self.clock = [0.0]
        stack.enter_context(patch.object(slots.time, "monotonic", side_effect=lambda: self.clock[0]))
        stack.enter_context(patch.object(slots.time, "sleep", side_effect=self.sleep))

    def sleep(self, seconds):
        self.clock[0] += seconds

    def test_default_limit_and_override(self):
        for gib, expected in ((4, 1), (16, 1), (48, 4), (128, 10)):
            with self.subTest(gib=gib):
                self.memory.return_value = memory(gib)
                self.assertEqual(expected, slots.editor_slot_snapshot()["maxEditors"])
        os.environ["UNITAP_MAX_EDITORS"] = "3"
        self.assertEqual(3, slots.editor_slot_snapshot()["maxEditors"])

    def test_invalid_override_blocks_instead_of_disabling_guard(self):
        for value in ("0", "-1", "abc", "1.5", ""):
            with self.subTest(value=value):
                os.environ["UNITAP_MAX_EDITORS"] = value
                self.assertFalse(slots.editor_slot_snapshot()["available"])

    def test_count_limit_and_exact_own_project_exclusion(self):
        os.environ["UNITAP_MAX_EDITORS"] = "2"
        self.processes.return_value = [editor(1, "/game"), editor(2, "/game--clone")]
        self.assertFalse(slots.editor_slot_snapshot()["available"])
        snapshot = slots.editor_slot_snapshot(Path("/game"))
        self.assertTrue(snapshot["available"])
        self.assertEqual(2, snapshot["currentCount"])
        self.assertEqual(1, snapshot["countedEditors"])

    def test_memory_boundary_and_swap_diagnostics(self):
        self.memory.return_value = memory(free=15)
        self.assertTrue(slots.editor_slot_snapshot()["available"])
        self.memory.return_value = memory(free=14.9)
        snapshot = slots.editor_slot_snapshot()
        self.assertFalse(snapshot["available"])
        self.assertEqual(41 * 1024 ** 3, snapshot["memory"]["swap"]["usedBytes"])

    def test_unknown_memory_and_failed_process_probe_block(self):
        self.memory.return_value = memory(free=None, errors=["Could not read freePercent on macOS."])
        self.assertFalse(slots.editor_slot_snapshot()["available"])
        self.memory.return_value = memory()
        self.processes.side_effect = unity.UnityProcessListError("ps unavailable")
        self.assertIn("ps unavailable", slots.editor_slot_snapshot()["reasons"])

    def test_no_wait_error_contains_full_snapshot(self):
        self.processes.return_value = [editor(pid) for pid in range(6)]
        with self.assertRaises(slots.EditorSlotUnavailable) as raised:
            with slots.editor_slot(Path("/game"), wait=False):
                self.fail("must not launch")
        error = raised.exception
        self.assertEqual("editor_slot_unavailable", error.code)
        self.assertEqual(6, error.details["currentCount"])
        self.assertEqual(6, len(error.details["editors"]))
        self.assertIn("swap", error.details["memory"])
        self.assertEqual(0, self.clock[0])

    def test_wait_then_admit_when_an_editor_exits(self):
        self.processes.side_effect = [[editor(pid) for pid in range(6)], []]
        with redirect_stderr(io.StringIO()) as stderr:
            with slots.editor_slot(Path("/game")):
                pass
        self.assertEqual(5, self.clock[0])
        self.assertEqual(1, len(stderr.getvalue().splitlines()))

    def test_wait_for_memory_recovery(self):
        self.memory.side_effect = [memory(free=10), memory(free=20)]
        with redirect_stderr(io.StringIO()):
            with slots.editor_slot(Path("/game")):
                pass
        self.assertEqual(5, self.clock[0])

    def test_timeout_and_minute_notices_use_stderr(self):
        self.processes.return_value = [editor(pid) for pid in range(6)]
        with redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(slots.EditorSlotUnavailable) as raised:
                with slots.editor_slot(Path("/game"), timeout_s=125.25):
                    self.fail("must not launch")
        self.assertEqual(125.25, raised.exception.details["waitedSeconds"])
        self.assertEqual(3, len(stderr.getvalue().splitlines()))
        self.assertEqual("", stdout.getvalue())

    def test_invalid_timeout(self):
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(slots.EditorSlotUnavailable):
                with slots.editor_slot(Path("/game"), timeout_s=timeout):
                    self.fail("must not launch")

    def test_machine_lock_serializes_check_and_spawn(self):
        with slots.editor_slot(Path("/one"), wait=False):
            with self.assertRaises(slots.EditorSlotUnavailable) as raised:
                with slots.editor_slot(Path("/two"), wait=False):
                    self.fail("second launch must wait")
            self.assertIn("Another session", str(raised.exception))
        with slots.editor_slot(Path("/two"), wait=False):
            pass

    def test_pending_spawn_holds_slot_until_visible_or_dead(self):
        os.environ["UNITAP_MAX_EDITORS"] = "1"
        with slots.editor_slot(Path("/one"), wait=False) as reservation:
            reservation.started(123)
        snapshot = slots.editor_slot_snapshot()
        self.assertEqual(1, snapshot["currentCount"])
        self.assertFalse(snapshot["available"])
        self.processes.return_value = [editor(123, "/one")]
        self.assertEqual(1, slots.editor_slot_snapshot()["currentCount"])
        self.processes.return_value = []
        self.alive.return_value = False
        self.assertTrue(slots.editor_slot_snapshot()["available"])

    def test_lock_released_after_launch_failure(self):
        with self.assertRaises(RuntimeError):
            with slots.editor_slot(Path("/one"), wait=False):
                raise RuntimeError("launch failed")
        with slots.editor_slot(Path("/two"), wait=False):
            pass

    def test_editors_and_doctor_expose_snapshot_and_warnings(self):
        self.processes.return_value = [editor(pid) for pid in range(7)]
        self.memory.return_value = memory(free=10)
        args = argparse.Namespace(json=True, project=None)
        with patch.object(session_commands, "collect_editors", return_value=[]), redirect_stdout(io.StringIO()) as out:
            session_commands.do_editors(args)
        self.assertEqual(4, json.loads(out.getvalue())["result"]["editorSlots"]["maxEditors"])
        with patch.object(session_commands, "check_cli_identity", return_value={}), \
                patch.object(session_commands, "package_git_state", return_value={}), redirect_stdout(io.StringIO()) as out:
            session_commands.do_doctor(args)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(any("limit exceeded" in w for w in payload["result"]["warnings"]))
        self.assertTrue(any("Memory free" in w for w in payload["result"]["warnings"]))

    def test_parser_exposes_slot_options_and_defaults(self):
        parser, _ = cli.build_parser()
        args = parser.parse_args(["launch"])
        self.assertEqual(1800, args.wait_slot_timeout)
        self.assertFalse(args.no_wait_slot)
        args = parser.parse_args(["launch", "--no-wait-slot", "--wait-slot-timeout", "0.25"])
        self.assertTrue(args.no_wait_slot)
        self.assertEqual(0.25, args.wait_slot_timeout)


class MemoryProbeTests(unittest.TestCase):
    def test_macos_output_parsing(self):
        outputs = ["51539607552\n", "System-wide memory free percentage: 14%\n",
                   "vm.swapusage: total = 42.00G used = 41.00G free = 1024.00M (encrypted)"]
        with patch.object(slots.platform, "system", return_value="Darwin"), \
                patch.object(slots, "_read_command", side_effect=outputs) as read:
            result = slots.read_memory_status()
        self.assertEqual(48 * 1024 ** 3, result["physicalBytes"])
        self.assertEqual(14, result["freePercent"])
        self.assertEqual(41 * 1024 ** 3, result["swap"]["usedBytes"])
        self.assertEqual([], result["errors"])
        self.assertEqual(["memory_pressure"], read.call_args_list[1].args[0])

    def test_macos_probe_failures_are_explicit(self):
        with patch.object(slots.platform, "system", return_value="Darwin"), \
                patch.object(slots, "_read_command", return_value=None):
            result = slots.read_memory_status()
        self.assertIsNone(result["freePercent"])
        self.assertIsNone(result["swap"])
        self.assertEqual(3, len(result["errors"]))

    def test_import_workers_are_not_editors_but_batch_editor_is_counted(self):
        command = "/x/Unity.app/Contents/MacOS/Unity -projectPath /game"
        table = f"1 {command}\n2 {command} -batchmode -name AssetImportWorker0\n3 {command} -batchmode"
        with patch.object(unity, "_read_process_table", return_value=table):
            self.assertEqual([1, 3], [p["pid"] for p in unity.list_unity_processes(strict=True)])
