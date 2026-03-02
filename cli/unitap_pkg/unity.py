import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path


def build_unity_launch_command(
    editor_path: Path,
    project_root: Path,
    *,
    ignore_compiler_errors: bool = False,
) -> list[str]:
    cmd = [str(editor_path), "-projectPath", str(project_root)]
    if ignore_compiler_errors:
        cmd.append("-ignoreCompilerErrors")
    return cmd


def get_unity_version(project_root: Path) -> str | None:
    """ProjectVersion.txt から Unity バージョンを取得"""
    version_file = project_root / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.exists():
        return None
    try:
        for line in version_file.read_text().splitlines():
            if line.startswith("m_EditorVersion:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def get_unity_editor_path(version: str) -> Path | None:
    """Unity Editor の実行ファイルパスを返す"""
    system = platform.system().lower()
    if system == "darwin":
        path = Path(f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity")
    elif system == "windows":
        path = Path(f"C:/Program Files/Unity/Hub/Editor/{version}/Editor/Unity.exe")
    else:
        path = Path(f"/opt/unity/editors/{version}/Editor/Unity")
    return path if path.exists() else None


def list_installed_unity_versions() -> list[str]:
    """インストール済み Unity バージョン一覧を返す"""
    system = platform.system().lower()
    if system == "darwin":
        hub_dir = Path("/Applications/Unity/Hub/Editor")
    elif system == "windows":
        hub_dir = Path("C:/Program Files/Unity/Hub/Editor")
    else:
        hub_dir = Path("/opt/unity/editors")
    if not hub_dir.exists():
        return []
    return sorted([d.name for d in hub_dir.iterdir() if d.is_dir()])


def _normalize_path_for_match(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def _normalize_path_for_compare(path: str | Path | None) -> str | None:
    normalized = _normalize_path_for_match(path)
    if normalized is None:
        return None

    normalized = normalized.rstrip("/\\")
    if platform.system().lower() == "windows":
        normalized = normalized.lower()
    return normalized


def _extract_project_path_from_command(command: str) -> str | None:
    try:
        args = shlex.split(command)
    except ValueError:
        return None

    project_flags = ("-projectPath", "--projectPath", "-projectpath", "--projectpath")

    for idx, token in enumerate(args):
        if token in project_flags:
            if idx + 1 < len(args):
                return args[idx + 1]
            continue

        for flag in project_flags:
            prefix = f"{flag}="
            if token.startswith(prefix):
                value = token[len(prefix):]
                if value:
                    return value

    return None


def _is_unity_editor_command(command: str) -> bool:
    line = command.lower()
    if "unityhub" in line or "assetimportworker" in line:
        return False
    if "unitycrashhandler" in line or "unityshadercompiler" in line:
        return False
    if "unityhelper" in line:
        return False
    if "unity.app/contents/macos/unity" in line:
        return True
    if "/editor/unity" in line:
        return True
    if "unity.exe" in line:
        return True
    return False


def _is_same_project_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False

    na = _normalize_path_for_compare(a)
    nb = _normalize_path_for_compare(b)
    if na and nb and na == nb:
        return True

    return False


def _command_matches_project(command: str, project_root: Path) -> bool:
    project_root_raw = str(project_root)
    project_root_norm = _normalize_path_for_match(project_root_raw)

    # 先に文字列一致で高速に判定し、必要時のみ projectPath 引数を解釈する。
    if project_root_raw in command:
        return True
    if project_root_norm and project_root_norm in command:
        return True

    command_project = _extract_project_path_from_command(command)
    if _is_same_project_path(command_project, project_root_raw):
        return True
    if _is_same_project_path(command_project, project_root_norm):
        return True
    return False


def _list_unity_processes(project_root: Path | None = None) -> list[tuple[int, str]]:
    import subprocess

    processes: list[tuple[int, str]] = []
    try:
        result = subprocess.run(
            ["ps", "axww", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return processes

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue

        pid_str, command = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue

        if not _is_unity_editor_command(command):
            continue
        if project_root and not _command_matches_project(command, project_root):
            continue

        processes.append((pid, command))

    return processes


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # 権限エラーでも PID が存在する場合がある
        return True


def is_unity_process_running(project_root: Path | None = None) -> bool:
    return bool(_list_unity_processes(project_root))


def list_unity_processes(project_root: Path | None = None) -> list[dict]:
    entries: list[dict] = []
    for pid, command in _list_unity_processes(project_root):
        entries.append({
            "pid": pid,
            "command": command,
            "projectPath": _extract_project_path_from_command(command),
        })
    return entries


def clean_recovery_files(project_root: Path) -> list[str]:
    """Recovery Scene Backups ダイアログを防ぐためバックアップを削除"""
    import shutil
    targets = [
        project_root / "Assets" / "_Recovery",
        project_root / "Assets" / "_Recovery.meta",
        project_root / "Library" / "Backup",
    ]

    # Unity のバージョン/OS 差でケースや接尾辞が揺れるため複数パターンを掃除する
    temp_dir = project_root / "Temp"
    for pattern in ("__Backupscenes", "__BackupScenes", "__Backupscenes*", "__BackupScenes*"):
        targets.extend(temp_dir.glob(pattern))

    removed = []
    seen: set[str] = set()
    for t in targets:
        key = str(t)
        if key in seen:
            continue
        seen.add(key)
        if t.exists():
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
            else:
                try:
                    t.unlink()
                except OSError:
                    pass
            removed.append(str(t))
    return removed


def kill_unity_processes(project_root: Path | None = None) -> list[int]:
    """Unity プロセスを終了させる。project_root 指定時はそのプロジェクトのみ対象"""
    import signal

    targets = _list_unity_processes(project_root)
    if not targets:
        return []

    terminated: list[int] = []
    remaining: set[int] = set()

    for pid, _ in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
            remaining.add(pid)
        except ProcessLookupError:
            continue
        except OSError:
            # 権限等で kill できない PID は残存扱いにする
            remaining.add(pid)

    deadline = time.time() + 8.0
    while remaining and time.time() < deadline:
        remaining = {pid for pid in remaining if _process_exists(pid)}
        if remaining:
            time.sleep(0.2)

    # SIGTERM で落ちない Unity を強制終了
    if remaining:
        for pid in list(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
                terminated.append(pid)
            except ProcessLookupError:
                pass
            except OSError:
                pass

        kill_deadline = time.time() + 3.0
        while remaining and time.time() < kill_deadline:
            remaining = {pid for pid in remaining if _process_exists(pid)}
            if remaining:
                time.sleep(0.2)

    # 出力は重複なし・安定順にする
    return sorted(set(terminated))


def _run_osascript(script: str, timeout_seconds: float = 3.0) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript timeout"
    except OSError as ex:
        return False, f"osascript execution failed: {ex}"

    if result.returncode == 0:
        return True, (result.stdout or "").strip()

    detail = (result.stderr or result.stdout or "").strip()
    if not detail:
        detail = f"osascript exit code {result.returncode}"
    return False, detail


def _frontmost_process_pid() -> tuple[int | None, str | None]:
    ok, out = _run_osascript(
        'tell application "System Events" to get unix id of first application process whose frontmost is true'
    )
    if not ok:
        return None, out
    value = (out or "").strip()
    if not value:
        return None, "frontmost pid is empty"
    try:
        return int(value), None
    except ValueError:
        return None, f"frontmost pid parse failed: {value}"


def _frontmost_process_name() -> tuple[str | None, str | None]:
    ok, out = _run_osascript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    if not ok:
        return None, out
    name = (out or "").strip()
    if not name:
        return None, "frontmost process name is empty"
    return name, None


def focus_unity_editor(
    project_root: Path | None = None,
    *,
    verify_frontmost: bool = True,
    log_failures: bool = False,
) -> bool:
    """Unity Editor を前面化する（macOSのみ）。"""
    if platform.system().lower() != "darwin":
        if log_failures:
            print("[unitap] Unity focus is not supported on this OS.", file=sys.stderr)
        return False

    target_pid = None
    targets = _list_unity_processes(project_root)
    if targets:
        target_pid = targets[0][0]

    scripts: list[str] = []
    if target_pid is not None:
        scripts.append(
            f'tell application "System Events" to set frontmost of (first process whose unix id is {target_pid}) to true'
        )
    scripts.append('tell application "Unity" to activate')

    failure_reasons: list[str] = []
    for script in scripts:
        ok, detail = _run_osascript(script)
        if not ok:
            failure_reasons.append(detail or "unknown AppleScript failure")
            continue

        if not verify_frontmost:
            time.sleep(0.2)
            return True

        if target_pid is not None:
            front_pid, err = _frontmost_process_pid()
            if front_pid == target_pid:
                time.sleep(0.2)
                return True
            if err:
                failure_reasons.append(f"frontmost pid check failed: {err}")
            else:
                failure_reasons.append(
                    f"frontmost pid mismatch (expected={target_pid}, actual={front_pid})"
                )
            continue

        front_name, err = _frontmost_process_name()
        if front_name and "unity" in front_name.lower():
            time.sleep(0.2)
            return True
        if err:
            failure_reasons.append(f"frontmost process check failed: {err}")
        else:
            failure_reasons.append(
                f"frontmost process is not Unity (actual={front_name or 'unknown'})"
            )

    if log_failures:
        if target_pid is None:
            failure_reasons.insert(0, "Unity process not found for this project")
        message = "; ".join(failure_reasons[-5:]) if failure_reasons else "unknown reason"
        print(f"[unitap] Unity focus failed: {message}", file=sys.stderr)
    return False
