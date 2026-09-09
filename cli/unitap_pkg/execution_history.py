from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .project import resolve_project_root


EXECUTION_HISTORY_NAME = "execution-history.jsonl"
ASYNC_JOB_HISTORY_NAME = "async-job-history.jsonl"

_SUMMARY_KEYS = (
    "backend",
    "operation",
    "action",
    "captureDir",
    "compareFailCount",
    "compareMissingReferenceCount",
    "compareWarnCount",
    "compileStarted",
    "compiled",
    "count",
    "duplicate",
    "errorCount",
    "expectedBuildTarget",
    "firstError",
    "firstFailedStep",
    "hasErrors",
    "idle",
    "isCompiling",
    "isPlaying",
    "isUpdating",
    "jobId",
    "message",
    "outputDir",
    "platform",
    "port",
    "profile",
    "qualityGateFailed",
    "qualityGateReasons",
    "rawSuccess",
    "reportDir",
    "resultJson",
    "runtimeErrorCount",
    "status",
    "success",
    "suiteName",
    "testCasePath",
    "timedOut",
    "total",
    "pending",
    "unitapSuccess",
    "warningCount",
)

_SINCE_PATTERN = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)


def _normalize_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded


def resolve_unitap_dir(project_path: str | None) -> Path | None:
    root = resolve_project_root(project_path, allow_process_discovery=False)
    if root is None:
        return None
    unitap_dir = _normalize_path(root / "Library" / "Unitap")
    unitap_dir.mkdir(parents=True, exist_ok=True)
    return unitap_dir


def get_execution_history_path(project_path: str | None) -> Path | None:
    unitap_dir = resolve_unitap_dir(project_path)
    if unitap_dir is None:
        return None
    return unitap_dir / EXECUTION_HISTORY_NAME


def get_async_job_history_path(project_path: str | None) -> Path | None:
    unitap_dir = resolve_unitap_dir(project_path)
    if unitap_dir is None:
        return None
    return unitap_dir / ASYNC_JOB_HISTORY_NAME


def append_jsonl_entry(path: Path | None, payload: dict) -> None:
    if path is None:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line)
            fp.write("\n")
    except OSError:
        return


def _extract_tool_exec_context(params: dict) -> dict:
    tool_name = params.get("tool")
    nested = params.get("params")
    if not isinstance(nested, dict):
        nested = {}

    context = {
        "tool": tool_name if isinstance(tool_name, str) else None,
        "action": nested.get("action") if isinstance(nested.get("action"), str) else None,
        "suiteName": nested.get("suiteName") if isinstance(nested.get("suiteName"), str) else None,
        "testCasePath": nested.get("testCasePath") if isinstance(nested.get("testCasePath"), str) else None,
        "profile": nested.get("profile") if isinstance(nested.get("profile"), str) else None,
        "contextParam": nested.get("contextParam") if isinstance(nested.get("contextParam"), str) else None,
        "menuPath": nested.get("menuPath") if isinstance(nested.get("menuPath"), str) else None,
    }
    return context


def extract_command_context_from_args(args) -> dict:
    command = str(getattr(args, "command", "") or "")
    context = {
        "command": command,
        "tool": None,
        "action": None,
        "suiteName": None,
        "testCasePath": None,
        "profile": None,
        "contextParam": None,
        "menuPath": None,
        "requestParams": {},
    }

    if command == "tool_exec":
        tool_name = str(getattr(args, "tool", "") or "")
        raw_params = getattr(args, "params", "{}")
        try:
            parsed = json.loads(raw_params)
        except (TypeError, json.JSONDecodeError):
            parsed = {"_raw": raw_params}
        if not isinstance(parsed, dict):
            parsed = {"_raw": parsed}
        context["requestParams"] = {"tool": tool_name or None, "params": parsed}
        context.update(_extract_tool_exec_context(context["requestParams"]))
        if context["tool"] is None:
            context["tool"] = tool_name or None
        return context

    if command in ("unicli", "exec", "eval"):
        operation = getattr(args, "operation", None)
        forwarded = getattr(args, "unicli_args", [])
        context["requestParams"] = {
            "backend": "unicli", "operation": operation,
            "timeoutMs": getattr(args, "timeout_ms", None),
            "commandName": forwarded[0] if operation == "exec" and forwarded else None,
        }
        return context

    if command == "execute_menu":
        context["menuPath"] = getattr(args, "menuPath", None)
        context["requestParams"] = {"menuPath": context["menuPath"]}
        return context

    if command == "launch":
        context["requestParams"] = {
            "restart": bool(getattr(args, "restart", False)),
            "forceRestart": bool(getattr(args, "force_restart", False)),
            "noKill": bool(getattr(args, "no_kill", False)),
            "killProjectOnly": bool(getattr(args, "kill_project_only", False)),
            "noWait": bool(getattr(args, "no_wait", False)),
            "waitTimeout": getattr(args, "wait_timeout", None),
            "ignoreCompilerErrors": bool(getattr(args, "ignore_compiler_errors", False)),
            "noIgnoreCompilerErrors": bool(getattr(args, "no_ignore_compiler_errors", False)),
        }
        return context

    request_params = {}
    for key in ("suiteName", "testCasePath", "profile", "contextParam", "pack"):
        value = getattr(args, key, None)
        if value is not None:
            request_params[key] = value

    context["suiteName"] = request_params.get("suiteName")
    context["testCasePath"] = request_params.get("testCasePath")
    context["profile"] = request_params.get("profile")
    context["contextParam"] = request_params.get("contextParam")
    if command in ("run_automate_test", "run_playmode_test"):
        context["tool"] = command
    elif command in ("run_automate_batch", "run_automate_pack"):
        context["tool"] = "run_automate_test"
    context["requestParams"] = request_params
    return context


