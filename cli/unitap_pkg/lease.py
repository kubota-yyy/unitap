"""セッション単位の Editor 占有 (lease)。

`.editor-op.lock` はコマンド 1 回ごとの排他なので、PlayMode の QA のように
「play → 操作 → 待機 → capture → stop」を続けて行う間に、別セッションの
play / stop / compile_check / launch --restart が割り込める。lease は作業の
区切りまで Editor を 1 セッションに予約し、他セッションの排他コマンドを待たせる。

lease ファイルが無い限り挙動は従来どおり (後方互換)。
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .editor_lock import _EditorOperationFileLock, _editor_operation_dir

LEASE_FILE_NAME = ".session-lease.json"
LEASE_LOCK_NAME = ".session-lease.lock"
DEFAULT_LEASE_TTL_SECONDS = 1800
LEASE_POLL_SECONDS = 1.0

# 優先順。明示指定 > 各エージェントのセッション ID。
_SESSION_ENV_KEYS = (
    ("UNITAP_SESSION", None),
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_THREAD_ID", "codex"),
    ("CODEX_SESSION_ID", "codex"),
    ("STARSESSION_SESSION_ID", "starsession"),
)


class EditorLeasedError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = "editor_leased"
        self.message = message
        self.details = details or {}


def resolve_session_owner(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for key, prefix in _SESSION_ENV_KEYS:
        value = (env.get(key) or "").strip()
        if value:
            return value if prefix is None else f"{prefix}:{value}"
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _lease_path(project_root: Path) -> Path:
    return _editor_operation_dir(project_root) / LEASE_FILE_NAME


@contextlib.contextmanager
def _lease_file_guard(project_root: Path):
    """lease ファイルの read-modify-write を直列化する (短時間のみ保持)。"""
    lock_path = _editor_operation_dir(project_root) / LEASE_LOCK_NAME
    with _EditorOperationFileLock(lock_path) as lock_file:
        deadline = time.monotonic() + 5.0
        acquired = lock_file.try_acquire()
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.05)
            acquired = lock_file.try_acquire()
        try:
            yield
        finally:
            if acquired:
                lock_file.release()


def read_lease(project_root: Path | None) -> dict | None:
    if project_root is None:
        return None
    path = _lease_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def lease_is_active(lease: dict | None, now: datetime | None = None) -> bool:
    if not isinstance(lease, dict):
        return False
    expires = _parse_time(lease.get("expiresAt"))
    if expires is None:
        return False
    return expires > (now or _now())


def lease_snapshot(project_root: Path | None, owner: str | None = None) -> dict | None:
    lease = read_lease(project_root)
    if lease is None:
        return None
    snapshot = dict(lease)
    snapshot["active"] = lease_is_active(lease)
    snapshot["ownedByCaller"] = bool(owner) and lease.get("owner") == owner
    expires = _parse_time(lease.get("expiresAt"))
    if expires is not None:
        snapshot["remainingSeconds"] = max(0, int((expires - _now()).total_seconds()))
    return snapshot


def _write_lease(project_root: Path, lease: dict) -> None:
    path = _lease_path(project_root)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(lease, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def acquire_lease(
    project_root: Path,
    owner: str,
    *,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    note: str | None = None,
    wait: bool = False,
    timeout_s: float = 0.0,
) -> dict:
    """lease を取得または延長する。他セッションが有効な lease を持つ場合は待つか失敗する。"""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        with _lease_file_guard(project_root):
            current = read_lease(project_root)
            if lease_is_active(current) and current.get("owner") != owner:
                holder = current
            else:
                now = _now()
                renewing = lease_is_active(current) and current.get("owner") == owner
                lease = {
                    "owner": owner,
                    "note": note if note is not None else (current.get("note") if renewing else None),
                    "callerPid": os.getppid(),
                    "acquiredAt": current.get("acquiredAt") if renewing else now.isoformat(),
                    "renewedAt": now.isoformat(),
                    "ttlSeconds": int(ttl_seconds),
                    "expiresAt": (now + timedelta(seconds=int(ttl_seconds))).isoformat(),
                    "projectPath": str(project_root),
                }
                _write_lease(project_root, lease)
                return lease
        if not wait or time.monotonic() >= deadline:
            raise EditorLeasedError(
                "Another session holds the Unity Editor lease for this project.",
                {"holder": holder, "requestedBy": owner},
            )
        time.sleep(LEASE_POLL_SECONDS)


def release_lease(project_root: Path, owner: str | None, *, force: bool = False) -> dict:
    with _lease_file_guard(project_root):
        current = read_lease(project_root)
        if current is None:
            return {"released": False, "reason": "no_lease"}
        if not force and lease_is_active(current) and current.get("owner") != owner:
            raise EditorLeasedError(
                "The lease belongs to another session. Use --force only when that session is gone.",
                {"holder": current, "requestedBy": owner},
            )
        try:
            _lease_path(project_root).unlink()
        except OSError:
            pass
        return {"released": True, "previous": current}


def touch_lease_if_owned(project_root: Path, owner: str | None) -> None:
    """lease 所有者の排他コマンド実行時に期限を延長する。"""
    if not owner:
        return
    current = read_lease(project_root)
    if not lease_is_active(current) or current.get("owner") != owner:
        return
    try:
        acquire_lease(project_root, owner, ttl_seconds=int(current.get("ttlSeconds") or DEFAULT_LEASE_TTL_SECONDS))
    except EditorLeasedError:
        pass


def wait_for_foreign_lease(
    project_root: Path,
    owner: str | None,
    *,
    wait: bool,
    timeout_s: float,
) -> float:
    """他セッションの有効な lease が外れるまで待つ。待った秒数を返す。"""
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    while True:
        current = read_lease(project_root)
        if not lease_is_active(current) or (owner and current.get("owner") == owner):
            return time.monotonic() - started
        if not wait or time.monotonic() >= deadline:
            raise EditorLeasedError(
                "Another session holds the Unity Editor lease for this project. "
                "Wait for it, use a cloned project (`unitap clone`), or pass --ignore-lease if that session is gone.",
                {
                    "holder": current,
                    "requestedBy": owner,
                    "waited": wait,
                    "waitedSeconds": round(time.monotonic() - started, 3),
                },
            )
        time.sleep(LEASE_POLL_SECONDS)


# --- Play Mode の開始セッション記録 -------------------------------------------
# compile_check は Play Mode 中だと stop してから再実行する。別セッションが
# PlayMode QA 中だと、その QA を黙って止めてしまうため、誰が play したかを残す。
PLAY_OWNER_FILE_NAME = ".play-owner.json"
PLAY_OWNER_MAX_AGE_SECONDS = 12 * 3600


def _play_owner_path(project_root: Path) -> Path:
    return _editor_operation_dir(project_root) / PLAY_OWNER_FILE_NAME


def record_play_owner(project_root: Path, owner: str | None) -> None:
    if not owner:
        clear_play_owner(project_root)
        return
    try:
        _play_owner_path(project_root).write_text(
            json.dumps({"owner": owner, "startedAt": _now().isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear_play_owner(project_root: Path) -> None:
    try:
        _play_owner_path(project_root).unlink()
    except OSError:
        pass


def foreign_play_owner(project_root: Path | None, owner: str | None) -> dict | None:
    """別セッションが開始した Play Mode の記録を返す (自分・不明・古い記録なら None)。"""
    if project_root is None:
        return None
    try:
        data = json.loads(_play_owner_path(project_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("owner"):
        return None
    if owner and data.get("owner") == owner:
        return None
    started = _parse_time(data.get("startedAt"))
    if started is None or (_now() - started).total_seconds() > PLAY_OWNER_MAX_AGE_SECONDS:
        return None
    return data
