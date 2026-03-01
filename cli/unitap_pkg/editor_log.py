import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from .constants import (
    COMPILER_MESSAGE_PATTERN,
    EDITOR_LOG_MAX_AGE_SECONDS,
    EDITOR_LOG_TAIL_BYTES,
    POLL_INTERVAL,
    SCRIPT_COMPILATION_PROGRESS_PATTERN,
    SCRIPT_COMPILATION_RESULT_PATTERN,
    SCRIPT_COMPILATION_START_PATTERN,
)
from .project import ProjectResolutionError, resolve_project_root


def find_project_root(project_path: str | None = None) -> Path | None:
    """Unity プロジェクトルートを特定する"""
    try:
        return resolve_project_root(project_path, allow_process_discovery=False)
    except ProjectResolutionError:
        return None


def _normalize_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser())


def read_compile_errors(project_path: str | None = None) -> list[dict] | None:
    """Library/Unitap/compile-errors.json を直接読む（TCP不要）"""
    root = find_project_root(project_path)
    if not root:
        return None
    path = root / "Library" / "Unitap" / "compile-errors.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("entries", [])
    except (json.JSONDecodeError, OSError):
        return None


def get_editor_log_candidates(project_path: str | None = None) -> list[Path]:
    """Editor.log の候補パスを返す（優先順）"""
    candidates: list[Path] = []

    env_path = os.environ.get("UNITY_EDITOR_LOG_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    root = find_project_root(project_path)
    if root:
        candidates.append(root / "Logs" / "Editor.log")
        candidates.append(root / "Library" / "Editor.log")

    system = platform.system().lower()
    home = Path.home()
    if system == "darwin":
        candidates.append(home / "Library" / "Logs" / "Unity" / "Editor.log")
    elif system == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Unity" / "Editor" / "Editor.log")
    else:
        candidates.append(home / ".config" / "unity3d" / "Editor.log")
        candidates.append(home / ".local" / "share" / "unity3d" / "Editor.log")

    deduped: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def find_editor_log(project_path: str | None = None) -> Path | None:
    for path in get_editor_log_candidates(project_path):
        if path.exists() and path.is_file():
            return path
    return None


def read_text_tail(path: Path, max_bytes: int = EDITOR_LOG_TAIL_BYTES) -> str | None:
    """大きなログでも末尾のみ読む"""
    try:
        with path.open("rb") as f:
            size = path.stat().st_size
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def parse_compiler_entries(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    seen: set[tuple] = set()

    for line in lines:
        stripped = line.strip()
        m = COMPILER_MESSAGE_PATTERN.match(stripped)
        if not m:
            continue

        level = m.group("level").lower()
        item = {
            "file": m.group("file"),
            "line": int(m.group("line")),
            "column": int(m.group("col")),
            "level": level,
            "code": m.group("code"),
            "message": m.group("msg"),
            "raw": stripped,
        }
        key = (item["file"], item["line"], item["column"], item["level"], item["code"], item["message"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(item)

    return entries


def find_latest_compile_session(lines: list[str]) -> dict:
    """Editor.log から最新コンパイルセッションの範囲を特定する"""
    if not lines:
        return {"start": 0, "end": -1, "state": "unknown"}

    sessions: list[dict] = []
    current_start: int | None = None

    for idx, line in enumerate(lines):
        if current_start is None and (
            SCRIPT_COMPILATION_START_PATTERN.match(line) or SCRIPT_COMPILATION_PROGRESS_PATTERN.match(line)
        ):
            current_start = idx

        result_match = SCRIPT_COMPILATION_RESULT_PATTERN.match(line)
        if result_match:
            result = result_match.group("result").lower()
            start = current_start if current_start is not None and current_start <= idx else 0
            sessions.append({"start": start, "end": idx, "state": result})
            current_start = None

    if current_start is not None:
        sessions.append({"start": current_start, "end": len(lines) - 1, "state": "running"})

    if sessions:
        return sessions[-1]

    return {"start": 0, "end": len(lines) - 1, "state": "unknown"}


PROJECT_PATH_ARG_PATTERN = re.compile(
    r"(?:^|\s)(?:-projectPath|--projectPath)(?:\s+|=)(?P<value>\"[^\"]+\"|'[^']+'|[^\s]+)",
    re.IGNORECASE,
)


def parse_project_paths_from_editor_log(lines: list[str]) -> list[str]:
    detected: list[str] = []
    seen: set[str] = set()

    # 末尾側に最新起動情報がある想定。大きなログでは探索範囲を制限する。
    window = lines[-8000:]
    for line in window:
        for match in PROJECT_PATH_ARG_PATTERN.finditer(line):
            raw = match.group("value").strip().strip("\"'")
            normalized = _normalize_path(raw)
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            detected.append(normalized)

    return detected


def parse_editor_log_snapshot(project_path: str | None = None) -> dict | None:
    """Editor.log からコンパイル状態とエラーを抽出する"""
    log_path = find_editor_log(project_path)
    if not log_path:
        return None

    text = read_text_tail(log_path)
    if text is None:
        return None

    lines = text.splitlines()
    detected_project_paths = parse_project_paths_from_editor_log(lines)
    session = find_latest_compile_session(lines)
    start = max(session["start"], 0)
    end = session["end"]
    if session["state"] == "unknown":
        # セッション境界が取れない場合はログ末尾のみを対象にして、古いエラー混入を抑える
        window = lines[-4000:]
    else:
        window = lines[start:end + 1] if end >= start else []
    entries = parse_compiler_entries(window)
    errors = [e for e in entries if e["level"] == "error"]
    warnings = [e for e in entries if e["level"] == "warning"]

    log_age = None
    try:
        log_age = max(0.0, time.time() - log_path.stat().st_mtime)
    except OSError:
        pass

    is_compiling = session["state"] == "running"
    if is_compiling and log_age is not None and log_age > EDITOR_LOG_MAX_AGE_SECONDS:
        # ログが古い場合は「実行中」判定を無効化（古いログ断片の誤検知回避）
        is_compiling = False

    return {
        "source": "editor_log",
        "logPath": str(log_path),
        "logAgeSeconds": log_age,
        "detectedProjectPaths": detected_project_paths,
        "sessionState": session["state"],
        "isCompiling": is_compiling,
        "entries": entries,
        "errors": errors,
        "warnings": warnings,
        "hasErrors": len(errors) > 0,
        "errorCount": len(errors),
        "warningCount": len(warnings),
    }


def format_compile_entry_message(entry: dict) -> str:
    location = ""
    if entry.get("file"):
        location = f"{entry.get('file')}({entry.get('line', 0)},{entry.get('column', 0)}): "
    return f"{location}{entry.get('level', 'error')} {entry.get('code', '')}: {entry.get('message', '')}".strip()


def print_cli_result(args, result: dict):
    if args.json:
        print(json.dumps({"ok": True, "result": result}, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def wait_for_editor_log_idle(project_path: str | None, timeout_ms: int) -> tuple[dict | None, bool, int]:
    """Editor.log だけで idle を待機する"""
    started = time.time()
    deadline = started + max(timeout_ms, 0) / 1000.0

    while True:
        snapshot = parse_editor_log_snapshot(project_path)
        if snapshot is None:
            elapsed_ms = int((time.time() - started) * 1000)
            return None, True, elapsed_ms

        if not snapshot.get("isCompiling"):
            elapsed_ms = int((time.time() - started) * 1000)
            return snapshot, False, elapsed_ms

        if time.time() >= deadline:
            elapsed_ms = int((time.time() - started) * 1000)
            return snapshot, True, elapsed_ms

        time.sleep(POLL_INTERVAL)


def _fallback_source(snapshot: dict) -> dict:
    """フォールバック結果に共通で付与するソース情報"""
    return {
        "source": "editor_log_fallback",
        "logPath": snapshot.get("logPath", ""),
        "logAgeSeconds": snapshot.get("logAgeSeconds"),
        "detectedProjectPaths": snapshot.get("detectedProjectPaths", []),
    }


def _fallback_heartbeat(args, snapshot: dict) -> bool:
    root = find_project_root(args.project) or Path.cwd()
    hb = {
        "pid": 0, "port": 0, "projectPath": str(root),
        "projectName": "", "unityVersion": "", "lastHeartbeat": "",
        "isCompiling": snapshot.get("isCompiling", False),
        "isPlaying": False, "hasErrors": snapshot.get("hasErrors", False),
        "errorCount": snapshot.get("errorCount", 0), "fresh": False,
        **_fallback_source(snapshot),
    }
    print(json.dumps(hb, indent=2, ensure_ascii=False))
    sys.exit(1)


def _fallback_status(args, snapshot: dict) -> bool:
    result = {
        "isPlaying": False, "isCompiling": snapshot.get("isCompiling", False),
        "isUpdating": snapshot.get("isCompiling", False), "isPaused": False,
        "activeScene": "", "unityVersion": "", "platform": "",
        "hasErrors": snapshot.get("hasErrors", False),
        "errorCount": snapshot.get("errorCount", 0),
        "warningCount": snapshot.get("warningCount", 0),
        "loadedSceneCount": 0, "timeSinceStartup": 0,
        "compileSessionState": snapshot.get("sessionState", "unknown"),
        **_fallback_source(snapshot),
    }
    print_cli_result(args, result)
    return True


def _fallback_read_console(args, snapshot: dict) -> bool:
    type_filter = (args.type or "").lower()
    selected: list[dict] = []
    for e in snapshot.get("entries", []):
        if type_filter == "error" and e.get("level") != "error":
            continue
        if type_filter == "warning" and e.get("level") != "warning":
            continue
        if type_filter == "log":
            continue
        selected.append(e)
        if len(selected) >= args.limit:
            break
    entries = [
        {"message": format_compile_entry_message(e), "stackTrace": "",
         "type": "Error" if e.get("level") == "error" else "Warning", "timestamp": ""}
        for e in selected
    ]
    result = {
        "entries": entries,
        "count": len(entries),
        "since": getattr(args, "since", None),
        "sinceLastClearApplied": False,
        **_fallback_source(snapshot),
    }
    print_cli_result(args, result)
    return True


def _format_error_list(entries: list[dict]) -> list[dict]:
    return [{"file": e.get("file", ""), "line": e.get("line", 0), "column": e.get("column", 0),
             "code": e.get("code", ""), "message": e.get("message", "")} for e in entries]


def _fallback_wait_idle(args, snapshot: dict) -> bool:
    waited_snapshot, timed_out, elapsed_ms = wait_for_editor_log_idle(args.project, args.timeout)
    if waited_snapshot is None:
        return False
    is_compiling = waited_snapshot.get("isCompiling", False)
    is_updating = bool(is_compiling)
    result = {
        "idle": (not is_compiling) and (not is_updating),
        "isCompiling": is_compiling,
        "isUpdating": is_updating,
        "timedOut": timed_out,
        "elapsedMs": elapsed_ms,
        "status": "completed",
        "hasErrors": waited_snapshot.get("hasErrors", False),
        "errorCount": waited_snapshot.get("errorCount", 0),
        "warningCount": waited_snapshot.get("warningCount", 0),
        **_fallback_source(waited_snapshot),
    }
    print_cli_result(args, result)
    return True


def _fallback_compile_check(args, snapshot: dict) -> bool:
    waited_snapshot, timed_out, elapsed_ms = wait_for_editor_log_idle(args.project, args.timeout)
    if waited_snapshot is None:
        return False
    is_compiling = waited_snapshot.get("isCompiling", False)
    is_updating = bool(is_compiling)
    errors = _format_error_list(waited_snapshot.get("errors", []))
    warnings = _format_error_list(waited_snapshot.get("warnings", []))
    result = {
        "compiled": (not timed_out) and (not is_compiling) and (not is_updating),
        "idle": (not is_compiling) and (not is_updating),
        "isCompiling": is_compiling,
        "isUpdating": is_updating,
        "hasErrors": len(errors) > 0,
        "errors": errors, "warnings": warnings,
        "errorCount": len(errors), "warningCount": len(warnings),
        "elapsedMs": elapsed_ms, "timedOut": timed_out, "status": "completed",
        **_fallback_source(waited_snapshot),
    }
    print_cli_result(args, result)
    return True


def _fallback_diagnose(args, snapshot: dict) -> bool:
    issues = []
    if snapshot.get("isCompiling"):
        issues.append({"issue": "compiling",
                        "detail": "Unity is currently compiling scripts (detected via Editor.log)",
                        "next_actions": ["wait_idle", "read_console --type error"]})
    if snapshot.get("errorCount", 0) > 0:
        issues.append({"issue": "compile_errors",
                        "detail": f"{snapshot.get('errorCount', 0)} errors in latest compilation",
                        "next_actions": ["read_console --type error", "compile_check"]})
    result = {
        "status": "issues_found" if issues else "ok",
        "detail": f"{len(issues)} issue(s) detected" if issues else "No issues detected from Editor.log",
        "issues": issues,
        "next_actions": ["status"] if issues else [],
        **_fallback_source(snapshot),
    }
    print_cli_result(args, result)
    return True


def _is_recent_snapshot(snapshot: dict) -> bool:
    age = snapshot.get("logAgeSeconds")
    if not isinstance(age, (int, float)):
        return False
    return age <= EDITOR_LOG_MAX_AGE_SECONDS


def _is_same_path(a: str | Path | None, b: str | Path | None) -> bool:
    na = _normalize_path(a)
    nb = _normalize_path(b)
    return bool(na and nb and na == nb)


def _snapshot_matches_project(snapshot: dict, project_root: Path | None) -> bool:
    if project_root is None:
        return True

    normalized_root = _normalize_path(project_root)
    if not normalized_root:
        return False

    log_path = _normalize_path(snapshot.get("logPath"))
    if log_path:
        expected_logs = {
            _normalize_path(Path(normalized_root) / "Logs" / "Editor.log"),
            _normalize_path(Path(normalized_root) / "Library" / "Editor.log"),
        }
        if log_path in expected_logs:
            return True

    detected_paths = snapshot.get("detectedProjectPaths", [])
    if isinstance(detected_paths, list) and detected_paths:
        for path in detected_paths:
            if _is_same_path(path, normalized_root):
                return True
        return False

    # 判定不能なグローバルログは採用しない（誤判定防止を優先）
    return False


_FALLBACK_HANDLERS: dict[str, callable] = {
    "heartbeat": _fallback_heartbeat,
    "status": _fallback_status,
    "read_console": _fallback_read_console,
    "wait_idle": _fallback_wait_idle,
    "compile_check": _fallback_compile_check,
    "diagnose": _fallback_diagnose,
}


def try_editor_log_fallback(args, reason: str) -> bool:
    """heartbeat/TCP 不可時の Editor.log フォールバック"""
    handler = _FALLBACK_HANDLERS.get(args.command)
    if not handler:
        return False

    snapshot = parse_editor_log_snapshot(args.project)
    if snapshot is None:
        return False

    if not _is_recent_snapshot(snapshot):
        return False

    project_root = find_project_root(args.project)
    if not _snapshot_matches_project(snapshot, project_root):
        return False

    print(
        f"Unitap fallback: {reason}. Using Editor.log ({snapshot.get('logPath', 'unknown')})",
        file=sys.stderr,
    )
    return handler(args, snapshot)
