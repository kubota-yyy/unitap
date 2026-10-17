"""Refuse to run the same QA menu again when nothing in the project changed since its last run.

A QA menu run takes minutes. Running it again on unchanged code and assets only repeats the
same result, so agents must read the previous result, fix something, then run it once more.
The fingerprint covers what a QA exercises (Assets, Packages, ProjectSettings): the git HEAD
plus the size and mtime of every changed or untracked file there. Any edit, including to the QA
script itself, makes the next run allowed. When git is unavailable the guard stays out of the way.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "Library/Unitap/qa-runs.json"
FINGERPRINT_PATHS = ("Assets", "Packages", "ProjectSettings")
DEFAULT_QA_MENU_PATTERN = r"(^|/)QA(/|$)"


def guard_enabled() -> bool:
    return os.environ.get("UNITAP_QA_REPEAT_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")


def is_qa_menu(menu_path: str) -> bool:
    pattern = os.environ.get("UNITAP_QA_MENU_PATTERN") or DEFAULT_QA_MENU_PATTERN
    try:
        return re.search(pattern, menu_path or "") is not None
    except re.error:
        return re.search(DEFAULT_QA_MENU_PATTERN, menu_path or "") is not None


def _git(project_root: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def project_fingerprint(project_root: Path) -> str | None:
    head = _git(project_root, "rev-parse", "HEAD")
    status = _git(
        project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *FINGERPRINT_PATHS
    )
    if head is None or status is None:
        return None
    digest = hashlib.sha256(head.strip())
    for entry in sorted(e for e in status.split(b"\0") if len(e) > 3):
        digest.update(b"\n" + entry)
        path = project_root / os.fsdecode(entry[3:])
        try:
            stat = path.stat()
            digest.update(f" {stat.st_size} {stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b" missing")
    return digest.hexdigest()


def _read_state(project_root: Path) -> dict:
    try:
        data = json.loads((project_root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def check_repeat(project_root: Path | None, menu_path: str) -> dict | None:
    """Return the previous run when this QA menu would rerun on an unchanged project."""
    if project_root is None or not guard_enabled() or not is_qa_menu(menu_path):
        return None
    previous = _read_state(project_root).get(menu_path)
    if not isinstance(previous, dict) or not previous.get("fingerprint"):
        return None
    current = project_fingerprint(project_root)
    if current is None or current != previous["fingerprint"]:
        return None
    return previous


def record_run(project_root: Path | None, menu_path: str, session: str | None = None) -> None:
    if project_root is None or not is_qa_menu(menu_path):
        return
    fingerprint = project_fingerprint(project_root)
    if fingerprint is None:
        return
    state = _read_state(project_root)
    state[menu_path] = {
        "fingerprint": fingerprint,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session,
    }
    path = project_root / STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