def summarize_result_payload(result) -> dict | None:
    if not isinstance(result, dict):
        return None

    summary: dict[str, object] = {}
    for key in _SUMMARY_KEYS:
        value = result.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            summary[key] = value
            continue
        if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
            summary[key] = value

    data = result.get("data")
    if isinstance(data, dict):
        for key in ("action", "currentAction", "currentStep", "message", "status", "suiteName", "testCasePath"):
            value = data.get(key)
            if value is not None and key not in summary and isinstance(value, (str, int, float, bool)):
                summary[key] = value

    return summary or None


def build_execution_history_entry(
    *,
    project_path: str | None,
    args,
    response: dict | None,
    started_at_epoch: float,
    completed_at_epoch: float | None = None,
    exit_code: int | None = None,
) -> dict | None:
    path = get_execution_history_path(project_path)
    if path is None:
        return None

    completed_at_epoch = completed_at_epoch if completed_at_epoch is not None else time.time()
    started_at = datetime.fromtimestamp(started_at_epoch, tz=timezone.utc)
    completed_at = datetime.fromtimestamp(completed_at_epoch, tz=timezone.utc)
    context = extract_command_context_from_args(args)

    ok = exit_code in (None, 0)
    error_code = None
    error_message = None
    processing_time_ms = None
    transport_kind = None
    editor = None
    result_summary = None

    if isinstance(response, dict):
        if isinstance(response.get("ok"), bool):
            ok = bool(response.get("ok"))
        error = response.get("error")
        if isinstance(error, dict):
            error_code = error.get("code")
            error_message = error.get("message")
        processing_time_ms = response.get("processingTimeMs")
        transport_kind = response.get("transportKind")
        response_editor = response.get("editor")
        if isinstance(response_editor, dict):
            editor = {
                key: response_editor.get(key)
                for key in ("isPlaying", "isCompiling", "isUpdating", "activeScene")
                if response_editor.get(key) is not None
            } or None
        result_summary = summarize_result_payload(response.get("result"))

    if not ok and not error_code and exit_code not in (None, 0):
        error_code = "system_exit"
        error_message = f"Exited with status {exit_code}"

    return {
        "source": "unitap_cli",
        "timestamp": completed_at.isoformat(),
        "startedAt": started_at.isoformat(),
        "completedAt": completed_at.isoformat(),
        "durationMs": round((completed_at_epoch - started_at_epoch) * 1000),
        "projectPath": project_path,
        "ok": ok,
        "exitCode": exit_code if exit_code is not None else (0 if ok else 1),
        "errorCode": error_code,
        "errorMessage": error_message,
        "processingTimeMs": processing_time_ms,
        "transportKind": transport_kind,
        "editor": editor,
        "resultSummary": result_summary,
        **context,
    }


def record_execution_history(
    *,
    project_path: str | None,
    args,
    response: dict | None,
    started_at_epoch: float,
    completed_at_epoch: float | None = None,
    exit_code: int | None = None,
) -> None:
    entry = build_execution_history_entry(
        project_path=project_path,
        args=args,
        response=response,
        started_at_epoch=started_at_epoch,
        completed_at_epoch=completed_at_epoch,
        exit_code=exit_code,
    )
    if entry is None:
        return
    append_jsonl_entry(get_execution_history_path(project_path), entry)


def iter_jsonl_entries(path: Path | None):
    if path is None or not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield data
    except OSError:
        return


def parse_since_to_epoch(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    match = _SINCE_PATTERN.match(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        seconds_per_unit = {
            "m": 60,
            "h": 3600,
            "d": 86400,
            "w": 7 * 86400,
        }
        return time.time() - amount * seconds_per_unit[unit]

    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, f"{normalized}T00:00:00+09:00", f"{normalized}T00:00:00+00:00"):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None
