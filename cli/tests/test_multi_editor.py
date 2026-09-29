import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import clone, commands, editor_lock, execution_history, heartbeat, lease, transport, unity


def _make_project(root: Path) -> Path:
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir()
    (root / "Packages").mkdir()
    return root


def _launch_args(project: Path, **overrides) -> argparse.Namespace:
    values = dict(
        project=str(project), restart=False, force_restart=False, no_kill=False,
        kill_project_only=False, kill_all=False, no_wait=True, wait_timeout=10,
        no_ignore_compiler_errors=False, json=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class ProjectMatchTests(unittest.TestCase):
    def test_sibling_project_with_same_prefix_is_not_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            sibling = _make_project(Path(tmp) / "game--clone")
            command = f"/Applications/Unity/Hub/Editor/6000.3.24f1/Unity.app/Contents/MacOS/Unity -projectPath {sibling} -useHub"
            self.assertFalse(unity._command_matches_project(command, game))
            self.assertTrue(unity._command_matches_project(command, sibling))

    def test_project_path_with_spaces_is_parsed_from_unquoted_ps_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "My Game")
            command = f"/x/Unity.app/Contents/MacOS/Unity -projectpath {game} -useHub -hubIPC"
            self.assertTrue(unity._command_matches_project(command, game))

    def test_focus_does_not_fall_back_to_another_projects_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            with patch.object(unity.platform, "system", return_value="Darwin"), \
                    patch.object(unity, "_list_unity_processes", return_value=[]), \
                    patch.object(unity, "_run_osascript") as osa:
                self.assertFalse(unity.focus_unity_editor(game))
            osa.assert_not_called()


class HeartbeatOwnershipTests(unittest.TestCase):
    def _write_hb(self, project: Path, recorded_project: Path) -> None:
        hb = project / "Library" / "Unitap" / ".heartbeat.json"
        hb.parent.mkdir(parents=True)
        hb.write_text(json.dumps({
            "projectPath": str(recorded_project),
            "lastHeartbeat": datetime.now(timezone.utc).isoformat(),
            "transportKind": "file",
        }))

    def test_copied_heartbeat_pointing_to_source_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _make_project(Path(tmp) / "game")
            copied = _make_project(Path(tmp) / "game--copy")
            self._write_hb(copied, source)
            self.assertIsNone(heartbeat.find_heartbeat(str(copied)))

    def test_explicit_project_does_not_fall_back_to_cwd_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = _make_project(Path(tmp) / "target")
            other = _make_project(Path(tmp) / "other")
            self._write_hb(other, other)
            cwd = os.getcwd()
            try:
                os.chdir(other)
                self.assertIsNone(heartbeat.find_heartbeat(str(target)))
                self.assertIsNotNone(heartbeat.find_heartbeat(str(other)))
            finally:
                os.chdir(cwd)


