import os
import platform
import json
import re
import signal
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def get_expected_build_target(project_root: Path) -> str | None:
    """Project-local Unitap build target guard.

    UNITAP_EXPECTED_BUILD_TARGET overrides ProjectSettings/UnitapSettings.json.
    """
    override = os.environ.get("UNITAP_EXPECTED_BUILD_TARGET")
    if override and override.strip():
        return override.strip()

    settings_path = project_root / "ProjectSettings" / "UnitapSettings.json"
    if not settings_path.exists():
        return None

    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    target = payload.get("expectedBuildTarget") if isinstance(payload, dict) else None
    if isinstance(target, str) and target.strip():
        return target.strip()
    return None


def build_unity_launch_command(
    editor_path: Path,
    project_root: Path,
    *,
    ignore_compiler_errors: bool = True,
    build_target: str | None = None,
) -> list[str]:
    cmd = [str(editor_path), "-projectPath", str(project_root)]
    if build_target:
        cmd.extend(["-buildTarget", build_target])
    if ignore_compiler_errors:
        cmd.append("-ignoreCompilerErrors")
    return cmd


def build_unity_launch_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("RWD_SUPPRESS_EDITOR_WELCOME_DIALOGS", "1")
    return env


# pgrep -f で既存ウォッチャーを見つけるための識別子。スクリプト先頭に埋め込む。
_STARTUP_DIALOG_DISMISS_MARKER = "unitap:startup-dialog-dismiss"

_SAFE_MODE_DIALOG_MATCHER = r'''
on isSafeModeDialog(dialogText, buttonNames)
    if buttonNames does not contain "Ignore" then return false
    if buttonNames contains "Enter Safe Mode" then return true
    return dialogText contains "Enter Safe Mode?"
end isSafeModeDialog
'''

_KNOWN_DIALOG_HANDLERS = _SAFE_MODE_DIALOG_MATCHER + r'''
on jsonString(valueText)
    set escapedText to ""
    repeat with ch in characters of (valueText as text)
        set c to ch as text
        if c is "\"" or c is "\\" then
            set escapedText to escapedText & "\\" & c
        else if c is linefeed then
            set escapedText to escapedText & "\\n"
        else if c is return then
            set escapedText to escapedText & "\\r"
        else if c is tab then
            set escapedText to escapedText & "\\t"
        else if (id of c) < 32 then
            set escapedText to escapedText & " "
        else
            set escapedText to escapedText & c
        end if
    end repeat
    return "\"" & escapedText & "\""
end jsonString

on dialogRecord(kind, editorPid, dialogText, buttonName, errorText)
    return "{" & "\"status\":" & my jsonString(kind) & ",\"pid\":" & editorPid & ",\"text\":" & my jsonString(dialogText) & ",\"button\":" & my jsonString(buttonName) & ",\"message\":" & my jsonString(errorText) & "}" & linefeed
end dialogRecord

on clickButton(editorPid, j, buttonName)
    with timeout of 5 seconds
        tell application "System Events" to click button buttonName of window j of (first process whose unix id is editorPid)
    end timeout
end clickButton

on handleWindow(editorPid, j)
    with timeout of 5 seconds
      tell application "System Events"
        set wName to name of window j of (first process whose unix id is editorPid)
        set buttonNames to {}
        try
            set buttonNames to name of every button of window j of (first process whose unix id is editorPid)
        end try
        set textValues to {}
        try
            set textValues to value of every static text of window j of (first process whose unix id is editorPid)
        end try
      end tell
    end timeout
    if wName is missing value then set wName to ""
    set dialogText to wName
    repeat with textValue in textValues
        try
            set dialogText to dialogText & " " & (textValue as text)
        end try
    end repeat
    set buttonName to ""
    if my isSafeModeDialog(dialogText, buttonNames) then
        set buttonName to "Ignore"
        set dialogText to "Enter Safe Mode?"
    end if
    if wName contains "Corrupted Library Detected" and buttonNames contains "Rebuild Library" then
        set buttonName to "Rebuild Library"
    end if
    if buttonNames contains "Reload" and buttonNames contains "Ignore" then
        if dialogText contains "open scene" or dialogText contains "open scene(s)" or dialogText contains "changed on disk" then
            set buttonName to "Reload"
        end if
    end if
    if buttonNames contains "Cancel" and buttonNames contains "Open manual" then
        if dialogText contains "UI Toolkit Particles" then
            set buttonName to "Cancel"
        end if
    end if
    if buttonName is not "" then
        try
            my clickButton(editorPid, j, buttonName)
            return my dialogRecord("dismissed", editorPid, dialogText, buttonName, "")
        on error errorText
            return my dialogRecord("blocked", editorPid, dialogText, buttonName, errorText)
        end try
    end if
    if (count of buttonNames) > 0 then return my dialogRecord("remaining", editorPid, dialogText, "", "Unrecognized window; no button clicked")
    return ""
end handleWindow

on scanEditors()
    set dialogRows to ""
    with timeout of 5 seconds
        tell application "System Events" to set unityPids to unix id of (every process whose name is "Unity")
    end timeout
    repeat with pidItem in unityPids
        set editorPid to pidItem as integer
        try
            with timeout of 5 seconds
                tell application "System Events" to set winCount to count of windows of (first process whose unix id is editorPid)
            end timeout
            repeat with j from winCount to 1 by -1
                try
                    set dialogRows to dialogRows & my handleWindow(editorPid, j)
                end try
            end repeat
        end try
    end repeat
    return dialogRows
end scanEditors
'''

