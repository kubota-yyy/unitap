from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


EDITOR_OPERATION_LOCK_NAME = ".editor-op.lock"
EDITOR_OPERATION_METADATA_NAME = ".editor-op.json"
EDITOR_OPERATION_POLL_SECONDS = 0.2

LOCKED_COMMANDS = {
    "capture",
    "capture_editor",
    "compile_check",
    "execute_menu",
    "launch",
    "play",
    "redo",
    "refresh",
    "run_automate_batch",
    "run_automate_test",
    "run_playmode_test",
    "save_scene",
    "stop",
    "undo",
    "wait_idle",
}

UNLOCKED_COMMANDS = {
    "diagnose",
    "heartbeat",
    "read_console",
    "status",
    "tool_list",
}

LOCKED_TOOL_NAMES = {
    "capture_editor_window",
    "capture_gameview",
    "capture_sceneview",
    "open_scene",
    "run_automate_test",
    "run_playmode_test",
    "set_component",
}

UNLOCKED_TOOL_NAMES = {
    "find_assets",
    "get_project_settings",
    "inspect_component",
    "inspect_fsm_state",
    "inspect_hierarchy",
    "validate_prefab",
}


class EditorOperationBusyError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = "editor_busy"
        self.message = message
        self.details = details or {}


def command_requires_editor_lock(args) -> bool:
    command = str(getattr(args, "command", "") or "")
    if command in UNLOCKED_COMMANDS:
        return False
    if command in LOCKED_COMMANDS:
        return True
    if command != "tool_exec":
        return False

    tool_name = str(getattr(args, "tool", "") or "")
    if tool_name in UNLOCKED_TOOL_NAMES:
        return False
    return tool_name in LOCKED_TOOL_NAMES


def build_editor_lock_metadata(args, project_root: Path) -> dict:
    metadata = {
        "pid": os.getpid(),
        "command": str(getattr(args, "command", "") or ""),
        "tool": None,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "projectPath": str(project_root),
    }
    if metadata["command"] == "tool_exec":
        tool_name = str(getattr(args, "tool", "") or "")
        metadata["tool"] = tool_name or None
    return metadata


def _editor_operation_dir(project_root: Path) -> Path:
    path = Path(project_root) / "Library" / "Unitap"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _editor_operation_lock_path(project_root: Path) -> Path:
    return _editor_operation_dir(project_root) / EDITOR_OPERATION_LOCK_NAME


def _editor_operation_metadata_path(project_root: Path) -> Path:
    return _editor_operation_dir(project_root) / EDITOR_OPERATION_METADATA_NAME


class _EditorOperationFileLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._file = None

    def __enter__(self) -> "_EditorOperationFileLock":
        self._file = self.lock_path.open("a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def try_acquire(self) -> bool:
        if self._file is None:
            raise RuntimeError("lock file is not open")

        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        import fcntl

        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def release(self) -> None:
        if self._file is None:
            return

        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return

        import fcntl

        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def read_editor_operation_metadata(project_root: Path | None) -> dict | None:
    if project_root is None:
        return None

    path = _editor_operation_metadata_path(project_root)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_editor_operation_metadata(project_root: Path, metadata: dict) -> None:
    path = _editor_operation_metadata_path(project_root)
    try:
        path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_editor_operation_metadata(project_root: Path, metadata: dict) -> None:
    path = _editor_operation_metadata_path(project_root)
    try:
        current = read_editor_operation_metadata(project_root)
        if current is not None and current != metadata:
            return
        if path.exists():
            path.unlink()
    except OSError:
        pass


def is_editor_operation_locked(project_root: Path | None) -> bool:
    if project_root is None:
        return False

    lock_path = _editor_operation_lock_path(project_root)
    with _EditorOperationFileLock(lock_path) as lock_file:
        if lock_file.try_acquire():
            lock_file.release()
            return False
        return True


def get_editor_operation_lock_snapshot(project_root: Path | None) -> dict | None:
    if project_root is None:
        return None

    held = is_editor_operation_locked(project_root)
    holder = read_editor_operation_metadata(project_root)
    if not held and holder is None:
        return None

    snapshot = {
        "held": held,
        "holder": holder,
        "metadataStale": bool(holder) and not held,
        "lockPath": str(_editor_operation_lock_path(project_root)),
        "metadataPath": str(_editor_operation_metadata_path(project_root)),
    }
    return snapshot


def enrich_diagnose_result_with_editor_lock(project_root: Path | None, result: dict) -> dict:
    if not isinstance(result, dict):
        return result

    snapshot = get_editor_operation_lock_snapshot(project_root)
    if snapshot is None:
        return result

    enriched = dict(result)
    enriched["editorOperationLock"] = snapshot
    return enriched


def _build_busy_details(
    project_root: Path,
    metadata: dict,
    *,
    waited: bool,
    timeout_s: float | None = None,
) -> dict:
    holder = read_editor_operation_metadata(project_root)
    details = {
        "holder": holder,
        "requested": {
            "pid": metadata.get("pid"),
            "command": metadata.get("command"),
            "tool": metadata.get("tool"),
            "projectPath": metadata.get("projectPath"),
        },
        "lockPath": str(_editor_operation_lock_path(project_root)),
        "metadataPath": str(_editor_operation_metadata_path(project_root)),
        "waited": waited,
    }
    if timeout_s is not None:
        details["lockTimeoutSeconds"] = timeout_s
    return details


@contextlib.contextmanager
def editor_operation_lock(project_root: Path, metadata: dict, *, wait: bool, timeout_s: float):
    lock_path = _editor_operation_lock_path(project_root)
    with _EditorOperationFileLock(lock_path) as lock_file:
        acquired = lock_file.try_acquire()
        deadline = time.monotonic() + max(timeout_s, 0.0)

        while not acquired and wait and time.monotonic() < deadline:
            time.sleep(EDITOR_OPERATION_POLL_SECONDS)
            acquired = lock_file.try_acquire()

        if not acquired:
            if wait:
                message = "Timed out waiting for another Unitap editor operation to finish."
            else:
                message = "Another Unitap editor operation is already in progress."
            raise EditorOperationBusyError(
                message,
                _build_busy_details(project_root, metadata, waited=wait, timeout_s=timeout_s if wait else None),
            )

        _write_editor_operation_metadata(project_root, metadata)
        try:
            yield
        finally:
            _clear_editor_operation_metadata(project_root, metadata)
            lock_file.release()
