import json
import os
import sys
import time

from .constants import CONNECTION_RETRY_INTERVAL, DEFAULT_TIMEOUT_MS
from .editor_lock import enrich_diagnose_result_with_editor_lock
from .editor_log import find_project_root, read_compile_errors
from .heartbeat import find_heartbeat, check_heartbeat_fresh
from .image_quality import inspect_capture_image
from .transport import (
    build_request,
    extract_wait_idle_state,
    poll_async_job,
    send_request,
    send_with_retry,
    wait_for_connection,
)
from .unity import (
    build_unity_launch_command,
    clean_recovery_files,
    focus_unity_editor,
    get_unity_editor_path,
    get_unity_version,
    is_unity_process_running,
    kill_unity_processes,
    list_unity_processes,
    list_installed_unity_versions,
)


def send_unitap_sync(args, port: int, command: str, params: dict, timeout_ms: int = DEFAULT_TIMEOUT_MS, retryable: bool = False) -> dict:
    req = build_request(command, params, timeout_ms, retryable)
    return send_with_retry(
        "127.0.0.1",
        port,
        req,
        timeout_s=timeout_ms / 1000 + 5,
        project_path=args.project,
        exit_on_error=True,
    )


def print_unitap_response(args, resp: dict) -> None:
    if resp.get("ok"):
        result = resp.get("result", {})
        if isinstance(result, dict):
            resp = dict(resp)
            resp["result"] = merge_wait_meta_into_result(args, result)

    if args.json:
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return

    if resp.get("ok"):
        result = resp.get("result", {})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    err = resp.get("error", {})
    print(f"Error [{err.get('code', 'unknown')}]: {err.get('message', 'unknown error')}", file=sys.stderr)
    sys.exit(1)


def merge_wait_meta_into_result(args, result):
    wait_meta = getattr(args, "_wait_meta", None)
    if not isinstance(wait_meta, dict):
        return result
    if not isinstance(result, dict):
        return result
    merged = dict(result)
    for key, value in wait_meta.items():
        if key not in merged:
            merged[key] = value
    return merged


def _format_processes(processes: list[dict], limit: int = 5) -> list[dict]:
    output: list[dict] = []
    for item in processes[:limit]:
        output.append({
            "pid": item.get("pid"),
            "projectPath": item.get("projectPath"),
            "command": item.get("command"),
        })
    return output