_DISMISS_KNOWN_DIALOGS_SCRIPT = _KNOWN_DIALOG_HANDLERS + r'''
on run argv
    return my scanEditors()
end run
'''

_STARTUP_DIALOG_DISMISS_SCRIPT = (
    f"-- {_STARTUP_DIALOG_DISMISS_MARKER}\n" + _KNOWN_DIALOG_HANDLERS + r'''
on run argv
    set timeoutSeconds to 90
    if (count of argv) is greater than 0 then
        try
            set timeoutSeconds to (item 1 of argv) as integer
        end try
    end if

    set deadline to (current date) + timeoutSeconds
    repeat while (current date) < deadline
        try
            with timeout of 5 seconds
                do shell script "touch " & quoted form of (item 2 of argv)
            end timeout
            my scanEditors()
        end try
        delay 0.5
    end repeat
end run
''')


def _dialog_watch_beat_path() -> Path:
    return Path(tempfile.gettempdir()) / f"unitap-dialog-watch-{os.getuid()}.beat"


def _startup_dialog_dismisser_running() -> bool:
    """生存だけでなく巡回の進行を確認し、停止した旧版の監視も置き換える。"""
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", _STARTUP_DIALOG_DISMISS_MARKER],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    try:
        if time.time() - _dialog_watch_beat_path().stat().st_mtime <= 20:
            return True
    except OSError:
        pass
    for value in result.stdout.split():
        if value.isdigit() and int(value) != os.getpid():
            try:
                os.kill(int(value), signal.SIGTERM)
            except ProcessLookupError:
                pass
    return False


