"""Machine-wide admission control for new Unity Editors (no Editor operations)."""
from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from .editor_lock import _EditorOperationFileLock
from .unity import UnityProcessListError, _is_same_project_path, _process_exists, list_unity_processes

DEFAULT_WAIT_SLOT_TIMEOUT = 1800.0
MIN_FREE_PERCENT = 15.0
POLL_SECONDS = 5.0


def _read_command(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read_memory_status() -> dict:
    """Unknown metrics stay null; never interpret a failed probe as free memory."""
    physical = free = swap = None
    errors = []
    if platform.system() == "Darwin":
        physical_text = _read_command(["sysctl", "-n", "hw.memsize"])
        if physical_text and physical_text.strip().isdigit():
            physical = int(physical_text.strip())
        pressure = _read_command(["memory_pressure"])
        match = re.search(r"free percentage:\s*(\d+(?:\.\d+)?)%", pressure or "", re.I)
        if match:
            free = float(match[1])
        swap_text = _read_command(["sysctl", "vm.swapusage"])
        values = re.findall(r"(total|used|free)\s*=\s*([\d.]+)([KMGT]?)", swap_text or "", re.I)
        if len(values) == 3:
            swap = {key.lower() + "Bytes": round(float(value) * 1024 ** (" KMGT".index(unit.upper())))
                    for key, value, unit in values}
        for name, value in (("physicalMemory", physical), ("freePercent", free), ("swap", swap)):
            if value is None:
                errors.append(f"Could not read {name} on macOS.")
    else:
        try:
            physical = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            pass
    return {"physicalBytes": physical, "freePercent": free, "minFreePercent": MIN_FREE_PERCENT,
            "swap": swap, "errors": errors}


def _slot_directory() -> Path:
    # Shared by every checkout/session of this user; never project-local.
    return Path.home() / ".unitap"


def _pending_editors(processes: list[dict]) -> list[dict]:
    """Bridge Popen -> exec: ps may not yet identify a just-spawned Unity child."""
    try:
        records = json.loads((_slot_directory() / "editor-slots.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    seen = {p.get("pid") for p in processes}
    return [r for r in records if isinstance(r, dict) and isinstance(r.get("pid"), int)
            and r["pid"] > 0 and r["pid"] not in seen
            and isinstance(r.get("expiresAt"), (int, float)) and r["expiresAt"] > time.time()
            and _process_exists(r["pid"])]


def editor_slot_snapshot(project_root: Path | None = None) -> dict:
    memory = read_memory_status()
    default_limit = max(1, int(memory["physicalBytes"] or 0) // (8 * 1024 ** 3))
    reasons = []
    limit = default_limit
    override = os.environ.get("UNITAP_MAX_EDITORS")
    if override is not None:
        try:
            limit = int(override)
            if limit < 1:
                raise ValueError
        except ValueError:
            limit = default_limit
            reasons.append("UNITAP_MAX_EDITORS must be a positive integer")
    try:
        editors = list_unity_processes(strict=True)
    except UnityProcessListError as ex:
        editors = []
        reasons.append(str(ex))
    pending = _pending_editors(editors)
    all_editors = editors + pending
    counted = [p for p in all_editors
               if not (project_root and _is_same_project_path(p.get("projectPath"), project_root))]
    if len(counted) >= limit:
        reasons.append(f"Editor limit reached ({len(counted)}/{limit})")
    if memory["freePercent"] is not None and memory["freePercent"] < MIN_FREE_PERCENT:
        reasons.append(f"Memory free {memory['freePercent']:g}% < {MIN_FREE_PERCENT:g}%")
    reasons.extend(memory["errors"])
    return {"maxEditors": limit, "currentCount": len(all_editors), "countedEditors": len(counted),
            "excludedProjectPath": str(project_root) if project_root else None,
            "editors": editors, "pendingEditors": pending, "memory": memory,
            "available": not reasons, "reasons": reasons}


class EditorSlotUnavailable(RuntimeError):
    code = "editor_slot_unavailable"

    def __init__(self, details: dict):
        super().__init__("No Unity Editor slot available: " + "; ".join(details["reasons"]))
        self.details = details


class _SlotReservation:
    def __init__(self, snapshot: dict, project_root: Path):
        self.snapshot = snapshot
        self.project_root = project_root

    def started(self, pid: int) -> None:
        pending = self.snapshot["pendingEditors"] + [{
            "pid": pid, "projectPath": str(self.project_root), "expiresAt": time.time() + 60,
        }]
        (_slot_directory() / "editor-slots.json").write_text(json.dumps(pending))


@contextlib.contextmanager
def editor_slot(project_root: Path, *, wait: bool = True, timeout_s: float = DEFAULT_WAIT_SLOT_TIMEOUT):
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise EditorSlotUnavailable({"reasons": ["--wait-slot-timeout must be finite and >= 0"]})
    started = time.monotonic()
    deadline = started + timeout_s
    next_notice = started
    directory = _slot_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _EditorOperationFileLock(directory / "editor-slots.lock") as lock:
            while True:
                acquired = lock.try_acquire()
                snapshot = editor_slot_snapshot(project_root)
                if acquired and snapshot["available"]:
                    try:
                        yield _SlotReservation(snapshot, project_root)
                    finally:
                        lock.release()
                    return
                if acquired:
                    lock.release()
                else:
                    snapshot["reasons"].append("Another session is starting an Editor")
                    snapshot["available"] = False
                now = time.monotonic()
                snapshot["waitedSeconds"] = round(now - started, 3)
                snapshot["waitSlotTimeout"] = timeout_s
                if not wait or now >= deadline:
                    raise EditorSlotUnavailable(snapshot)
                if now >= next_notice:
                    print("[unitap] waiting for Editor slot: " + "; ".join(snapshot["reasons"]),
                          file=sys.stderr, flush=True)
                    next_notice = now + 60
                time.sleep(min(POLL_SECONDS, max(0, deadline - now)))
    except OSError as ex:
        raise EditorSlotUnavailable({"reasons": [f"Cannot access Editor slot lock/reservation: {ex}"]}) from ex