def do_launch(args) -> None:
    """Unity Editor を起動する"""
    import subprocess

    project_root = find_project_root(args.project)
    if not project_root:
        print(json.dumps({"ok": False, "error": "Unity project not found"}, indent=2))
        sys.exit(1)

    # 1. Unity バージョン取得
    version = get_unity_version(project_root)
    if not version:
        print(json.dumps({"ok": False, "error": "ProjectVersion.txt not found or invalid"}, indent=2))
        sys.exit(1)

    # 2. Editor パス確認
    editor_path = get_unity_editor_path(version)
    if not editor_path:
        installed = list_installed_unity_versions()
        print(json.dumps({
            "ok": False,
            "error": f"Unity {version} is not installed",
            "installedVersions": installed,
        }, indent=2))
        sys.exit(1)

    if args.restart and args.no_kill:
        print(json.dumps({
            "ok": False,
            "error": "--restart and --no-kill cannot be used together.",
            "projectPath": str(project_root),
        }, indent=2))
        sys.exit(1)

    running_same_project = list_unity_processes(project_root)
    running_any = list_unity_processes()

    if running_same_project and not args.restart:
        hb = find_heartbeat(args.project)
        connected = bool(hb and hb.get("port") and check_heartbeat_fresh(hb))
        if not connected and not args.no_wait:
            timeout_s = args.wait_timeout
            max_retries = max(timeout_s // CONNECTION_RETRY_INTERVAL, 1)
            hb = wait_for_connection(args.project, max_retries=max_retries, require_tcp=True)
            connected = bool(hb)

        payload = {
            "ok": True,
            "launched": False,
            "alreadyRunning": True,
            "connected": connected,
            "version": version,
            "projectPath": str(project_root),
            "message": "Unity is already running for this project. Skipped launch to prevent multi-instance startup.",
            "runningProcesses": _format_processes(running_same_project),
        }
        if hb and hb.get("port"):
            payload["port"] = hb.get("port")
        print(json.dumps(payload, indent=2))
        return

    if running_any and not args.restart:
        print(json.dumps({
            "ok": False,
            "error": "Another Unity instance is already running. Launch aborted to prevent multiple Unity instances.",
            "projectPath": str(project_root),
            "runningProcesses": _format_processes(running_any),
        }, indent=2))
        sys.exit(1)

    if args.no_kill and running_any:
        print(json.dumps({
            "ok": False,
            "error": "--no-kill cannot be used while a Unity process is already running.",
            "projectPath": str(project_root),
            "runningProcesses": _format_processes(running_any),
        }, indent=2))
        sys.exit(1)

    # 3. バックアップ削除（Recovery Scene Backups ダイアログ防止）
    removed = clean_recovery_files(project_root)

    # 4. 既存プロセスを kill
    kill_target_project = project_root if args.kill_project_only else None
    if not args.no_kill:
        killed = kill_unity_processes(kill_target_project)
        if killed:
            print(f"Killed Unity processes: {killed}", file=sys.stderr)
            time.sleep(2)
        if is_unity_process_running():
            print(json.dumps({
                "ok": False,
                "error": "Failed to terminate existing Unity process. Launch aborted to avoid multiple Unity instances.",
                "projectPath": str(project_root),
                "runningProcesses": _format_processes(list_unity_processes()),
            }, indent=2))
            sys.exit(1)

    # 5. kill 後にも再掃除（ロック解放後に残るバックアップ対策）
    for p in clean_recovery_files(project_root):
        if p not in removed:
            removed.append(p)
    if removed:
        print(f"Cleaned recovery files: {', '.join(removed)}", file=sys.stderr)

    # 6. 古い heartbeat を削除
    old_hb_path = project_root / "Library" / "Unitap" / ".heartbeat.json"
    if old_hb_path.exists():
        try:
            old_hb_path.unlink()
        except OSError:
            pass

    # 7. Unity 起動
    # Restart flows should ignore compile-error dialogs to prevent headless deadlocks.
    ignore_compiler_errors = bool(args.restart or getattr(args, "ignore_compiler_errors", False))
    cmd = build_unity_launch_command(
        editor_path,
        project_root,
        ignore_compiler_errors=ignore_compiler_errors,
    )
    print(f"Launching Unity {version}...", file=sys.stderr)
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # 8. 起動確認待ち
    if args.no_wait:
        print(json.dumps({
            "ok": True,
            "launched": True,
            "version": version,
            "projectPath": str(project_root),
        }, indent=2))
        return

    timeout_s = args.wait_timeout
    print(f"Waiting for Unity to start (timeout: {timeout_s}s)...", file=sys.stderr)
    max_retries = max(timeout_s // CONNECTION_RETRY_INTERVAL, 1)
    hb = wait_for_connection(args.project, max_retries=max_retries, require_tcp=True)

    if hb:
        print(json.dumps({
            "ok": True,
            "launched": True,
            "connected": True,
            "version": version,
            "projectPath": str(project_root),
            "port": hb.get("port"),
        }, indent=2))
    else:
        # プロセスが存在するかだけ確認
        process_running = is_unity_process_running(project_root)

        print(json.dumps({
            "ok": True,
            "launched": True,
            "connected": False,
            "version": version,
            "projectPath": str(project_root),
            "processRunning": process_running,
            "message": "Unity is starting but Unitap is not yet connected. It may still be loading.",
        }, indent=2))


def do_capture(args, port: int) -> None:
    """capture コマンド: Play mode 自動制御 + ファイルポーリング"""

    def _wait_capture_file_ready(path: str, timeout_seconds: float) -> bool:
        last_size = -1
        stable_count = 0
        max_polls = max(1, int(max(timeout_seconds, 0.1) / 0.1))
        for _ in range(max_polls):
            time.sleep(0.1)
            if os.path.exists(path):
                size = os.path.getsize(path)
                if size > 0:
                    if size == last_size:
                        stable_count += 1
                        if stable_count >= 2:
                            return True
                    else:
                        stable_count = 0
                    last_size = size
        return os.path.exists(path) and os.path.getsize(path) > 0

    def _send_capture_with_retry(target_port: int, capture_params: dict) -> dict:
        req = build_request("capture", capture_params, DEFAULT_TIMEOUT_MS, False)
        return send_with_retry("127.0.0.1", target_port, req, timeout_s=10, project_path=args.project)

    wait_meta = getattr(args, "_wait_meta", None)
    if isinstance(wait_meta, dict) and wait_meta.get("waitedForReconnect"):
        print("[unitap] reconnect直後のため capture 前に wait_idle で安定化します。", file=sys.stderr)
        idle_resp = poll_async_job(
            "127.0.0.1",
            port,
            "wait_idle",
            {"timeoutMs": 20000},
            20000,
            args.project,
        )
        if not idle_resp.get("ok"):
            print("[unitap] wait_idle failed after reconnect; capture を継続します。", file=sys.stderr)
        elif idle_resp.get("result", {}).get("timedOut"):
            print("[unitap] wait_idle timed out after reconnect; capture を継続します。", file=sys.stderr)
        time.sleep(0.35)

    output_path = args.output
    params = {"outputPath": output_path, "superSize": args.superSize}

    try:
        resp = _send_capture_with_retry(port, params)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not resp.get("ok"):
        err = resp.get("error", {})
        print(f"Error: {err.get('message', 'unknown error')}", file=sys.stderr)
        sys.exit(1)

    result = resp.get("result", {})

    # Play mode でない場合: 自動 play → isPlaying確認 → リトライ
    if result.get("isPlaying") is False:
        print("Play mode required, starting Play mode...", file=sys.stderr)
        try:
            play_req = build_request("play", {}, 10000, False)
            send_request("127.0.0.1", port, play_req, timeout_s=15)
        except Exception:
            pass
        hb = wait_for_connection(args.project, require_tcp=True)
        if not hb:
            print("Error: Unity did not recover after entering Play mode", file=sys.stderr)
            sys.exit(1)
        port = hb["port"]

        # isPlaying=true を確認してからキャプチャ
        for _ in range(15):
            time.sleep(1)
            try:
                status_req = build_request("status", {}, 5000)
                status_resp = send_request("127.0.0.1", port, status_req, timeout_s=5)
                if status_resp.get("ok") and status_resp.get("result", {}).get("isPlaying"):
                    break
            except Exception:
                pass

        resp = _send_capture_with_retry(port, params)
        if not resp.get("ok"):
            err = resp.get("error", {})
            print(f"Error: {err.get('message', 'capture failed')}", file=sys.stderr)
            sys.exit(1)
        result = resp.get("result", {})

    if not result.get("requested"):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(1)

    success = _wait_capture_file_ready(output_path, args.timeout)
    out = {"outputPath": output_path, "success": success, "retryCount": 0}
    if not success:
        out["error"] = "File write timeout"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    first_quality = inspect_capture_image(output_path)
    out["quality"] = first_quality
    should_retry_anomaly = first_quality.get("ok") and first_quality.get("isAnomaly")
    if should_retry_anomaly:
        reasons = first_quality.get("anomalyReasons") or ["unknown"]
        print(f"[unitap] capture anomaly detected: {', '.join(reasons)}. retrying once...", file=sys.stderr)
        time.sleep(0.35)
        try:
            retry_resp = _send_capture_with_retry(port, params)
        except Exception as ex:
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = f"Retry capture failed: {ex}"
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return
        if not retry_resp.get("ok"):
            err = retry_resp.get("error", {})
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = f"Retry capture failed: {err.get('message', 'capture failed')}"
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return

        retry_result = retry_resp.get("result", {})
        if not retry_result.get("requested"):
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = "Retry capture request was not accepted"
            out["retryResult"] = retry_result
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return

        retry_success = _wait_capture_file_ready(output_path, args.timeout)
        out["retryCount"] = 1
        out["retrySuccess"] = retry_success
        if not retry_success:
            out["success"] = False
            out["error"] = "Retry file write timeout"
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return

        final_quality = inspect_capture_image(output_path)
        out["qualityInitial"] = first_quality
        out["quality"] = final_quality
        if final_quality.get("ok") and final_quality.get("isAnomaly"):
            out["success"] = False
            out["error"] = "Capture anomaly detected after retry"
            out["anomalyReasons"] = final_quality.get("anomalyReasons")

    print(json.dumps(out, indent=2, ensure_ascii=False))


def do_capture_editor(args, port: int) -> None:
    """capture_editor コマンド: 任意のEditorWindowをキャプチャ"""
    params = {
        "outputPath": args.output,
        "index": max(args.index, 0),
        "focus": not args.no_focus,
        "openIfMissing": not args.no_open,
    }

    if args.window:
        params["window"] = args.window
    if args.window_title:
        params["windowTitle"] = args.window_title
    if args.window_type:
        params["windowType"] = args.window_type
    if args.menu_path:
        params["menuPath"] = args.menu_path

    resp = send_unitap_sync(
        args,
        port,
        "tool_exec",
        {"tool": "capture_editor_window", "params": params},
        timeout_ms=15000,
        retryable=False,
    )
    print_unitap_response(args, resp)


def do_focus(args) -> None:
    project_root = find_project_root(args.project)
    focused = focus_unity_editor(project_root, log_failures=True)
    if focused:
        print_unitap_response(args, {"ok": True, "result": {"focused": True}})
        return

    print_unitap_response(args, {
        "ok": False,
        "error": {
            "code": "focus_failed",
            "message": "Unity の前面化に失敗しました。Unityが起動中か確認してください。",
        },
    })


def do_wait_idle(args, port: int) -> None:
    timeout_val = args.timeout
    params = {"timeoutMs": timeout_val}
    resp = poll_async_job("127.0.0.1", port, "wait_idle", params, timeout_val, args.project)
    if not resp.get("ok"):
        print_unitap_response(args, resp)
        return

    result = dict(resp.get("result", {}))
    auto_focus_attempted = False
    auto_focus_succeeded = False
    should_auto_focus = bool(getattr(args, "auto_focus_on_stall", False))
    compile_stalled = bool(result.get("timedOut")) and (
        bool(result.get("isCompiling")) or bool(result.get("isUpdating"))
    )

    if compile_stalled and should_auto_focus:
        auto_focus_attempted = True
        auto_focus_succeeded = _try_focus_for_compile(args, "wait_idle retry after timeout")
        if auto_focus_succeeded:
            reconnect_max_retries = max(3, int(timeout_val / 1000 / CONNECTION_RETRY_INTERVAL) + 3)
            hb = wait_for_connection(
                args.project,
                max_retries=reconnect_max_retries,
                require_tcp=True,
            )
            if hb and hb.get("port"):
                retry_resp = poll_async_job(
                    "127.0.0.1",
                    int(hb["port"]),
                    "wait_idle",
                    params,
                    timeout_val,
                    args.project,
                )
                if not retry_resp.get("ok"):
                    print_unitap_response(args, retry_resp)
                    return
                result = dict(retry_resp.get("result", {}))

    result["autoFocusAttempted"] = auto_focus_attempted
    result["autoFocusSucceeded"] = auto_focus_succeeded

    if bool(result.get("timedOut")) and (bool(result.get("isCompiling")) or bool(result.get("isUpdating"))):
        print_unitap_response(
            args,
            {
                "ok": False,
                "error": {
                    "code": "compile_stalled_background",
                    "message": "wait_idle timed out while Unity remained compiling in background.",
                    "details": merge_wait_meta_into_result(args, result),
                },
            },
        )
        return

    print_unitap_response(args, {"ok": True, "result": result})


def _inspect_fsm_state(args, port: int, game_object: str, fsm_name: str, timeout_ms: int = 5000) -> dict:
    return send_unitap_sync(
        args,
        port,
        "tool_exec",
        {
            "tool": "inspect_fsm_state",
            "params": {
                "action": "state",
                "gameObject": game_object,
                "fsmName": fsm_name,
            },
        },
        timeout_ms=max(500, int(timeout_ms)),
        retryable=False,
    )


def wait_for_fsm_state(
    args,
    port: int,
    game_object: str,
    fsm_name: str,
    target_state: str,
    timeout_seconds: float = 20.0,
    poll_interval_seconds: float = 0.2,
) -> dict:
    timeout_seconds = max(float(timeout_seconds), 0.1)
    poll_interval_seconds = max(float(poll_interval_seconds), 0.05)
    started = time.time()
    deadline = started + timeout_seconds
    poll_count = 0
    last_active_state = None
    last_response = None
    last_error = None

    while True:
        poll_count += 1
        remaining_ms = max(int((deadline - time.time()) * 1000), 0)
        request_timeout_ms = min(5000, max(remaining_ms + 300, 700))
        try:
            resp = _inspect_fsm_state(args, port, game_object, fsm_name, timeout_ms=request_timeout_ms)
            if not resp.get("ok"):
                last_error = resp.get("error", {}).get("message", "inspect_fsm_state failed")
            else:
                tool_result = resp.get("result", {})
                if isinstance(tool_result, dict):
                    last_response = tool_result
                    data = tool_result.get("data", {})
                    if isinstance(data, dict):
                        last_active_state = data.get("activeState")
                        if last_active_state == target_state:
                            elapsed = time.time() - started
                            return {
                                "matched": True,
                                "timedOut": False,
                                "targetState": target_state,
                                "lastActiveState": last_active_state,
                                "gameObject": game_object,
                                "fsmName": fsm_name,
                                "pollCount": poll_count,
                                "elapsedSeconds": round(elapsed, 3),
                                "currentElapsedSeconds": round(elapsed, 3),
                                "fsmResult": tool_result,
                            }
        except Exception as ex:
            last_error = str(ex)

        now = time.time()
        if now >= deadline:
            elapsed = now - started
            result = {
                "matched": False,
                "timedOut": True,
                "targetState": target_state,
                "lastActiveState": last_active_state,
                "gameObject": game_object,
                "fsmName": fsm_name,
                "pollCount": poll_count,
                "elapsedSeconds": round(elapsed, 3),
                "currentElapsedSeconds": round(elapsed, 3),
            }
            if last_response is not None:
                result["fsmResult"] = last_response
            if last_error:
                result["lastError"] = last_error
            return result

        time.sleep(poll_interval_seconds)


def do_wait_fsm(args, port: int) -> None:
    result = wait_for_fsm_state(
        args,
        port,
        args.gameObject,
        args.fsmName,
        args.state,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
    )
    result = merge_wait_meta_into_result(args, result)
    print_unitap_response(args, {"ok": True, "result": result})
    if not result.get("matched"):
        sys.exit(1)


def do_play(args, port: int) -> None:
    output: dict = {}

    if getattr(args, "wait_idle_first", False):
        idle_timeout = max(0, int(args.idle_timeout))
        idle_resp = poll_async_job(
            "127.0.0.1",
            port,
            "wait_idle",
            {"timeoutMs": idle_timeout},
            idle_timeout,
            args.project,
        )
        if not idle_resp.get("ok"):
            print_unitap_response(args, idle_resp)
            return
        idle_result = idle_resp.get("result", {})
        output["waitIdleFirst"] = idle_result
        if idle_result.get("timedOut"):
            message = "wait_idle timed out before play."
            if getattr(args, "wait_idle_required", False):
                print_unitap_response(
                    args,
                    {
                        "ok": False,
                        "error": {
                            "code": "wait_idle_timeout",
                            "message": message,
                        },
                    },
                )
                return
            print(f"[unitap] {message} Continuing play because --wait-idle-required is not set.", file=sys.stderr)

    play_resp = send_unitap_sync(
        args,
        port,
        "play",
        {},
        timeout_ms=10000,
        retryable=False,
    )
    if not play_resp.get("ok"):
        print_unitap_response(args, play_resp)
        return

    play_result = play_resp.get("result", {})
    if isinstance(play_result, dict):
        output.update(play_result)
    else:
        output["play"] = play_result

    wait_fsm_enabled = bool(
        getattr(args, "wait_fsm_gameobject", None)
        and getattr(args, "wait_fsm_name", None)
        and getattr(args, "wait_fsm_state", None)
    )
    if wait_fsm_enabled:
        wait_fsm_result = wait_for_fsm_state(
            args,
            port,
            args.wait_fsm_gameobject,
            args.wait_fsm_name,
            args.wait_fsm_state,
            timeout_seconds=args.wait_fsm_timeout,
            poll_interval_seconds=args.wait_fsm_poll,
        )
        output["waitFsm"] = wait_fsm_result
        if not wait_fsm_result.get("matched") and getattr(args, "wait_fsm_required", False):
            print_unitap_response(
                args,
                {
                    "ok": False,
                    "error": {
                        "code": "wait_fsm_timeout",
                        "message": "FSM wait timed out after play.",
                    },
                },
            )
            return

    if getattr(args, "capture_output", None):
        capture_resp = send_unitap_sync(
            args,
            port,
            "tool_exec",
            {
                "tool": "capture_gameview",
                "params": {
                    "outputPath": args.capture_output,
                    "superSize": max(1, int(args.capture_supersize)),
                },
            },
            timeout_ms=30000,
            retryable=False,
        )
        if not capture_resp.get("ok"):
            print_unitap_response(args, capture_resp)
            return
        output["capture"] = capture_resp.get("result", {})

    output = merge_wait_meta_into_result(args, output)
    print_unitap_response(args, {"ok": True, "result": output})


def _normalize_compile_check_result(raw_result: dict, project_path: str | None) -> dict:
    result = dict(raw_result or {})
    is_compiling, is_updating = extract_wait_idle_state(result, project_path)
    result.setdefault("isCompiling", is_compiling)
    result.setdefault("isUpdating", is_updating)
    result.setdefault(
        "compileStarted",
        bool(result.get("isCompiling")) or bool(result.get("isUpdating")),
    )
    result.setdefault("compileStartObservedAtMs", None)
    result.setdefault("idle", (not result["isCompiling"]) and (not result["isUpdating"]))
    result.setdefault("status", "completed")
    if "compiled" not in result:
        result["compiled"] = bool(result.get("idle")) and not bool(result.get("timedOut"))
    elif result.get("timedOut") and result.get("compiled"):
        # timedOut=true の場合は compiled=false に矯正（フォールバック由来の不整合防止）
        result["compiled"] = False
    return result


def _try_focus_for_compile(args, reason: str) -> bool:
    project_root = find_project_root(args.project)
    focused = focus_unity_editor(project_root, log_failures=True)
    if focused:
        print(f"[unitap] Unity focused ({reason}).", file=sys.stderr)
        wait_ms = max(0, int(getattr(args, "focus_wait_ms", 350)))
        if wait_ms > 0:
            time.sleep(wait_ms / 1000.0)
        return True

    print(f"[unitap] Unity focus failed ({reason}).", file=sys.stderr)
    return False


def _compile_stall_reasons(result: dict) -> list[str]:
    reasons: list[str] = []
    if bool(result.get("timedOut")) and (bool(result.get("isCompiling")) or bool(result.get("isUpdating"))):
        reasons.append("timed_out_while_compiling")
    if result.get("compileStarted") is False:
        reasons.append("compile_not_started")
    return reasons


def _attach_focus_meta(result: dict, attempted: bool, succeeded: bool) -> dict:
    merged = dict(result or {})
    merged["autoFocusAttempted"] = bool(attempted)
    merged["autoFocusSucceeded"] = bool(succeeded)
    return merged


def do_compile_check(args, port: int) -> None:
    max_retries = max(0, getattr(args, "max_retries", 3))
    auto_focus_on_stall = bool(getattr(args, "auto_focus_on_stall", False))
    focus_attempted = False
    focus_succeeded = False

    def attempt_focus(reason: str) -> bool:
        nonlocal focus_attempted, focus_succeeded
        focus_attempted = True
        focused = _try_focus_for_compile(args, reason)
        if focused:
            focus_succeeded = True
        return focused

    if getattr(args, "focus_unity", False):
        attempt_focus("before compile_check")

    result: dict = {}
    for attempt in range(1 + max_retries):
        resp = poll_async_job(
            "127.0.0.1",
            port,
            "compile_check",
            {"timeoutMs": args.timeout},
            args.timeout,
            args.project,
        )
        if not resp.get("ok"):
            print_unitap_response(args, resp)
            return

        result = _normalize_compile_check_result(resp.get("result", {}), args.project)
        stall_reasons = _compile_stall_reasons(result)

        # コンパイル完了 or エラーあり → 即返却
        if not stall_reasons:
            break
        if result.get("hasErrors") or result.get("errorCount", 0) > 0:
            break

        # stall 発生時のリトライ
        remaining = max_retries - attempt
        if remaining <= 0:
            break

        if auto_focus_on_stall:
            if "compile_not_started" in stall_reasons:
                attempt_focus("compile did not start; retrying with focus")
            elif "timed_out_while_compiling" in stall_reasons:
                attempt_focus("compile timed out while compiling; retrying with focus")

        print(
            f"[unitap] compile_check stalled ({','.join(stall_reasons)}), retrying ({attempt + 1}/{max_retries})...",
            file=sys.stderr,
        )

    result = _attach_focus_meta(result, focus_attempted, focus_succeeded)
    unresolved_stall_reasons = _compile_stall_reasons(result)
    if unresolved_stall_reasons and not (result.get("hasErrors") or result.get("errorCount", 0) > 0):
        code = "compile_stalled_background"
        message = "compile_check timed out while Unity remained compiling in background."
        if "compile_not_started" in unresolved_stall_reasons:
            code = "compile_not_started"
            message = "compile_check could not confirm script compilation start."
        print_unitap_response(
            args,
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": merge_wait_meta_into_result(args, result),
                },
            },
        )
        return

    print_unitap_response(args, {"ok": True, "result": result})