class LaunchCoexistenceTests(unittest.TestCase):
    def _run_launch(self, args, processes_by_project, *, hb=None, frozen=False):
        def list_processes(project_root=None):
            if project_root is None:
                return [p for items in processes_by_project.values() for p in items]
            return list(processes_by_project.get(str(project_root), []))

        killed_targets = []

        def fake_kill(project_root=None):
            killed_targets.append(project_root)
            if project_root is None:
                pids = [p["pid"] for items in processes_by_project.values() for p in items]
                processes_by_project.clear()
            else:
                pids = [p["pid"] for p in processes_by_project.pop(str(project_root), [])]
            return pids

        stdout = io.StringIO()
        with patch.object(commands, "get_unity_version", return_value="6000.3.24f1"), \
                patch.object(commands, "get_unity_editor_path", return_value=Path("/bin/true")), \
                patch.object(commands, "list_unity_processes", side_effect=list_processes), \
                patch.object(commands, "kill_unity_processes", side_effect=fake_kill), \
                patch.object(commands, "clean_recovery_files", return_value=[]), \
                patch.object(commands, "find_heartbeat", return_value=hb), \
                patch.object(commands, "check_heartbeat_fresh", return_value=bool(hb)), \
                patch.object(commands, "check_heartbeat_frozen", return_value=frozen), \
                patch.object(commands, "dismiss_safe_mode_dialog_async"), \
                patch.object(commands.time, "sleep"), \
                patch("subprocess.Popen") as popen, \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            try:
                commands.do_launch(args)
                code = 0
            except SystemExit as ex:
                code = ex.code
        return json.loads(stdout.getvalue()), killed_targets, popen, code

    def test_launch_keeps_other_projects_editor_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            game = _make_project(tmp / "game")
            other = str(tmp / "other")
            procs = {other: [{"pid": 11, "projectPath": other, "command": "Unity"}]}
            payload, killed, popen, code = self._run_launch(_launch_args(game), procs)
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["launched"])
        self.assertEqual([], killed)
        self.assertEqual(1, payload["otherEditorsRunning"])
        popen.assert_called_once()

    def test_restart_skips_healthy_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp).resolve() / "game")
            procs = {str(game): [{"pid": 22, "projectPath": str(game), "command": "Unity"}]}
            payload, killed, popen, _ = self._run_launch(
                _launch_args(game, restart=True), procs, hb={"transportKind": "file"}
            )
        self.assertTrue(payload["restartSkipped"])
        self.assertEqual([], killed)
        popen.assert_not_called()

    def test_restart_kills_only_this_project_when_unresponsive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            game = _make_project(tmp / "game")
            other = str(tmp / "other")
            procs = {
                str(game): [{"pid": 22, "projectPath": str(game), "command": "Unity"}],
                other: [{"pid": 11, "projectPath": other, "command": "Unity"}],
            }
            payload, killed, popen, _ = self._run_launch(_launch_args(game, restart=True), procs, hb=None)
        self.assertEqual([game], killed)
        self.assertEqual([22], payload["killedPids"])
        popen.assert_called_once()

    def test_kill_all_is_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            game = _make_project(tmp / "game")
            other = str(tmp / "other")
            procs = {other: [{"pid": 11, "projectPath": other, "command": "Unity"}]}
            payload, killed, _, _ = self._run_launch(_launch_args(game, kill_all=True), procs)
        self.assertEqual([None], killed)
        self.assertEqual([11], payload["killedPids"])


