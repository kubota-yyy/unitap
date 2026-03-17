import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from .constants import (
    EDITOR_LOG_FALLBACK_COMMANDS,
    CONNECTION_RETRY_MAX,
    LIVENESS_RECONNECT_RETRY_MAX,
)
from .editor_log import find_project_root, read_compile_errors, try_editor_log_fallback
from .editor_lock import (
    EditorOperationBusyError,
    build_editor_lock_metadata,
    command_requires_editor_lock,
    editor_operation_lock,
)
from .heartbeat import check_heartbeat_fresh, find_heartbeat
from .project import ProjectResolutionError, resolve_project_root
from .transport import wait_for_connection
from .unity import focus_unity_editor, is_unity_process_running
from .commands import (
    do_capture,
    do_capture_editor,
    do_compile_check,
    do_focus,
    do_launch,
    do_play,
    do_sync_command,
    do_wait_fsm,
    do_wait_idle,
)

# Extension point: loaded if unitap_ext package is available on sys.path
_ext_module = None
try:
    import unitap_ext as _ext_module
except ImportError:
    pass
except Exception as e:
    print(f"Warning: unitap_ext found but failed to load: {e}", file=sys.stderr)


def _print_cli_error(args, code: str, message: str, details: dict | None = None) -> None:
    if args.json:
        payload = {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if details:
            payload["error"]["details"] = details
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Error [{code}]: {message}", file=sys.stderr)


def _is_unity_running_for_project(project_root: Path | None) -> bool:
    if project_root is not None:
        return is_unity_process_running(project_root)
    return is_unity_process_running()


def _append_wait_meta(
    args,
    reason: str,
    started_at: float,
    recovered: bool,
    max_retries: int | None = None,
    auto_focus_attempted: bool | None = None,
    auto_focus_succeeded: bool | None = None,
) -> None:
    elapsed = max(0.0, time.time() - started_at)
    meta = getattr(args, "_wait_meta", None)
    if not isinstance(meta, dict):
        meta = {
            "waitedForReconnect": False,
            "waitReconnectSeconds": 0.0,
            "waitReconnectEvents": [],
        }

    events = list(meta.get("waitReconnectEvents", []))
    event = {
        "reason": reason,
        "elapsedSeconds": round(elapsed, 3),
        "recovered": bool(recovered),
    }
    if max_retries is not None:
        event["maxRetries"] = int(max_retries)
    if auto_focus_attempted is not None:
        event["autoFocusAttempted"] = bool(auto_focus_attempted)
    if auto_focus_succeeded is not None:
        event["autoFocusSucceeded"] = bool(auto_focus_succeeded)
    events.append(event)

    total = float(meta.get("waitReconnectSeconds", 0.0)) + elapsed
    meta["waitedForReconnect"] = True
    meta["waitReconnectSeconds"] = round(total, 3)
    meta["waitReconnectEvents"] = events
    meta["waitReconnectReason"] = reason
    meta["waitReconnectRecovered"] = bool(recovered)
    meta["waitReconnectEventCount"] = len(events)
    if auto_focus_attempted is not None:
        meta["waitReconnectAutoFocusAttempted"] = bool(
            meta.get("waitReconnectAutoFocusAttempted", False) or auto_focus_attempted
        )
    if auto_focus_succeeded is not None:
        meta["waitReconnectAutoFocusSucceeded"] = bool(
            meta.get("waitReconnectAutoFocusSucceeded", False) or auto_focus_succeeded
        )

    setattr(args, "_wait_meta", meta)


def main():
    parser = argparse.ArgumentParser(description="Unitap - Unity Editor control CLI")
    parser.add_argument("--project", help="Unity project path", default=None)
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--wait-lock",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for the editor operation lock (default: enabled; use --no-wait-lock to fail immediately)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=1800.0,
        help="Maximum seconds to wait when --wait-lock is specified",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- Core commands ---
    subparsers.add_parser("status")
    p_play = subparsers.add_parser("play")
    p_play.add_argument("--wait-idle-first", action="store_true", help="Run wait_idle before sending play")
    p_play.add_argument("--idle-timeout", type=int, default=30000, help="Timeout for --wait-idle-first in ms")
    p_play.add_argument(
        "--wait-idle-required",
        action="store_true",
        help="Treat wait_idle timeout as error when used with --wait-idle-first",
    )
    p_play.add_argument("--wait-fsm-gameobject", default=None, help="Target GameObject path for FSM wait")
    p_play.add_argument("--wait-fsm-name", default=None, help="FSM name for wait")
    p_play.add_argument("--wait-fsm-state", default=None, help="Target FSM state to wait for")
    p_play.add_argument("--wait-fsm-timeout", type=float, default=20.0, help="FSM wait timeout in seconds")
    p_play.add_argument("--wait-fsm-poll", type=float, default=0.2, help="FSM poll interval in seconds")
    p_play.add_argument("--wait-fsm-required", action="store_true", help="Fail when FSM wait times out")
    p_play.add_argument("--capture-output", default=None, help="Capture GameView to path after play")
    p_play.add_argument("--capture-supersize", type=int, default=1, help="Capture resolution multiplier")
    subparsers.add_parser("stop")

    p_menu = subparsers.add_parser("execute_menu")
    p_menu.add_argument("--menuPath", required=True)

    subparsers.add_parser("refresh")
    subparsers.add_parser("focus", help="Bring Unity Editor to front")

    p_idle = subparsers.add_parser("wait_idle")
    p_idle.add_argument("--timeout", type=int, default=30000)
    p_idle.set_defaults(auto_focus_on_stall=True)
    p_idle.add_argument(
        "--auto-focus-on-stall",
        dest="auto_focus_on_stall",
        action="store_true",
        help="Try focusing Unity when wait_idle appears stalled (default: enabled)",
    )
    p_idle.add_argument(
        "--no-auto-focus-on-stall",
        dest="auto_focus_on_stall",
        action="store_false",
        help="Disable auto focus recovery for wait_idle stall handling",
    )
    p_idle.add_argument(
        "--stall-focus-interval-ms",
        type=int,
        default=6000,
        help="Minimum interval between focus attempts while waiting for reconnect (ms)",
    )

    p_wait_fsm = subparsers.add_parser("wait_fsm", help="Wait until FSM reaches target state")
    p_wait_fsm.add_argument("--gameObject", required=True, help="Target GameObject path")
    p_wait_fsm.add_argument("--fsmName", required=True, help="FSM name")
    p_wait_fsm.add_argument("--state", required=True, help="Target state name")
    p_wait_fsm.add_argument("--timeout", type=float, default=20.0, help="Timeout in seconds")
    p_wait_fsm.add_argument("--poll-interval", type=float, default=0.2, help="Poll interval in seconds")

    p_console = subparsers.add_parser("read_console")
    p_console.add_argument("--type", default=None, help="error|warning|log")
    p_console.add_argument("--limit", type=int, default=200)
    p_console.add_argument("--since-last-clear", action="store_true",
                           help="Only include logs recorded after the last clear_console")
    p_console.add_argument("--since", default=None,
                           help="Only include logs since ISO8601 timestamp (e.g. 2026-02-15T00:00:00Z)")

    subparsers.add_parser("clear_console")
    subparsers.add_parser("cancel")
    subparsers.add_parser("tool_list")

    p_exec = subparsers.add_parser("tool_exec")
    p_exec.add_argument("--tool", required=True)
    p_exec.add_argument("--params", default="{}", help="JSON params")

    p_compile = subparsers.add_parser("compile_check", help="Compile and check for errors")
    p_compile.add_argument("--timeout", type=int, default=90000, help="Timeout in ms")
    p_compile.add_argument("--max-retries", type=int, default=3, dest="max_retries",
                           help="Max retries when timed out while still compiling (default: 3)")
    p_compile.set_defaults(focus_unity=True, auto_focus_on_stall=True)
    p_compile.add_argument(
        "--focus-unity",
        dest="focus_unity",
        action="store_true",
        help="Focus Unity before compile_check and retry once on timeout while compiling (default: enabled)",
    )
    p_compile.add_argument(
        "--no-focus-unity",
        dest="focus_unity",
        action="store_false",
        help="Disable Unity focusing during compile_check",
    )
    p_compile.add_argument(
        "--focus-wait-ms",
        type=int,
        default=350,
        help="Wait after focusing Unity (ms)",
    )
    p_compile.add_argument(
        "--auto-focus-on-stall",
        dest="auto_focus_on_stall",
        action="store_true",
        help="Try focusing Unity when compile_check appears stalled (default: enabled)",
    )
    p_compile.add_argument(
        "--no-auto-focus-on-stall",
        dest="auto_focus_on_stall",
        action="store_false",
        help="Disable auto focus recovery for compile_check stall handling",
    )
    p_compile.add_argument(
        "--stall-focus-interval-ms",
        type=int,
        default=6000,
        help="Minimum interval between focus attempts while waiting for reconnect (ms)",
    )

    p_save = subparsers.add_parser("save_scene", help="Save current or all scenes")
    p_save.add_argument("--all", action="store_true", help="Save all open scenes")

    subparsers.add_parser("undo", help="Undo last action")
    subparsers.add_parser("redo", help="Redo last undone action")

    subparsers.add_parser("diagnose", help="Diagnose editor issues")

    p_capture = subparsers.add_parser("capture", help="Capture GameView screenshot")
    p_capture.add_argument("--output", default="/tmp/unity_gameview.png", help="Output file path")
    p_capture.add_argument("--superSize", type=int, default=1, help="Resolution multiplier (1-4)")
    p_capture.add_argument("--timeout", type=float, default=10.0, help="File write timeout in seconds")

    p_capture_editor = subparsers.add_parser("capture_editor", help="Capture an EditorWindow screenshot")
    p_capture_editor.add_argument("--output", default="/tmp/unity_editor_window.png", help="Output file path")
    p_capture_editor.add_argument("--window", default=None,
                                  help="Window title/type hint (e.g. Inspector, PlayMaker Editor)")
    p_capture_editor.add_argument("--window-title", default=None,
                                  help="Window title substring filter")
    p_capture_editor.add_argument("--window-type", default=None,
                                  help="EditorWindow type name/full name filter")
    p_capture_editor.add_argument("--index", type=int, default=0,
                                  help="Match index when multiple windows are found")
    p_capture_editor.add_argument("--menu-path", default=None,
                                  help="MenuItem path to open the window when not found")
    p_capture_editor.add_argument("--no-focus", action="store_true",
                                  help="Do not focus the target window before capture")
    p_capture_editor.add_argument("--no-open", action="store_true",
                                  help="Do not auto-open windowType when not found")

    subparsers.add_parser("heartbeat", help="Show heartbeat info")

    p_launch = subparsers.add_parser("launch", help="Launch Unity Editor")
    p_launch.add_argument("--no-kill", action="store_true", help="Don't kill existing Unity processes")
    p_launch.add_argument("--kill-project-only", action="store_true", help="Only kill Unity for this project")
    p_launch.add_argument("--restart", action="store_true", help="Force restart when Unity is already running")
    p_launch.add_argument(
        "--ignore-compiler-errors",
        action="store_true",
        help="Append -ignoreCompilerErrors when launching Unity",
    )
    p_launch.add_argument("--no-wait", action="store_true", help="Don't wait for startup confirmation")
    p_launch.add_argument("--wait-timeout", type=int, default=180, help="Startup wait timeout in seconds")

    # --- Dispatch table ---
    dispatch_table = {
        "launch": lambda args, port: do_launch(args),
        "focus": lambda args, port: do_focus(args),
        "wait_idle": do_wait_idle,
        "wait_fsm": do_wait_fsm,
        "play": do_play,
        "compile_check": do_compile_check,
        "capture": do_capture,
        "capture_editor": do_capture_editor,
    }

    # --- Extension registration ---
    ext_ok = False
    if _ext_module and hasattr(_ext_module, "register"):
        try:
            _ext_module.register(subparsers, dispatch_table)
            ext_ok = True
        except Exception as e:
            print(f"Warning: unitap_ext.register() failed: {e}", file=sys.stderr)

    args = parser.parse_args()

    # --- Validation ---
    if args.command == "read_console" and args.since_last_clear and args.since:
        print("Error: --since-last-clear and --since cannot be used together", file=sys.stderr)
        sys.exit(1)

    if args.command == "play":
        if args.idle_timeout < 0:
            print("Error: --idle-timeout must be >= 0", file=sys.stderr)
            sys.exit(1)
        if args.wait_fsm_timeout <= 0:
            print("Error: --wait-fsm-timeout must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.wait_fsm_poll <= 0:
            print("Error: --wait-fsm-poll must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.capture_supersize < 1:
            print("Error: --capture-supersize must be >= 1", file=sys.stderr)
            sys.exit(1)
        fsm_wait_values = [args.wait_fsm_gameobject, args.wait_fsm_name, args.wait_fsm_state]
        if any(v is not None for v in fsm_wait_values) and not all(v is not None for v in fsm_wait_values):
            print(
                "Error: --wait-fsm-gameobject, --wait-fsm-name, and --wait-fsm-state must all be specified together",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.command == "wait_fsm":
        if args.timeout <= 0:
            print("Error: --timeout must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.poll_interval <= 0:
            print("Error: --poll-interval must be > 0", file=sys.stderr)
            sys.exit(1)

    if args.command in ("wait_idle", "compile_check"):
        if args.timeout <= 0:
            print("Error: --timeout must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.stall_focus_interval_ms < 0:
            print("Error: --stall-focus-interval-ms must be >= 0", file=sys.stderr)
            sys.exit(1)
    if args.lock_timeout <= 0:
        print("Error: --lock-timeout must be > 0", file=sys.stderr)
        sys.exit(1)

    try:
        resolved_project = resolve_project_root(args.project, allow_process_discovery=True)
    except ProjectResolutionError as ex:
        _print_cli_error(args, ex.code, ex.message, ex.details)
        sys.exit(1)

    if resolved_project:
        args.project = str(resolved_project)

    lock_context = contextlib.nullcontext()
    if command_requires_editor_lock(args):
        if resolved_project is None:
            _print_cli_error(
                args,
                "project_not_found",
                "Unity project not found for editor operation lock.",
                {"command": args.command},
            )
            sys.exit(1)
        metadata = build_editor_lock_metadata(args, resolved_project)
        lock_context = editor_operation_lock(
            resolved_project,
            metadata,
            wait=bool(args.wait_lock),
            timeout_s=float(args.lock_timeout),
        )

    # Commands that don't need heartbeat
    if args.command == "launch":
        try:
            with lock_context:
                dispatch_table["launch"](args, None)
        except EditorOperationBusyError as ex:
            _print_cli_error(args, ex.code, ex.message, ex.details)
            sys.exit(1)
        return

    if args.command == "focus":
        dispatch_table["focus"](args, None)
        return

    # --- Pre-heartbeat hooks (extensions can add watchdog recovery here) ---
    pre_heartbeat_hook = None
    if _ext_module and hasattr(_ext_module, "pre_heartbeat_hook"):
        pre_heartbeat_hook = _ext_module.pre_heartbeat_hook

    # Heartbeat check
    heartbeat = find_heartbeat(args.project)
    project_root = find_project_root(args.project)

    if not heartbeat:
        if pre_heartbeat_hook:
            heartbeat = pre_heartbeat_hook(args, project_root)

        process_running = _is_unity_running_for_project(project_root)
        if not heartbeat and process_running:
            print("Unity process detected but heartbeat is missing, waiting for Unitap reconnect...", file=sys.stderr)
            reconnect_started_at = time.time()
            heartbeat = wait_for_connection(
                args.project,
                max_retries=LIVENESS_RECONNECT_RETRY_MAX,
                require_tcp=True,
            )
            _append_wait_meta(
                args,
                reason="missing_heartbeat_reconnect",
                started_at=reconnect_started_at,
                recovered=bool(heartbeat),
                max_retries=LIVENESS_RECONNECT_RETRY_MAX,
            )

        if heartbeat:
            pass
        elif try_editor_log_fallback(args, "heartbeat not found"):
            return
        elif process_running:
            _print_cli_error(
                args,
                "unity_running_but_unitap_unavailable",
                "Unity process is running but Unitap heartbeat/TCP is unavailable.",
                {"projectPath": args.project},
            )
            sys.exit(1)
        else:
            _print_cli_error(args, "unity_not_running", "Unity is not running (heartbeat not found).")
            sys.exit(1)

    if args.command == "heartbeat":
        fresh = check_heartbeat_fresh(heartbeat)
        heartbeat["fresh"] = fresh
        compile_errors = read_compile_errors(args.project)
        if compile_errors:
            error_entries = [e for e in compile_errors if e.get("level") == "error"]
            if error_entries:
                heartbeat["compileErrors"] = error_entries
                heartbeat["compileErrorCount"] = len(error_entries)
        print(json.dumps(heartbeat, indent=2))
        sys.exit(0 if fresh else 1)

    # Stale/compiling heartbeat recovery
    if not check_heartbeat_fresh(heartbeat) or heartbeat.get("isCompiling"):
        state = "compiling" if heartbeat.get("isCompiling") else "stale"

        # Extension hook for stale heartbeat recovery
        stale_hook = None
        if _ext_module and hasattr(_ext_module, "stale_heartbeat_hook"):
            stale_hook = _ext_module.stale_heartbeat_hook
        if stale_hook:
            recovered_hb = stale_hook(args, project_root, heartbeat, state)
            if recovered_hb:
                heartbeat = recovered_hb
                state = "fresh"

        if state == "fresh":
            pass
        if state == "compiling":
            print("Unity is reloading scripts, waiting...", file=sys.stderr)
        elif state == "stale":
            print("Unity heartbeat is stale, waiting...", file=sys.stderr)
        if state in ("stale", "compiling"):
            if args.command in EDITOR_LOG_FALLBACK_COMMANDS:
                if state == "stale":
                    max_retries = 3
                else:
                    max_retries = 10
            else:
                max_retries = CONNECTION_RETRY_MAX
            should_auto_focus = (
                args.command in ("compile_check", "wait_idle")
                and bool(getattr(args, "auto_focus_on_stall", False))
            )
            auto_focus_attempted = False
            auto_focus_succeeded = False
            focus_interval_ms = max(0, int(getattr(args, "stall_focus_interval_ms", 6000)))
            last_focus_at = -1.0

            def maybe_focus(reason: str, force: bool = False) -> None:
                nonlocal auto_focus_attempted, auto_focus_succeeded, last_focus_at
                if not should_auto_focus:
                    return
                now = time.monotonic()
                if not force and focus_interval_ms > 0 and last_focus_at >= 0:
                    elapsed_ms = (now - last_focus_at) * 1000.0
                    if elapsed_ms < focus_interval_ms:
                        return
                last_focus_at = now
                auto_focus_attempted = True
                if focus_unity_editor(project_root, log_failures=True):
                    auto_focus_succeeded = True
                    print(f"[unitap] Unity focused ({reason}).", file=sys.stderr)

            wait_retry_hook = None
            if should_auto_focus:
                maybe_focus(f"{state} reconnect pre-wait", force=True)

                def wait_retry_hook(attempt_index: int, _max_retries: int, _heartbeat: dict | None) -> None:
                    if attempt_index <= 0:
                        return
                    maybe_focus(f"{state} reconnect retry {attempt_index + 1}/{_max_retries}")

            reconnect_started_at = time.time()
            heartbeat = wait_for_connection(
                args.project,
                max_retries=max_retries,
                require_tcp=True,
                on_wait_retry=wait_retry_hook,
            )
            _append_wait_meta(
                args,
                reason=f"{state}_reconnect",
                started_at=reconnect_started_at,
                recovered=bool(heartbeat),
                max_retries=max_retries,
                auto_focus_attempted=auto_focus_attempted if should_auto_focus else None,
                auto_focus_succeeded=auto_focus_succeeded if should_auto_focus else None,
            )
            if not heartbeat:
                if try_editor_log_fallback(args, f"heartbeat is {state} and TCP is unavailable"):
                    return
                compile_errors = read_compile_errors(args.project)
                if compile_errors:
                    error_entries = [e for e in compile_errors if e.get("level") == "error"]
                    if error_entries:
                        result = {
                            "compiled": True,
                            "hasErrors": True,
                            "errorCount": len(error_entries),
                            "errors": [{"message": e.get("message", ""), "file": e.get("file", ""), "line": e.get("line", 0)} for e in error_entries],
                            "source": "compile-errors.json (TCP unavailable)"
                        }
                        print(json.dumps(result, indent=2, ensure_ascii=False))
                        sys.exit(1)
                process_running = _is_unity_running_for_project(project_root)
                if process_running:
                    _print_cli_error(
                        args,
                        "unity_running_but_unitap_unavailable",
                        "Unity process is running but Unitap did not recover in time.",
                        {"projectPath": args.project, "state": state},
                    )
                else:
                    _print_cli_error(args, "unity_not_running", "Unity is not running.")
                sys.exit(1)

    port = heartbeat["port"]

    # --- Command dispatch ---
    handler = dispatch_table.get(args.command)
    if handler:
        try:
            with lock_context:
                handler(args, port)
        except EditorOperationBusyError as ex:
            _print_cli_error(args, ex.code, ex.message, ex.details)
            sys.exit(1)
        return

    # Fallback: sync command (extensions can override via _sync_command key)
    sync_handler = dispatch_table.get("_sync_command", do_sync_command)
    try:
        with lock_context:
            sync_handler(args, port)
    except EditorOperationBusyError as ex:
        _print_cli_error(args, ex.code, ex.message, ex.details)
        sys.exit(1)