def do_sync_command(args, port: int) -> None:
    """Synchronous command dispatcher for simple request-response commands."""
    params = {}
    retryable = True

    if args.command == "execute_menu":
        params = {"menuPath": args.menuPath}
        retryable = False
    elif args.command == "read_console":
        params = {"limit": args.limit}
        if args.type:
            params["type"] = args.type
        if args.since_last_clear:
            params["sinceLastClear"] = True
        if args.since:
            params["since"] = args.since
    elif args.command == "tool_exec":
        try:
            tool_params = json.loads(args.params)
        except (json.JSONDecodeError, TypeError):
            print("Error: Invalid JSON in --params", file=sys.stderr)
            sys.exit(1)
        params = {"tool": args.tool, "params": tool_params}
        retryable = False
    elif args.command == "save_scene":
        params = {"all": args.all}
        retryable = False

    timeout_ms = DEFAULT_TIMEOUT_MS
    req = build_request(args.command, params, timeout_ms, retryable)

    try:
        resp = send_with_retry(
            "127.0.0.1",
            port,
            req,
            timeout_s=timeout_ms / 1000 + 5,
            project_path=args.project,
            exit_on_error=True,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if isinstance(resp, dict) and resp.get("ok"):
        result = resp.get("result", {})
        if isinstance(result, dict):
            if args.command == "diagnose":
                project_root = find_project_root(args.project)
                result = enrich_diagnose_result_with_editor_lock(project_root, result)
            resp["result"] = merge_wait_meta_into_result(args, result)
    print_unitap_response(args, resp)
