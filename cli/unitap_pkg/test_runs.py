"""テスト実行の記録 (test_runs record / list / summary)。

EditMode・撮影QA・Automate など、プロジェクトごとに作りの違うテストの結果を
同じ形の1行にして、この Mac の ~/.unitap/test-runs.jsonl に貯める。
clone で流した分も元のプロジェクトの記録として残り、StarSeed などがMacごとに1ファイルを読めば済む。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .commands import print_unitap_response
from .execution_history import append_jsonl_entry, iter_jsonl_entries, parse_since_to_epoch, rotated_history_path
from .lease import resolve_session_owner

SCHEMA_VERSION = 1
TEST_RUNS_NAME = "test-runs.jsonl"
PATH_ENV = "UNITAP_TEST_RUNS_PATH"
TRIGGER_ENV = "UNITAP_TEST_TRIGGER"
PROJECT_ID_ENV = "UNITAP_TEST_PROJECT"

STATUSES = ("passed", "failed", "cancelled", "error", "timeout", "skipped")
# 各プロジェクトの結果ファイルの言い方を揃える
_STATUS_ALIASES = {
    "done": "passed", "ok": "passed", "success": "passed", "succeeded": "passed", "pass": "passed",
    "fail": "failed", "failure": "failed", "canceled": "cancelled", "aborted": "cancelled",
    "timedout": "timeout", "timed_out": "timeout", "skip": "skipped", "cutoff": "timeout",
}
TRIGGERS = ("manual", "nightly", "release", "repair", "ci")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
MAX_FAILURES = 10
MAX_TEXT = 300
_COUNT_KEYS = ("total", "passed", "failed", "skipped")


def test_runs_path() -> Path:
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override).expanduser()
    # editor-slots と同じく Mac 全体で1つ。clone を消しても記録は残る。
    return Path.home() / ".unitap" / TEST_RUNS_NAME


def normalize_status(value) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = _STATUS_ALIASES.get(key, key)
    return key if key in STATUSES else None


def _clip(value, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _git(path: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def resolve_project_identity(project_path: str | None, project_id: str | None = None) -> dict:
    """clone なら元のプロジェクトを、id は元のフォルダ名 (nek-unity・rwd-unity) を使う。"""
    if not project_path:
        return {"project": project_id or os.environ.get(PROJECT_ID_ENV) or None, "projectPath": None, "sourcePath": None}
    from .clone import read_clone_marker

    project = Path(project_path).expanduser()
    marker = read_clone_marker(project) or {}
    source = Path(marker["source"]) if isinstance(marker.get("source"), str) and marker.get("source") else project
    return {
        "project": project_id or os.environ.get(PROJECT_ID_ENV) or source.name,
        "projectPath": str(project),
        "sourcePath": str(source),
        "clone": bool(marker) or None,
    }


def _int_or_none(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def build_test_run(
    *,
    project_path: str | None,
    kind: str,
    suite: str,
    status: str,
    started_at=None,
    completed_at=None,
    seconds=None,
    total=None,
    passed=None,
    failed=None,
    skipped=None,
    failures=None,
    trigger: str | None = None,
    project_id: str | None = None,
    run_dir: str | None = None,
    message: str | None = None,
    extra: dict | None = None,
) -> dict:
    if not isinstance(kind, str) or not _NAME_PATTERN.match(kind):
        raise ValueError(f"kind must match {_NAME_PATTERN.pattern}: {kind!r}")
    if not isinstance(suite, str) or not suite.strip():
        raise ValueError("suite is required")
    normalized = normalize_status(status)
    if normalized is None:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}: {status!r}")
    trigger = trigger or os.environ.get(TRIGGER_ENV) or "manual"
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}: {trigger!r}")

    completed_iso = _iso(completed_at) or datetime.now(timezone.utc).isoformat()
    started_iso = _iso(started_at)
    if seconds is None and started_iso:
        seconds = max(0.0, (_epoch(completed_iso) or 0) - (_epoch(started_iso) or 0))
    if started_iso is None and seconds is not None:
        started_iso = datetime.fromtimestamp((_epoch(completed_iso) or time.time()) - float(seconds), tz=timezone.utc).isoformat()

    counts = {key: _int_or_none(value) for key, value in zip(_COUNT_KEYS, (total, passed, failed, skipped))}
    if counts["total"] is None and any(counts[key] is not None for key in ("passed", "failed", "skipped")):
        counts["total"] = sum(counts[key] or 0 for key in ("passed", "failed", "skipped"))

    identity = resolve_project_identity(project_path, project_id)
    git_root = Path(identity["projectPath"]) if identity.get("projectPath") else None
    entry = {
        "schema": SCHEMA_VERSION,
        "id": uuid.uuid4().hex[:12],
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname().split(".")[0].lower(),
        **{key: value for key, value in identity.items() if value is not None},
        "kind": kind,
        "suite": _clip(suite.strip(), 200),
        "trigger": trigger,
        "status": normalized,
        "startedAt": started_iso,
        "completedAt": completed_iso,
        "seconds": round(float(seconds), 2) if seconds is not None else None,
        **counts,
        "failures": [_clip(item) for item in (failures or []) if str(item).strip()][:MAX_FAILURES],
        "commit": _git(git_root, "rev-parse", "--short=12", "HEAD") if git_root else None,
        "branch": _git(git_root, "rev-parse", "--abbrev-ref", "HEAD") if git_root else None,
        "session": resolve_session_owner(),
    }
    if run_dir:
        entry["runDir"] = str(run_dir)
    if message:
        entry["message"] = _clip(message)
    if extra:
        entry["extra"] = {key: value for key, value in extra.items() if isinstance(value, (str, int, float, bool)) or value is None}
    return entry


def record_test_run(**kwargs) -> dict:
    """1回分を記録して、その行を返す。書けなくてもテストは止めない (append_jsonl_entry と同じ)。"""
    entry = build_test_run(**kwargs)
    append_jsonl_entry(test_runs_path(), entry)
    return entry


def iter_test_runs(path: Path | None = None):
    path = path or test_runs_path()
    # rotate 済みの古い世代から順に読む
    for candidate in (rotated_history_path(path), path):
        for entry in iter_jsonl_entries(candidate) or ():
            if isinstance(entry, dict) and entry.get("schema") == SCHEMA_VERSION:
                yield entry


def select_test_runs(*, since: str | None = None, project: str | None = None, kind: str | None = None,
                     suite: str | None = None, path: Path | None = None) -> list[dict]:
    since_epoch = parse_since_to_epoch(since) if since else None
    rows = []
    for entry in iter_test_runs(path):
        if project and entry.get("project") != project:
            continue
        if kind and entry.get("kind") != kind:
            continue
        if suite and entry.get("suite") != suite:
            continue
        if since_epoch is not None and (_epoch(entry.get("completedAt")) or 0) < since_epoch:
            continue
        rows.append(entry)
    rows.sort(key=lambda entry: entry.get("completedAt") or "")
    return rows


def summarize_test_runs(rows: list[dict]) -> dict:
    """プロジェクト×種類ごとに、最後の実行・失敗中の suite・件数をまとめる。"""
    projects: dict[str, dict] = {}
    for entry in rows:
        project = projects.setdefault(entry.get("project") or "-", {"kinds": {}})
        kind = project["kinds"].setdefault(entry["kind"], {"runs": 0, "suites": {}})
        kind["runs"] += 1
        kind["suites"][entry["suite"]] = entry
    result = {}
    for name, project in projects.items():
        kinds = {}
        for kind_name, kind in project["kinds"].items():
            latest = list(kind["suites"].values())
            last = max(latest, key=lambda entry: entry.get("completedAt") or "")
            failing = sorted((entry for entry in latest if entry["status"] != "passed"),
                             key=lambda entry: entry.get("completedAt") or "", reverse=True)
            kinds[kind_name] = {
                "runs": kind["runs"],
                "suites": len(latest),
                "passingSuites": sum(1 for entry in latest if entry["status"] == "passed"),
                "failingSuites": [{key: entry.get(key) for key in ("suite", "status", "completedAt", "host", "failures")}
                                  for entry in failing[:20]],
                "last": {key: last.get(key) for key in ("suite", "status", "completedAt", "host", "trigger", "seconds",
                                                         "total", "passed", "failed", "skipped", "commit")},
            }
        result[name] = kinds
    return result


def register(subparsers, dispatch_table) -> None:
    p_runs = subparsers.add_parser(
        "test_runs",
        help="Record or read test results (EditMode, screenshot QA, Automate) in this Mac's ~/.unitap/test-runs.jsonl",
    )
    p_runs.add_argument("action", choices=("record", "list", "summary"))
    p_runs.add_argument("--kind", default=None, help="unit / qa / automate / smoke ... (record: required)")
    p_runs.add_argument("--suite", default=None, help="Menu path, pack, fixture pattern or label (record: required)")
    p_runs.add_argument("--status", default=None, help="record: passed/failed/cancelled/error/timeout/skipped (done, success... accepted)")
    p_runs.add_argument("--trigger", default=None, choices=TRIGGERS, help=f"record: why it ran (default: ${TRIGGER_ENV} or manual)")
    p_runs.add_argument("--project-id", default=None, help=f"Project id (default: ${PROJECT_ID_ENV} or the source project's folder name)")
    p_runs.add_argument("--started-at", default=None, help="record: ISO 8601 or Unix seconds")
    p_runs.add_argument("--completed-at", default=None, help="record: ISO 8601 or Unix seconds (default: now)")
    p_runs.add_argument("--seconds", type=float, default=None)
    p_runs.add_argument("--total", type=int, default=None)
    p_runs.add_argument("--passed", type=int, default=None)
    p_runs.add_argument("--failed", type=int, default=None)
    p_runs.add_argument("--skipped", type=int, default=None)
    p_runs.add_argument("--failure", action="append", default=None, help="record: one failure line (repeatable, first 10 kept)")
    p_runs.add_argument("--run-dir", default=None, help="record: folder that holds the full result")
    p_runs.add_argument("--message", default=None)
    p_runs.add_argument("--from-json", default=None,
                        help="record: read fields from a result JSON (kind/suite/status/startedAt/seconds/total/passed/failed/skipped/failures); flags win")
    p_runs.add_argument("--since", default=None, help="list/summary: e.g. 24h, 7d (default: list 7d, summary 30d)")
    p_runs.add_argument("--all-projects", action="store_true", help="list/summary: every project on this Mac, not only --project")
    p_runs.add_argument("--limit", type=int, default=50, help="list: newest N rows")
    p_runs.set_defaults(_skip_heartbeat=True, _skip_history=True)
    dispatch_table["test_runs"] = do_test_runs


def _from_json(path: str) -> dict:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--from-json must contain a JSON object")
    failures = data.get("failures")
    if failures is None and data.get("failure"):
        failures = [data["failure"]]
    return {
        "kind": data.get("kind"), "suite": data.get("suite") or data.get("menu") or data.get("label") or data.get("pattern"),
        "status": data.get("status"), "started_at": data.get("startedAt"), "completed_at": data.get("completedAt"),
        "seconds": data.get("seconds"), "total": data.get("total"), "passed": data.get("passed"),
        "failed": data.get("failed"), "skipped": data.get("skipped"),
        "failures": failures if isinstance(failures, list) else None, "trigger": data.get("trigger"),
    }


def do_test_runs(args, _port=None) -> None:
    project_id = args.project_id
    if not project_id and args.project and not getattr(args, "all_projects", False):
        project_id = resolve_project_identity(args.project)["project"]
    try:
        if args.action == "record":
            if not args.project and not (args.project_id or os.environ.get(PROJECT_ID_ENV)):
                raise ValueError("record needs --project (or --project-id) to know whose test this was")
            fields =_from_json(args.from_json) if args.from_json else {}
            flags = {
                "kind": args.kind, "suite": args.suite, "status": args.status, "trigger": args.trigger,
                "started_at": args.started_at, "completed_at": args.completed_at, "seconds": args.seconds,
                "total": args.total, "passed": args.passed, "failed": args.failed, "skipped": args.skipped,
                "failures": args.failure,
            }
            fields.update({key: value for key, value in flags.items() if value is not None})
            if fields.get("trigger") not in (None, *TRIGGERS):
                fields["trigger"] = None
            for key in ("started_at", "completed_at"):
                value = fields.get(key)
                if isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value):
                    fields[key] = float(value)
            entry = record_test_run(project_path=args.project, project_id=args.project_id, run_dir=args.run_dir,
                                    message=args.message, **{key: fields.get(key) for key in (
                                        "kind", "suite", "status", "trigger", "started_at", "completed_at", "seconds",
                                        "total", "passed", "failed", "skipped", "failures")})
            print_unitap_response(args, {"ok": True, "result": {"recorded": entry, "path": str(test_runs_path())}})
            return
        project = None if args.all_projects else project_id
        if args.action == "list":
            rows = select_test_runs(since=args.since or "7d", project=project, kind=args.kind, suite=args.suite)
            limit = max(1, args.limit)
            print_unitap_response(args, {"ok": True, "result": {
                "path": str(test_runs_path()), "count": len(rows), "runs": rows[-limit:][::-1]}})
            return
        rows = select_test_runs(since=args.since or "30d", project=project, kind=args.kind)
        print_unitap_response(args, {"ok": True, "result": {
            "path": str(test_runs_path()), "since": args.since or "30d", "count": len(rows),
            "projects": summarize_test_runs(rows)}})
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        print_unitap_response(args, {"ok": False, "error": {"code": "invalid_test_run", "message": str(ex)}})
        setattr(args, "_command_exit_code", 1)
        raise SystemExit(1)
