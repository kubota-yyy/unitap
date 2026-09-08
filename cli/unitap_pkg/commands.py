import json
import os
import socket
import sys
import time

from .constants import (
    CONNECTION_RETRY_INTERVAL,
    CONNECTION_RETRY_MAX,
    DEFAULT_TIMEOUT_MS,
    POLL_INTERVAL,
)
from .editor_lock import enrich_diagnose_result_with_editor_lock
from .editor_log import find_project_root, read_compile_errors, summarize_project_editor_activity
from .heartbeat import find_heartbeat, check_heartbeat_fresh
from .image_quality import inspect_capture_image
from .runtime_diagnostics import classify_unity_unavailability
from .transport import (
    build_request,
    extract_wait_idle_state,
    poll_async_job,
    send_request_to_current_transport,
    send_with_retry,
    wait_for_connection,
)
from .unity import (
    build_unity_launch_environment,
    build_unity_launch_command,
    clean_recovery_files,
    dismiss_safe_mode_dialog_async,
    focus_unity_editor,
    get_unity_editor_path,
    get_expected_build_target,
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
    setattr(args, "_last_unitap_response", resp)
    if resp.get("ok"):
        result = resp.get("result", {})
        if isinstance(result, dict):
            resp = dict(resp)
            resp["result"] = merge_wait_meta_into_result(args, result)
            setattr(args, "_last_unitap_response", resp)

    if args.json:
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        if not resp.get("ok"):
            sys.exit(1)
        return

    if resp.get("ok"):
        result = resp.get("result", {})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    err = resp.get("error", {})
    print(f"Error [{err.get('code', 'unknown')}]: {err.get('message', 'unknown error')}", file=sys.stderr)
    details = err.get("details") if isinstance(err, dict) else None
    if isinstance(details, dict):
        suggestions = details.get("didYouMean")
        if isinstance(suggestions, list) and suggestions:
            print(f"  did you mean: {', '.join(str(s) for s in suggestions)}", file=sys.stderr)
        hint = details.get("hint")
        if isinstance(hint, str) and hint:
            print(f"  hint: {hint}", file=sys.stderr)
    sys.exit(1)


def merge_transport_fields(payload: dict, heartbeat: dict | None) -> dict:
    if not isinstance(payload, dict) or not isinstance(heartbeat, dict):
        return payload

    merged = dict(payload)
    for key in ("transportKind", "host", "port", "pipeName", "pipeSocketPath", "fileTransportDirectory"):
        if key not in merged and heartbeat.get(key) is not None:
            merged[key] = heartbeat.get(key)
    return merged


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


def _build_unavailability_payload(project_path: str | None, process_running: bool, *, state: str | None = None) -> dict:
    diagnosis = classify_unity_unavailability(
        process_running,
        summarize_project_editor_activity(project_path),
        state=state,
    )
    payload = {
        "ok": False,
        "error": {
            "code": diagnosis["code"],
            "message": diagnosis["message"],
        },
    }
    details = dict(diagnosis.get("details") or {})
    if project_path:
        details.setdefault("projectPath", project_path)
    if details:
        payload["error"]["details"] = details
    return payload


def do_launch(args) -> None:
    """Unity Editor を起動する"""
    import subprocess

    def emit(payload: dict, *, exit_code: int | None = None) -> None:
        setattr(args, "_last_unitap_response", payload)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if exit_code is not None:
            sys.exit(exit_code)

    project_root = find_project_root(args.project)
    if not project_root:
        emit({"ok": False, "error": {"code": "project_not_found", "message": "Unity project not found"}}, exit_code=1)

    # 1. Unity バージョン取得
    version = get_unity_version(project_root)
    if not version:
        emit(
            {"ok": False, "error": {"code": "invalid_project_version", "message": "ProjectVersion.txt not found or invalid"}},
            exit_code=1,
        )

    # 2. Editor パス確認
    editor_path = get_unity_editor_path(version)
    if not editor_path:
        installed = list_installed_unity_versions()
        emit({
            "ok": False,
            "error": {"code": "unity_not_installed", "message": f"Unity {version} is not installed"},
            "installedVersions": installed,
        }, exit_code=1)

    if args.restart and args.no_kill:
        emit({
            "ok": False,
            "error": {"code": "invalid_launch_args", "message": "--restart and --no-kill cannot be used together."},
            "projectPath": str(project_root),
        }, exit_code=1)

    running_same_project = list_unity_processes(project_root)
    running_any = list_unity_processes()

    if running_same_project and not args.restart:
        hb = find_heartbeat(args.project)
        connected = bool(hb and check_heartbeat_fresh(hb))
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
        payload = merge_transport_fields(payload, hb)
        emit(payload)
        return

    if running_any and not args.restart:
        emit({
            "ok": False,
            "error": {
                "code": "unity_already_running",
                "message": "Another Unity instance is already running. Launch aborted to prevent multiple Unity instances.",
            },
            "projectPath": str(project_root),
            "runningProcesses": _format_processes(running_any),
        }, exit_code=1)

    if args.no_kill and running_any:
        emit({
            "ok": False,
            "error": {
                "code": "invalid_launch_args",
                "message": "--no-kill cannot be used while a Unity process is already running.",
            },
            "projectPath": str(project_root),
            "runningProcesses": _format_processes(running_any),
        }, exit_code=1)

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
            emit({
                "ok": False,
                "error": {
                    "code": "unity_kill_failed",
                    "message": "Failed to terminate existing Unity process. Launch aborted to avoid multiple Unity instances.",
                },
                "projectPath": str(project_root),
                "runningProcesses": _format_processes(list_unity_processes()),
            }, exit_code=1)

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
    # Unity 6 では -ignoreCompilerErrors だけでは "Enter Safe Mode?" を抑制できない
    # ケースが残るため、デフォルトで CLI フラグを付与しつつ AppleScript dismisser も併用する。
    # オプトアウトしたい場合は --no-ignore-compiler-errors を指定する。
    ignore_compiler_errors = not getattr(args, "no_ignore_compiler_errors", False)
    expected_build_target = get_expected_build_target(project_root)
    cmd = build_unity_launch_command(
        editor_path,
        project_root,
        ignore_compiler_errors=ignore_compiler_errors,
        build_target=expected_build_target,
    )
    print(f"Launching Unity {version}...", file=sys.stderr)
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=build_unity_launch_environment(),
        start_new_session=True,
    )

    # 7.5 起動時ダイアログ自動処理 (macOS / Accessibility 権限が必要)
    if ignore_compiler_errors:
        dismiss_safe_mode_dialog_async(timeout_seconds=120)

    # 8. 起動確認待ち
    if args.no_wait:
        emit({
            "ok": True,
            "launched": True,
            "version": version,
            "projectPath": str(project_root),
            "expectedBuildTarget": expected_build_target,
        })
        return

    timeout_s = args.wait_timeout
    print(f"Waiting for Unity to start (timeout: {timeout_s}s)...", file=sys.stderr)
    max_retries = max(timeout_s // CONNECTION_RETRY_INTERVAL, 1)
    hb = wait_for_connection(args.project, max_retries=max_retries, require_tcp=True)

    if hb:
        emit(merge_transport_fields({
            "ok": True,
            "launched": True,
            "connected": True,
            "version": version,
            "projectPath": str(project_root),
            "port": hb.get("port"),
            "expectedBuildTarget": expected_build_target,
        }, hb))
    else:
        # プロセスが存在するかだけ確認
        process_running = is_unity_process_running(project_root)
        editor_activity = summarize_project_editor_activity(args.project)
        if process_running and (not editor_activity or editor_activity.get("recentActivity") is not False):
            emit(merge_transport_fields({
                "ok": True,
                "launched": True,
                "connected": False,
                "version": version,
                "projectPath": str(project_root),
                "processRunning": process_running,
                "expectedBuildTarget": expected_build_target,
                "message": "Unity is starting but Unitap is not yet connected. It may still be loading.",
            }, find_heartbeat(args.project)))
            return

        payload = _build_unavailability_payload(str(project_root), process_running, state="launch_timeout")
        payload["version"] = version
        payload["projectPath"] = str(project_root)
        payload["launched"] = True
        payload["connected"] = False
        payload["processRunning"] = process_running
        emit(payload, exit_code=1)


def do_capture(args, port: int) -> None:
    """capture コマンド: Play mode 自動制御 + ファイルポーリング"""

    def _remove_existing_capture_file(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _set_capture_response(ok: bool, payload: dict) -> None:
        if ok:
            setattr(args, "_last_unitap_response", {"ok": True, "result": payload})
            return
        error = {
            "code": payload.get("errorCode", "capture_failed"),
            "message": payload.get("error", "Capture failed"),
            "details": payload,
        }
        setattr(args, "_last_unitap_response", {"ok": False, "error": error})

    def _exit_capture_failure(payload: dict) -> None:
        _set_capture_response(False, payload)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.exit(1)

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
        _remove_existing_capture_file(output_path)
        resp = _send_capture_with_retry(port, params)
    except Exception as e:
        setattr(args, "_last_unitap_response", {
            "ok": False,
            "error": {
                "code": "capture_request_failed",
                "message": str(e),
            },
        })
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not resp.get("ok"):
        raw_err = resp.get("error", {})
        err = raw_err if isinstance(raw_err, dict) else {}
        message = err.get("message", "unknown error")
        setattr(args, "_last_unitap_response", {
            "ok": False,
            "error": {
                "code": err.get("code", "capture_request_failed"),
                "message": message,
                "details": err,
            },
        })
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)

    result = resp.get("result", {})

    # Play mode でない場合: 自動 play → isPlaying確認 → リトライ
    if result.get("isPlaying") is False:
        print("Play mode required, starting Play mode...", file=sys.stderr)
        try:
            play_req = build_request("play", {}, 10000, False)
            send_request_to_current_transport(
                args.project,
                play_req,
                timeout_s=15,
                fallback_port=port,
            )
        except Exception:
            pass
        hb = wait_for_connection(args.project, require_tcp=True)
        if not hb:
            setattr(args, "_last_unitap_response", {
                "ok": False,
                "error": {
                    "code": "unity_reconnect_failed",
                    "message": "Unity did not recover after entering Play mode",
                },
            })
            print("Error: Unity did not recover after entering Play mode", file=sys.stderr)
            sys.exit(1)
        port = int(hb.get("port", port))

        # isPlaying=true を確認してからキャプチャ
        for _ in range(15):
            time.sleep(1)
            try:
                status_req = build_request("status", {}, 5000)
                status_resp = send_request_to_current_transport(
                    args.project,
                    status_req,
                    timeout_s=5,
                    fallback_port=port,
                )
                if status_resp.get("ok") and status_resp.get("result", {}).get("isPlaying"):
                    break
            except Exception:
                pass

        _remove_existing_capture_file(output_path)
        resp = _send_capture_with_retry(port, params)
        if not resp.get("ok"):
            raw_err = resp.get("error", {})
            err = raw_err if isinstance(raw_err, dict) else {}
            message = err.get("message", "capture failed")
            setattr(args, "_last_unitap_response", {
                "ok": False,
                "error": {
                    "code": err.get("code", "capture_request_failed"),
                    "message": message,
                    "details": err,
                },
            })
            print(f"Error: {message}", file=sys.stderr)
            sys.exit(1)
        result = resp.get("result", {})

    if not result.get("requested"):
        result = dict(result)
        result["errorCode"] = "capture_not_requested"
        result.setdefault("error", "Capture request was not accepted")
        _exit_capture_failure(result)

    success = _wait_capture_file_ready(output_path, args.timeout)
    out = {"outputPath": output_path, "success": success, "retryCount": 0}
    if not success:
        out["error"] = "File write timeout"
        out["errorCode"] = "capture_file_timeout"
        _exit_capture_failure(out)

    first_quality = inspect_capture_image(output_path)
    out["quality"] = first_quality
    should_retry_anomaly = first_quality.get("ok") and first_quality.get("isAnomaly")
    if should_retry_anomaly:
        reasons = first_quality.get("anomalyReasons") or ["unknown"]
        print(f"[unitap] capture anomaly detected: {', '.join(reasons)}. retrying once...", file=sys.stderr)
        time.sleep(0.35)
        try:
            _remove_existing_capture_file(output_path)
            retry_resp = _send_capture_with_retry(port, params)
        except Exception as ex:
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = f"Retry capture failed: {ex}"
            out["errorCode"] = "capture_retry_failed"
            _exit_capture_failure(out)
        if not retry_resp.get("ok"):
            raw_err = retry_resp.get("error", {})
            err = raw_err if isinstance(raw_err, dict) else {}
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = f"Retry capture failed: {err.get('message', 'capture failed')}"
            out["errorCode"] = "capture_retry_failed"
            _exit_capture_failure(out)

        retry_result = retry_resp.get("result", {})
        if not retry_result.get("requested"):
            out["success"] = False
            out["retryCount"] = 1
            out["error"] = "Retry capture request was not accepted"
            out["errorCode"] = "capture_retry_not_requested"
            out["retryResult"] = retry_result
            _exit_capture_failure(out)

        retry_success = _wait_capture_file_ready(output_path, args.timeout)
        out["retryCount"] = 1
        out["retrySuccess"] = retry_success
        if not retry_success:
            out["success"] = False
            out["error"] = "Retry file write timeout"
            out["errorCode"] = "capture_retry_file_timeout"
            _exit_capture_failure(out)

        final_quality = inspect_capture_image(output_path)
        out["qualityInitial"] = first_quality
        out["quality"] = final_quality
        if final_quality.get("ok") and final_quality.get("isAnomaly"):
            out["success"] = False
            out["error"] = "Capture anomaly detected after retry"
            out["errorCode"] = "capture_anomaly"
            out["anomalyReasons"] = final_quality.get("anomalyReasons")
            _exit_capture_failure(out)

    _set_capture_response(True, out)
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
            if hb:
                retry_resp = poll_async_job(
                    "127.0.0.1",
                    int(hb.get("port", port)),
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
    if result.get("hasErrors") or int(result.get("errorCount") or 0) > 0:
        print_unitap_response(
            args,
            {
                "ok": False,
                "error": {
                    "code": "compile_errors",
                    "message": "compile_check completed with compiler errors.",
                    "details": merge_wait_meta_into_result(args, result),
                },
            },
        )
        return

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


# Menu paths that open native (modal) OS dialogs. These hang Unity for
# automation because the dialog blocks the main thread until a human closes
# it. Block them in the CLI and suggest safer alternatives.
BLOCKED_EXECUTE_MENU_PATHS = {
    "File/New Scene": "tool_exec --tool open_scene --params '{\"scenePath\": \"...\"}' を使う",
    "File/Open Scene": "tool_exec --tool open_scene --params '{\"path\": \"Assets/...unity\"}' を使う",
    "File/Open Scene Additive": "tool_exec --tool open_scene --params '{\"path\": \"...\", \"additive\": true}' を使う",
    "File/Save As...": "save_scene コマンドを使う",
    "File/Save As": "save_scene コマンドを使う",
    "File/Save Scene As": "save_scene コマンドを使う",
    "File/Save Scene As...": "save_scene コマンドを使う",
    "File/Save Project": "save_scene --all を使う",
    "File/Build And Run": "ビルドは CLI/CI 経由で実行し、対話的ビルドは避ける",
    "File/Build Settings...": "Build 系 window は CLI からは開かない",
    "Assets/Import New Asset...": "直接ファイルを Assets/ にコピーして refresh を呼ぶ",
    "Assets/Export Package...": "ExportPackage API を tool_exec 経由で呼ぶ",
}


TOOL_EXEC_TOP_LEVEL_ALIASES = {
    "clear_console": "clear_console",
    "list_tools": "tool_list",
    "execute_menu": "execute_menu --menuPath <path>",
    "manage_editor": "play / stop / launch のいずれか",
}

LONG_RUNNING_TOOL_TIMEOUTS_MS = {
    "export_sprite_atlas": 180000,
}


def _check_execute_menu_blocklist(menu_path: str) -> None:
    if not menu_path:
        return
    normalized = menu_path.strip()
    # exact match + endswith "..." variants
    for blocked, alt in BLOCKED_EXECUTE_MENU_PATHS.items():
        if normalized == blocked or normalized == blocked + "...":
            print(
                f"Error: execute_menu は '{menu_path}' を拒否しました。"
                f"ネイティブファイルダイアログが開いて Unity が固まります。"
                f"代わりに: {alt}",
                file=sys.stderr,
            )
            sys.exit(2)


def _result_is_pending(resp: dict) -> bool:
    if not isinstance(resp, dict) or not resp.get("ok"):
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("_mcp_status") == "pending"


def _mark_pending_tool_exec_response(resp: dict) -> dict:
    if not _result_is_pending(resp):
        return resp

    marked = dict(resp)
    result = dict(marked.get("result") or {})
    result.setdefault("rawSuccess", result.get("success"))
    result["pending"] = True
    result["success"] = False
    if not result.get("message"):
        result["message"] = "tool_exec is still pending. Re-run with --wait or poll action=status."
    marked["result"] = result
    return marked


def _result_is_terminal_complete(resp: dict) -> bool:
    if not isinstance(resp, dict) or not resp.get("ok"):
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False
    status = result.get("_mcp_status")
    # complete (success), error (terminal failure), or no _mcp_status (plain result) all stop polling.
    return status in (None, "complete", "error", "completed", "failed")


def poll_pending_tool_exec(
    args,
    port: int,
    tool_name: str,
    initial_resp: dict,
    request_timeout_ms: int,
) -> dict:
    """run_automate_test 等の PendingResponse を action=status で完了まで poll する。

    sync 側で完結させることで host LLM が status をループ呼びする必要をなくす。
    """
    if not _result_is_pending(initial_resp):
        return initial_resp

    wait_timeout_s = max(5.0, float(getattr(args, "wait_timeout", 1800) or 1800))
    poll_override = float(getattr(args, "wait_poll_interval", 0.0) or 0.0)
    deadline = time.time() + wait_timeout_s
    current_port = port
    resp = initial_resp

    request_timeout_ms = max(5000, min(int(request_timeout_ms), 60000))
    request_timeout_s = request_timeout_ms / 1000.0 + 5.0
    status_params = {"tool": tool_name, "params": {"action": "status"}}

    consecutive_conn_errors = 0

    while time.time() < deadline:
        if _result_is_terminal_complete(resp):
            return resp

        result = resp.get("result", {}) if isinstance(resp, dict) else {}
        if poll_override > 0:
            interval = max(0.5, poll_override)
        else:
            try:
                interval = float(result.get("_mcp_poll_interval") or POLL_INTERVAL)
            except (TypeError, ValueError):
                interval = POLL_INTERVAL
            interval = max(0.5, interval)
        time.sleep(interval)

        poll_req = build_request("tool_exec", status_params, request_timeout_ms, False)
        try:
            resp = send_request_to_current_transport(
                args.project,
                poll_req,
                timeout_s=request_timeout_s,
                fallback_host="127.0.0.1",
                fallback_port=current_port,
            )
            consecutive_conn_errors = 0
        except (ConnectionRefusedError, ConnectionError, ConnectionResetError, socket.timeout) as ex:
            consecutive_conn_errors += 1
            print(f"[unitap] tool_exec --wait: connection lost during poll ({ex}), checking Unity...", file=sys.stderr)
            poll_hb = find_heartbeat(args.project)
            root = find_project_root(args.project)
            process_alive = is_unity_process_running(root) if root else is_unity_process_running()
            if not process_alive and (not poll_hb or not check_heartbeat_fresh(poll_hb)):
                return {
                    "ok": False,
                    "error": {
                        "code": "connection_lost",
                        "message": "Unity process not found and heartbeat stale during pending poll",
                    },
                }
            # PendingResponse は本格的に長時間処理 (Play mode entry 等) を含むので
            # 再接続待ちは通常 wait より寛容にする。残時間の半分まで待つ。
            remaining = max(int((deadline - time.time()) / CONNECTION_RETRY_INTERVAL), 1)
            generous_budget = max(CONNECTION_RETRY_MAX * 4, remaining // 2)
            hb = wait_for_connection(
                args.project,
                max_retries=min(remaining, generous_budget),
                require_tcp=True,
            )
            if hb and hb.get("port"):
                current_port = int(hb["port"])
                resp = {"ok": True, "result": {"_mcp_status": "pending", "_mcp_poll_interval": 1.0}}
                continue
            # Heartbeat があれば process は生きている可能性が高い。最後にもう一度状態問い合わせを試す。
            poll_hb = find_heartbeat(args.project)
            if poll_hb and check_heartbeat_fresh(poll_hb):
                resp = {"ok": True, "result": {"_mcp_status": "pending", "_mcp_poll_interval": 2.0}}
                continue
            return {
                "ok": False,
                "error": {"code": "connection_lost", "message": "Unity reconnect timed out during pending poll"},
            }
        except OSError as ex:
            print(f"[unitap] tool_exec --wait: socket error during poll ({ex}), retrying...", file=sys.stderr)
            time.sleep(0.5)
            continue
        except RuntimeError as ex:
            print(f"[unitap] tool_exec --wait: protocol error during poll ({ex}), retrying...", file=sys.stderr)
            time.sleep(0.5)
            continue

        if not isinstance(resp, dict):
            continue

    # Timed out
    elapsed = int(wait_timeout_s)
    return {
        "ok": False,
        "error": {
            "code": "wait_timeout",
            "message": f"tool_exec --wait timed out after {elapsed}s while polling {tool_name} status",
            "details": {"tool": tool_name, "lastResponse": resp},
        },
    }


def _tool_exec_alias_error(tool_name: str, tool_params: dict) -> dict | None:
    if tool_name == "eval":
        return {
            "ok": False,
            "error": {
                "code": "tool_exec_eval_disabled",
                "message": "tool_exec eval は安全のため無効です。専用の custom tool か既存コマンドを追加してください。",
            },
        }

    alternative = TOOL_EXEC_TOP_LEVEL_ALIASES.get(tool_name)
    if alternative is None:
        return None

    if tool_name == "execute_menu":
        menu_path = tool_params.get("menuPath")
        if isinstance(menu_path, str) and menu_path.strip():
            alternative = f"execute_menu --menuPath {json.dumps(menu_path, ensure_ascii=False)}"

    if tool_name == "manage_editor":
        action = tool_params.get("action")
        if action in ("play", "stop"):
            alternative = str(action)
        elif action == "launch":
            alternative = "launch"

    return {
        "ok": False,
        "error": {
            "code": "tool_exec_top_level_command",
            "message": f"'{tool_name}' は tool_exec 用 custom tool ではありません。代わりに `{alternative}` を使ってください。",
            "details": {
                "tool": tool_name,
                "alternative": alternative,
            },
        },
    }


def do_sync_command(args, port: int) -> None:
    """Synchronous command dispatcher for simple request-response commands."""
    params = {}
    retryable = True
    timeout_ms = DEFAULT_TIMEOUT_MS

    if args.command == "execute_menu":
        _check_execute_menu_blocklist(args.menuPath)
        params = {"menuPath": args.menuPath}
        timeout_ms = int(getattr(args, "timeout_ms", timeout_ms) or timeout_ms)
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
        if not isinstance(tool_params, dict):
            print_unitap_response(
                args,
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_tool_params",
                        "message": "tool_exec --params は JSON object を指定してください。",
                    },
                },
            )
            return
        alias_error = _tool_exec_alias_error(args.tool, tool_params)
        if alias_error is not None:
            print_unitap_response(args, alias_error)
            return
        params = {"tool": args.tool, "params": tool_params}
        timeout_ms = int(getattr(args, "timeout_ms", DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS)
        if timeout_ms == DEFAULT_TIMEOUT_MS:
            timeout_ms = LONG_RUNNING_TOOL_TIMEOUTS_MS.get(args.tool, timeout_ms)
        retryable = False
    elif args.command == "save_scene":
        params = {"all": args.all}
        retryable = False
    elif args.command == "reimport":
        params = {
            "paths": list(args.paths),
            "recursive": bool(getattr(args, "recursive", True)),
        }
        retryable = False

    req = build_request(args.command, params, timeout_ms, retryable)

    try:
        resp = send_with_retry(
            "127.0.0.1",
            port,
            req,
            timeout_s=timeout_ms / 1000 + 5,
            project_path=args.project,
            exit_on_error=False,
        )
    except Exception as e:
        print_unitap_response(
            args,
            {
                "ok": False,
                "error": {
                    "code": "connection_lost",
                    "message": str(e),
                },
            },
        )
        return

    should_wait = bool(getattr(args, "wait", False))
    if args.command == "tool_exec" and getattr(args, "wait", None) is None and args.tool == "run_automate_test":
        should_wait = True

    if (
        args.command == "tool_exec"
        and should_wait
        and _result_is_pending(resp)
    ):
        resp = poll_pending_tool_exec(
            args,
            port,
            args.tool,
            resp,
            timeout_ms,
        )
    elif args.command == "tool_exec":
        resp = _mark_pending_tool_exec_response(resp)

    if isinstance(resp, dict) and resp.get("ok"):
        result = resp.get("result", {})
        if isinstance(result, dict):
            if args.command == "diagnose":
                project_root = find_project_root(args.project)
                result = enrich_diagnose_result_with_editor_lock(project_root, result)
            resp["result"] = merge_wait_meta_into_result(args, result)
    print_unitap_response(args, resp)