def dismiss_startup_dialogs_async(timeout_seconds: int = 90) -> None:
    """Unity の既知ダイアログをバックグラウンドで処理する.

    - "Enter Safe Mode?" は Ignore
    - "Corrupted Library Detected" は Rebuild Library
    - 外部変更された scene reload 確認は Reload

    macOS 限定. Accessibility 権限がない場合は何もしない (silent fail).
    既にウォッチャーが動いている間は起動しない (多重起動防止).
    """
    if platform.system().lower() != "darwin":
        return
    # unitap は agent から短時間に何度も呼ばれる。呼ばれるたびに最長 30 分生きる
    # osascript を切り離すと数千個溜まって System Events とメモリを食い潰すので、
    # 既存ウォッチャーが生きている間はスキップする。
    try:
        import fcntl
        beat_path = _dialog_watch_beat_path()
        # 短命のCLIが同時に来ても、再起動と起動直後の猶予を1か所で管理する。
        with beat_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _startup_dialog_dismisser_running():
                return
            beat_path.touch()
            subprocess.Popen(
                ["osascript", "-e", _STARTUP_DIALOG_DISMISS_SCRIPT, "--", str(int(timeout_seconds)), str(beat_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError:
        # osascript 不在等は致命ではないので無視する
        pass


def dismiss_known_dialogs_once() -> dict:
    """全Editorを1回だけ走査する。既知のボタン以外は押さない。"""
    result = {"dismissedDialogs": [], "remainingDialogs": []}
    if platform.system().lower() != "darwin":
        return result
    try:
        completed = subprocess.run(
            ["osascript", "-e", _DISMISS_KNOWN_DIALOGS_SCRIPT],
            capture_output=True, text=True, timeout=15,
        )
        if completed.returncode:
            raise ValueError(completed.stderr.strip() or f"osascript exited {completed.returncode}")
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record["status"] == "dismissed":
                result["dismissedDialogs"].append(f"{record['text']} -> {record['button']}")
            else:
                result["remainingDialogs"].append(record)
        if any(item["status"] == "blocked" for item in result["remainingDialogs"]):
            result["ok"] = False
            result["error"] = {
                "code": "editor_dialog_blocking",
                "message": "A known Editor dialog could not be clicked; stopped waiting.",
                "details": {"dialogs": result["remainingDialogs"]},
            }
        else:
            result["ok"] = True
        if "Enter Safe Mode? -> Ignore" in result["dismissedDialogs"]:
            result["hint"] = "コンパイルエラーがある。compile_check / read_console で直す。"
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as ex:
        result["scanError"] = str(ex)
        result["ok"] = False
        result["error"] = {"code": "editor_dialog_scan_failed", "message": str(ex)}
    return result


class EditorDialogBlocking(Exception):
    def __init__(self, args, response):
        self.args_namespace = args
        self.response = response
        super().__init__(response["error"]["message"])


def check_known_dialogs(args) -> None:
    """待機ループから5秒ごとに同期確認し、結果をコマンドのJSONへ引き継ぐ。"""
    if getattr(args, "no_ignore_compiler_errors", False):
        return
    now = time.monotonic()
    last = getattr(args, "_dialog_checked_at", None)
    if last is not None and now - last < 5:
        return
    result = dismiss_known_dialogs_once()
    args._dialog_checked_at = time.monotonic()
    previous = getattr(args, "_dialog_result", {})
    result["dismissedDialogs"] = list(dict.fromkeys(
        previous.get("dismissedDialogs", []) + result.get("dismissedDialogs", [])))
    if previous.get("hint"):
        result.setdefault("hint", previous["hint"])
    args._dialog_result = result
    if result.get("error", {}).get("code") == "editor_dialog_blocking":
        raise EditorDialogBlocking(args, merge_dialog_result(args, result))


def merge_dialog_result(args, response: dict) -> dict:
    dialogs = getattr(args, "_dialog_result", {})
    metadata = {key: dialogs[key] for key in ("dismissedDialogs", "hint", "scanError") if dialogs.get(key)}
    if not metadata:
        return response
    response = dict(response)
    response.update(metadata)
    if isinstance(response.get("result"), dict):
        response["result"] = {**response["result"], **metadata}
    return response


def dismiss_safe_mode_dialog_async(timeout_seconds: int = 90) -> None:
    """後方互換用。現在は起動時ダイアログ全般を処理する。"""
    dismiss_startup_dialogs_async(timeout_seconds)


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


_PROJECT_FLAGS = ("-projectPath", "--projectPath", "-projectpath", "--projectpath")


def _extract_project_path_from_command(command: str) -> str | None:
    # ps は引数を quote しないため、空白を含むパスでは shlex が分割してしまう。
    # まず shlex で読み、実在しなければ「次の -flag まで」を 1 つのパスとして読む。
    shlex_value = None
    try:
        args = shlex.split(command)
    except ValueError:
        args = []

    for idx, token in enumerate(args):
        if token in _PROJECT_FLAGS:
            if idx + 1 < len(args):
                shlex_value = args[idx + 1]
                break
            continue
        for flag in _PROJECT_FLAGS:
            prefix = f"{flag}="
            if token.startswith(prefix) and token[len(prefix):]:
                shlex_value = token[len(prefix):]
                break
        if shlex_value:
            break

    if shlex_value and Path(shlex_value).exists():
        return shlex_value

    match = re.search(r"(?:^|\s)--?projectpath[= ](?P<path>.+?)(?=\s+-[A-Za-z]|$)", command, re.IGNORECASE)
    if match:
        spaced_value = match.group("path").strip().strip("\"'")
        if spaced_value and Path(spaced_value).exists():
            return spaced_value

    return shlex_value


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
    # 部分文字列一致は使わない。/x/game と /x/game--clone のような兄弟プロジェクトを
    # 同一視すると、別 Editor を kill / focus してしまうため。
    command_project = _extract_project_path_from_command(command)
    return _is_same_project_path(command_project, project_root)


class UnityProcessListError(RuntimeError):
    """ps で Editor 一覧を取れなかった (「起動していない」と区別するため)。"""


_PS_TIMEOUTS_SECONDS = (5.0, 20.0)


def _read_process_table() -> str | None:
    # Editor を何台も動かしている高負荷時は ps が 5 秒を超えることがある。
    # 空の一覧として扱うと quit が「起動していない」と誤って成功するため、長めに再試行する。
    for timeout in _PS_TIMEOUTS_SECONDS:
        try:
            result = subprocess.run(
                ["ps", "axww", "-o", "pid=", "-o", "command="],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return None
        if result.returncode == 0:
            return result.stdout
    return None


def _list_unity_processes(project_root: Path | None = None, strict: bool = False) -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    table = _read_process_table()
    if table is None:
        if strict:
            raise UnityProcessListError("Could not read the process table (ps failed or timed out).")
        return processes

    for line in table.splitlines():
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


def list_unity_processes(project_root: Path | None = None, strict: bool = False) -> list[dict]:
    entries: list[dict] = []
    for pid, command in _list_unity_processes(project_root, strict=strict):
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


def kill_unity_processes(project_root: Path | None = None, strict: bool = False) -> list[int]:
    """Unity プロセスを終了させる。project_root 指定時はそのプロジェクトのみ対象"""
    import signal

    targets = _list_unity_processes(project_root, strict=strict)
    if not targets:
        return []

    terminated: list[int] = []
    remaining: set[int] = set()
    quit_timeout = float(os.environ.get("UNITAP_UNITY_QUIT_TIMEOUT_SECONDS", "60"))
    term_timeout = float(os.environ.get("UNITAP_UNITY_TERM_TIMEOUT_SECONDS", "20"))

    can_quit_by_app = True
    if project_root is not None:
        # 一覧を取れない時に「全部このプロジェクト」と見なすと、アプリ全体の quit で他の Editor まで閉じる
        try:
            can_quit_by_app = len(_list_unity_processes(strict=True)) == len(targets)
        except UnityProcessListError:
            can_quit_by_app = False

    if platform.system().lower() == "darwin" and can_quit_by_app:
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Unity" to quit'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            remaining = {pid for pid, _ in targets}
            deadline = time.time() + max(0.0, quit_timeout)
            while remaining and time.time() < deadline:
                remaining = {pid for pid in remaining if _process_exists(pid)}
                if remaining:
                    time.sleep(0.5)
            for pid, _ in targets:
                if pid not in remaining:
                    terminated.append(pid)
        except (subprocess.TimeoutExpired, OSError):
            remaining = {pid for pid, _ in targets}
    else:
        remaining = {pid for pid, _ in targets}

    for pid, _ in targets:
        if pid not in remaining:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            if pid not in terminated:
                terminated.append(pid)
            remaining.add(pid)
        except ProcessLookupError:
            continue
        except OSError:
            # 権限等で kill できない PID は残存扱いにする
            remaining.add(pid)

    deadline = time.time() + max(0.0, term_timeout)
    while remaining and time.time() < deadline:
        remaining = {pid for pid in remaining if _process_exists(pid)}
        if remaining:
            time.sleep(0.2)

    # SIGTERM で落ちない Unity を強制終了
    if remaining:
        for pid in list(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
                if pid not in terminated:
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
    elif project_root is not None:
        # 対象プロジェクトの Editor が見つからない時に `tell application "Unity"` へ
        # フォールバックすると、別プロジェクトの Editor を前面化してしまう。
        if log_failures:
            print("[unitap] Unity focus failed: Unity process not found for this project", file=sys.stderr)
        return False
    elif len(_list_unity_processes()) == 1:
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


def get_process_rss_mb(pid: int) -> float | None:
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    value = (result.stdout or "").strip()
    if not value:
        return None
    try:
        return round(int(value) / 1024.0, 1)
    except ValueError:
        return None


def parse_elapsed_seconds(value: str) -> int | None:
    """ps の etime ([[dd-]hh:]mm:ss) を秒にする。"""
    value = (value or "").strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        day_part, value = value.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    try:
        fields = [int(x) for x in value.split(":")]
    except ValueError:
        return None
    if not 1 <= len(fields) <= 3:
        return None
    seconds = 0
    for field in fields:
        seconds = seconds * 60 + field
    return days * 86400 + seconds


def get_process_elapsed_seconds(pid: int) -> int | None:
    env = dict(os.environ, LC_ALL="C")
    try:
        result = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=5, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return parse_elapsed_seconds(result.stdout)