class LeaseTests(unittest.TestCase):
    def test_foreign_lease_blocks_and_owner_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            lease.acquire_lease(game, "claude:a", ttl_seconds=60, note="QA")
            self.assertEqual(0.0, round(lease.wait_for_foreign_lease(game, "claude:a", wait=False, timeout_s=0)))
            with self.assertRaises(lease.EditorLeasedError) as raised:
                lease.wait_for_foreign_lease(game, "claude:b", wait=False, timeout_s=0)
            self.assertEqual("QA", raised.exception.details["holder"]["note"])
            with self.assertRaises(lease.EditorLeasedError):
                lease.acquire_lease(game, "claude:b", ttl_seconds=60)
            with self.assertRaises(lease.EditorLeasedError):
                lease.release_lease(game, "claude:b")
            self.assertTrue(lease.release_lease(game, "claude:a")["released"])
            lease.wait_for_foreign_lease(game, "claude:b", wait=False, timeout_s=0)

    def test_expired_lease_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            lease.acquire_lease(game, "claude:a", ttl_seconds=60)
            path = game / "Library" / "Unitap" / lease.LEASE_FILE_NAME
            data = json.loads(path.read_text())
            data["expiresAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            path.write_text(json.dumps(data))
            lease.wait_for_foreign_lease(game, "claude:b", wait=False, timeout_s=0)
            self.assertEqual("claude:b", lease.acquire_lease(game, "claude:b", ttl_seconds=5)["owner"])

    def test_session_owner_resolution_order(self):
        self.assertEqual("x", lease.resolve_session_owner({"UNITAP_SESSION": "x", "CLAUDE_CODE_SESSION_ID": "c"}))
        self.assertEqual("claude:c", lease.resolve_session_owner({"CLAUDE_CODE_SESSION_ID": "c"}))
        self.assertEqual("codex:t", lease.resolve_session_owner({"CODEX_THREAD_ID": "t"}))
        self.assertIsNone(lease.resolve_session_owner({}))


class PlayOwnerGuardTests(unittest.TestCase):
    def test_compile_check_does_not_stop_foreign_play_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            lease.record_play_owner(game, "claude:qa")
            rejected = {"ok": False, "error": {"code": "precondition_failed"}}
            with patch.object(transport, "send_with_retry", return_value=rejected) as send, \
                    patch.dict(os.environ, {"UNITAP_SESSION": "claude:other"}):
                result = transport.poll_async_job("localhost", 0, "compile_check", {}, 1000, str(game))
            self.assertEqual("play_mode_in_use", result["error"]["code"])
            self.assertEqual(["compile_check"], [c.args[2]["command"] for c in send.call_args_list])

    def test_own_play_mode_is_still_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = _make_project(Path(tmp) / "game")
            lease.record_play_owner(game, "claude:me")
            rejected = {"ok": False, "error": {"code": "precondition_failed"}}
            completed = {"ok": True, "result": {"status": "completed"}}
            with patch.object(transport, "send_with_retry", side_effect=[rejected, {"ok": True}, completed]) as send, \
                    patch.object(transport, "wait_for_connection", return_value={"transportKind": "file"}), \
                    patch.dict(os.environ, {"UNITAP_SESSION": "claude:me"}), \
                    redirect_stderr(io.StringIO()):
                transport.poll_async_job("localhost", 0, "compile_check", {}, 1000, str(game))
            self.assertEqual(["compile_check", "stop", "compile_check"], [c.args[2]["command"] for c in send.call_args_list])


class LockPolicyTests(unittest.TestCase):
    def _ns(self, tool, params):
        return argparse.Namespace(command="tool_exec", tool=tool, params=json.dumps(params))

    def test_input_tools_are_exclusive_but_polling_is_not(self):
        self.assertTrue(editor_lock.command_requires_editor_lock(self._ns("ui_pointer", {"action": "click"})))
        self.assertFalse(editor_lock.command_requires_editor_lock(self._ns("ui_pointer", {"action": "status"})))
        self.assertTrue(editor_lock.command_requires_editor_lock(self._ns("invoke_inspector_action", {})))
        self.assertTrue(editor_lock.command_requires_editor_lock(argparse.Namespace(command="quit")))


class HistoryRotationTests(unittest.TestCase):
    def test_rotates_once_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution-history.jsonl"
            path.write_text("x" * 100)
            with patch.dict(os.environ, {"UNITAP_HISTORY_MAX_BYTES": "50"}):
                execution_history.append_jsonl_entry(path, {"a": 1})
            self.assertEqual("x" * 100, (Path(tmp) / "execution-history.1.jsonl").read_text())
            self.assertEqual({"a": 1}, json.loads(path.read_text()))


class AgentMisuseHintTests(unittest.TestCase):
    def test_tool_exec_compile_check_points_to_cli_command(self):
        error = commands._tool_exec_alias_error("compile_check", {})
        self.assertEqual("tool_exec_top_level_command", error["error"]["code"])
        self.assertEqual("compile_check", error["error"]["details"]["alternative"])
        self.assertEqual("stop", commands._tool_exec_alias_error("set_play_mode", {"action": "stop"})["error"]["details"]["alternative"])

    def test_execute_menu_quit_is_redirected_to_project_quit(self):
        args = argparse.Namespace(json=True)
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            commands._check_execute_menu_blocklist(args, "File/Quit")
        payload = json.loads(stdout.getvalue())
        self.assertEqual("execute_menu_blocked", payload["error"]["code"])
        self.assertIn("unitap quit", payload["error"]["details"]["alternative"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class CloneTests(unittest.TestCase):
    def _repo_with_project(self, tmp: Path) -> tuple[Path, Path]:
        repo = tmp / "repo"
        project = _make_project(repo / "game")
        (tmp / "sharedpkg").mkdir()
        (project / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": {
            "com.example.shared": "file:../../../sharedpkg",
        }}))
        (project / "Assets" / "a.txt").write_text("a")
        (repo / ".gitignore").write_text("Library/\nAssets/Sdk/\n")
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        (project / "Assets" / "Sdk").mkdir()
        (project / "Assets" / "Sdk" / "sdk.txt").write_text("sdk")
        lib = project / "Library"
        (lib / "Unitap").mkdir(parents=True)
        (lib / "Unitap" / ".heartbeat.json").write_text("{}")
        (lib / "ArtifactDB").write_text("db")
        return repo, project

    def test_worktree_clone_resolves_relative_packages_and_resets_unitap_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            repo, project = self._repo_with_project(tmp)
            (project / "Assets" / "a.txt").write_text("changed")
            with patch.object(clone, "list_unity_processes", return_value=[]):
                result = clone.create_clone(project, label="s2", include_uncommitted=True)
                cloned = Path(result["projectPath"])
                self.assertEqual(tmp / "repo--s2" / "game", cloned)
                self.assertEqual([], result["unresolvedFilePackages"])
                self.assertEqual("changed", (cloned / "Assets" / "a.txt").read_text())
                self.assertEqual("sdk", (cloned / "Assets" / "Sdk" / "sdk.txt").read_text())
                self.assertEqual("db", (cloned / "Library" / "ArtifactDB").read_text())
                self.assertFalse((cloned / "Library" / "Unitap" / ".heartbeat.json").exists())
                self.assertEqual([str(cloned)], [c["projectPath"] for c in clone.list_clones(project)])

                with self.assertRaises(RuntimeError):
                    clone.remove_clone(project, cloned)  # uncommitted change in clone
                clone.remove_clone(project, cloned, force=True)
            self.assertFalse((tmp / "repo--s2").exists())
            self.assertEqual("changed", (project / "Assets" / "a.txt").read_text())

    def test_default_clone_is_clean_head_even_when_source_is_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            repo, project = self._repo_with_project(tmp)
            (project / "Assets" / "a.txt").write_text("changed")
            (project / "Assets" / "new.txt").write_text("untracked")
            with patch.object(clone, "list_unity_processes", return_value=[]):
                result = clone.create_clone(project, label="clean")
            cloned = Path(result["projectPath"])
            self.assertEqual("a", (cloned / "Assets" / "a.txt").read_text())
            self.assertFalse((cloned / "Assets" / "new.txt").exists())
            status = subprocess.run(["git", "-C", str(cloned), "status", "--porcelain"], capture_output=True, text=True)
            self.assertEqual("", status.stdout.strip())
            self.assertIn("cloned", result["steps"][0])

    def test_remove_refuses_non_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            _, project = self._repo_with_project(tmp)
            other = _make_project(tmp / "other")
            with self.assertRaises(PermissionError):
                clone.remove_clone(project, other)
            self.assertTrue(other.exists())

    def test_snapshot_clone_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            project = _make_project(tmp / "game")
            (project / "Assets" / "b.txt").write_text("b")
            (project / "Temp").mkdir()
            with patch.object(clone, "list_unity_processes", return_value=[]):
                result = clone.create_clone(project, label="snap", copy_library=False)
            cloned = Path(result["projectPath"])
            self.assertEqual("snapshot", result["mode"])
            self.assertEqual("b", (cloned / "Assets" / "b.txt").read_text())
            self.assertFalse((cloned / "Temp").exists())


if __name__ == "__main__":
    unittest.main()
