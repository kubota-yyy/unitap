import json
import os
import socket
import struct
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    CONNECTION_RETRY_INTERVAL,
    CONNECTION_RETRY_MAX,
    DEFAULT_TIMEOUT_MS,
    HEADER_SIZE,
    LIVENESS_RECONNECT_RETRY_MAX,
    POLL_INTERVAL,
    TCP_TEST_TIMEOUT,
)
from .editor_log import find_project_root, parse_project_editor_log_snapshot, read_compile_errors
from .heartbeat import check_heartbeat_fresh, find_heartbeat
from .unity import is_unity_process_running


def send_request(host: str, port: int, request: dict, timeout_s: float = 30) -> dict:
    """TCP 接続してリクエストを送信し、レスポンスを受信"""
    payload = json.dumps(request).encode("utf-8")
    header = struct.pack(">q", len(payload))

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((host, port))
        sock.sendall(header + payload)

        # レスポンスヘッダ受信
        resp_header = recv_exact(sock, HEADER_SIZE)
        resp_len = struct.unpack(">q", resp_header)[0]
        if resp_len <= 0 or resp_len > 64 * 1024 * 1024:
            raise RuntimeError(f"Invalid response frame size: {resp_len}")

        resp_payload = recv_exact(sock, resp_len)
        return json.loads(resp_payload.decode("utf-8"))
    finally:
        sock.close()


def send_pipe_request(pipe_socket_path: str, request: dict, timeout_s: float = 30) -> dict:
    """Unix domain socket 経由で Unitap pipe transport に送信する"""
    if not pipe_socket_path:
        raise ConnectionError("Pipe socket path is missing")

    payload = json.dumps(request).encode("utf-8")
    header = struct.pack("<i", len(payload))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(pipe_socket_path)
        sock.sendall(header + payload)

        ack = recv_exact(sock, 1)
        if ack != b"\x01":
            raise RuntimeError(f"Invalid pipe ack: {ack!r}")

        resp_header = recv_exact(sock, 4)
        resp_len = struct.unpack("<i", resp_header)[0]
        if resp_len <= 0 or resp_len > 64 * 1024 * 1024:
            raise RuntimeError(f"Invalid response frame size: {resp_len}")

        resp_payload = recv_exact(sock, resp_len)
        return json.loads(resp_payload.decode("utf-8"))
    finally:
        sock.close()


def send_file_request(file_transport_directory: str, request: dict, timeout_s: float = 30) -> dict:
    """ファイル transport に request を置き、response を待つ"""
    if not file_transport_directory:
        raise ConnectionError("File transport directory is missing")

    root = Path(file_transport_directory)
    requests_dir = root / "requests"
    responses_dir = root / "responses"
    if not requests_dir.exists() or not responses_dir.exists():
        raise ConnectionError(f"File transport directory is not ready: {file_transport_directory}")

    request_id = str(request.get("requestId") or uuid.uuid4())
    request_path = requests_dir / f"{request_id}.json"
    temp_path = requests_dir / f".{request_id}.{uuid.uuid4().hex}.tmp"
    response_path = responses_dir / f"{request_id}.json"

    payload = json.dumps(request, ensure_ascii=False)
    if response_path.exists():
        try:
            response_path.unlink()
        except OSError:
            pass

    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(request_path)

    deadline = time.time() + timeout_s
    last_error = None
    while time.time() < deadline:
        if response_path.exists():
            try:
                raw = response_path.read_text(encoding="utf-8")
                response = json.loads(raw)
                try:
                    response_path.unlink()
                except OSError:
                    pass
                return response
            except (OSError, json.JSONDecodeError) as ex:
                last_error = ex
        time.sleep(0.05)

    # タイムアウト時はリクエストファイルを削除する。
    # 残しておくとドメインリロード後に Unity が古いリクエストを処理してしまい、
    # 例えば status ポーリングが "completed" 状態を読んで state ファイルを消去し、
    # 本来のポーリングが "状態が見つかりません" になる問題が発生する。
    try:
        if request_path.exists():
            request_path.unlink()
    except OSError:
        pass

    if last_error is not None:
        raise TimeoutError(f"Timed out waiting for file response ({last_error})")
    raise TimeoutError("Timed out waiting for file response")


def recv_exact(sock: socket.socket, count: int) -> bytes:
    """指定バイト数を確実に受信"""
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)


def build_request(command: str, params: dict | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS, retryable: bool = True) -> dict:
    """リクエスト JSON を構築"""
    return {
        "version": 1,
        "requestId": str(uuid.uuid4()),
        "idempotencyKey": str(uuid.uuid4()),
        "command": command,
        "params": params or {},
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "timeoutMs": timeout_ms,
        "retryable": retryable,
    }


def _heartbeat_transport_kind(heartbeat: dict | None) -> str:
    if not isinstance(heartbeat, dict):
        return "tcp"
    kind = str(heartbeat.get("transportKind") or "tcp").strip().lower()
    return kind or "tcp"


def _heartbeat_pipe_socket_path(heartbeat: dict) -> str | None:
    socket_path = heartbeat.get("pipeSocketPath")
    if isinstance(socket_path, str) and socket_path.strip():
        return socket_path.strip()

    pipe_name = heartbeat.get("pipeName")
    if not isinstance(pipe_name, str) or not pipe_name.strip():
        return None
    return str(Path(tempfile.gettempdir()) / f"CoreFxPipe_{pipe_name.strip()}")


def send_request_via_heartbeat(
    heartbeat: dict,
    request: dict,
    timeout_s: float = 30,
    fallback_host: str = "127.0.0.1",
    fallback_port: int | None = None,
) -> dict:
    kind = _heartbeat_transport_kind(heartbeat)
    if kind == "pipe":
        pipe_socket_path = _heartbeat_pipe_socket_path(heartbeat)
        return send_pipe_request(pipe_socket_path, request, timeout_s)
    if kind == "file":
        return send_file_request(heartbeat.get("fileTransportDirectory"), request, timeout_s)

    port_value = heartbeat.get("port", fallback_port)
    if not port_value:
        raise ConnectionError("TCP port is unavailable")
    host_value = str(heartbeat.get("host") or fallback_host)
    return send_request(host_value, int(port_value), request, timeout_s)


def send_request_to_current_transport(
    project_path: str | None,
    request: dict,
    timeout_s: float = 30,
    fallback_host: str = "127.0.0.1",
    fallback_port: int | None = None,
) -> dict:
    heartbeat = find_heartbeat(project_path) if project_path else None
    if heartbeat:
        return send_request_via_heartbeat(
            heartbeat,
            request,
            timeout_s=timeout_s,
            fallback_host=fallback_host,
            fallback_port=fallback_port,
        )

    if not fallback_port:
        raise ConnectionError("Unitap heartbeat not found and TCP fallback port is unavailable")
    return send_request(fallback_host, int(fallback_port), request, timeout_s)


def wait_for_connection(
    project_path: str | None,
    max_retries: int = CONNECTION_RETRY_MAX,
    require_tcp: bool = False,
    on_wait_retry=None,
) -> dict | None:
    """heartbeat が fresh になるまで待機し、接続情報を返す。
    require_tcp=True の場合、現在の transport で request/response が確認できるまで待機する。"""
    ever_seen_heartbeat = False
    for i in range(max_retries):
        if i > 0:
            time.sleep(CONNECTION_RETRY_INTERVAL)

        hb = find_heartbeat(project_path)
        if hb:
            ever_seen_heartbeat = True

        if on_wait_retry:
            try:
                on_wait_retry(i, max_retries, hb)
            except Exception:
                pass

        if not hb or not check_heartbeat_fresh(hb):
            # heartbeat を一度も観測していない場合、Unity プロセスの存在を確認
            # 起動直後のレースを避けるため最低2回はチェック
            if not ever_seen_heartbeat and not hb and i >= 2:
                root = find_project_root(project_path)
                if root and not is_unity_process_running(root):
                    print(f"  Unity process not found, aborting wait ({i+1}/{max_retries})", file=sys.stderr)
                    return None
            state = "not responding" if hb else "not found"
            print(f"  waiting for Unity... ({state}, {i+1}/{max_retries})", file=sys.stderr)
            continue

        if hb.get("isCompiling"):
            if require_tcp:
                # ドメインリロード完了後、heartbeat更新前に transport が復帰している可能性
                try:
                    test_req = build_request("status", {}, 1000)
                    send_request_via_heartbeat(hb, test_req, timeout_s=1)
                    return hb
                except Exception:
                    pass  # transport未復帰 → 次のループへ
            print(f"  waiting for Unity... (domain reload, {i+1}/{max_retries})", file=sys.stderr)
            continue

        if not require_tcp:
            return hb

        # 現在の transport で接続を確認
        try:
            test_req = build_request("status", {}, TCP_TEST_TIMEOUT * 1000)
            send_request_via_heartbeat(hb, test_req, timeout_s=TCP_TEST_TIMEOUT)
            return hb
        except Exception:
            print(f"  waiting for Unity... (transport not ready, {i+1}/{max_retries})", file=sys.stderr)
            continue
    return None


def send_with_retry(
    host: str,
    port: int,
    request: dict,
    timeout_s: float,
    project_path: str | None,
    exit_on_error: bool = True,
) -> dict:
    """現在の transport で送信。Connection refused/closed 時にドメインリロード待ちでリトライ"""
    current_port = port

    for attempt in range(3):  # 復帰後の再接続断に備えて最大3回リトライ
        try:
            return send_request_to_current_transport(
                project_path,
                request,
                timeout_s=timeout_s,
                fallback_host=host,
                fallback_port=current_port,
            )
        except (ConnectionRefusedError, ConnectionError, ConnectionResetError, TimeoutError, OSError, RuntimeError) as e:
            if attempt == 0:
                hb = find_heartbeat(project_path)
                if not hb:
                    # heartbeat 不在でも、起動直後は Unitap 側初期化遅延の可能性があるため短時間待つ
                    recovered_hb = wait_for_connection(
                        project_path,
                        max_retries=LIVENESS_RECONNECT_RETRY_MAX,
                        require_tcp=True,
                    )
                    if recovered_hb:
                        if recovered_hb.get("port"):
                            current_port = int(recovered_hb["port"])
                        request["requestId"] = str(uuid.uuid4())
                        request["idempotencyKey"] = str(uuid.uuid4())
                        continue

                    root = find_project_root(project_path)
                    process_running = is_unity_process_running(root) if root else is_unity_process_running()
                    # 診断情報をローカルデータから出力（TCP通信なし）
                    diag_hb = find_heartbeat(project_path)
                    diag_parts = [f"heartbeat={'found' if diag_hb else 'not found'}"]
                    if diag_hb:
                        diag_parts.append(f"fresh={check_heartbeat_fresh(diag_hb)}")
                        diag_parts.append(f"isCompiling={diag_hb.get('isCompiling')}")
                    diag_parts.append(f"process={'running' if process_running else 'not found'}")
                    print(f"  diagnostic: {', '.join(diag_parts)}", file=sys.stderr)
                    if exit_on_error:
                        if process_running:
                            print(
                                f"Error [unity_running_but_unitap_unavailable]: {e} "
                                "(Unity process is running but Unitap is unavailable)",
                                file=sys.stderr,
                            )
                        else:
                            print(f"Error: {e} (Unity not running)", file=sys.stderr)
                        sys.exit(1)
                    if process_running:
                        raise ConnectionError(
                            f"{e} (Unity process is running but Unitap is unavailable)"
                        ) from e
                    raise ConnectionError(f"{e} (Unity not running)") from e
            print(f"Connection lost ({e}), waiting for domain reload to complete...", file=sys.stderr)

        # transport接続確認込みで復帰を待つ。
        # 動画収録など Play Mode 入り直後の初回 domain reload が長い用途では、
        # 呼び出し側が UNITAP_RECONNECT_RETRY_MAX で待機時間を拡張できる。
        reconnect_max_retries = CONNECTION_RETRY_MAX
        env_retries = os.environ.get("UNITAP_RECONNECT_RETRY_MAX")
        if env_retries:
            try:
                reconnect_max_retries = max(reconnect_max_retries, int(env_retries))
            except ValueError:
                pass
        hb = wait_for_connection(project_path, max_retries=reconnect_max_retries, require_tcp=True)
        if not hb:
            if exit_on_error:
                print("Error: Unity did not recover in time", file=sys.stderr)
                sys.exit(1)
            raise ConnectionError("Unity did not recover in time")

        if hb.get("port"):
            current_port = int(hb["port"])
        # ドメインリロード後は古いリクエストファイルが再処理されるため
        # idempotencyKey も再生成して衝突を防ぐ
        request["requestId"] = str(uuid.uuid4())
        request["idempotencyKey"] = str(uuid.uuid4())

    if exit_on_error:
        print("Error: Unity did not recover after retries", file=sys.stderr)
        sys.exit(1)
    raise ConnectionError("Unity did not recover after retries")


def extract_wait_idle_state(result: dict, project_path: str | None) -> tuple[bool, bool]:
    is_compiling = bool(result.get("isCompiling"))
    is_updating = bool(result.get("isUpdating"))
    if "isCompiling" in result and "isUpdating" in result:
        return is_compiling, is_updating

    hb = find_heartbeat(project_path)
    if hb:
        try:
            status_req = build_request("status", {}, 5000)
            status_resp = send_request_via_heartbeat(hb, status_req, timeout_s=5)
            if status_resp.get("ok"):
                status_result = status_resp.get("result", {})
                return bool(status_result.get("isCompiling")), bool(status_result.get("isUpdating"))
        except Exception:
            pass
        fallback_compiling = bool(hb.get("isCompiling"))
        return fallback_compiling, fallback_compiling

    snapshot = parse_project_editor_log_snapshot(project_path)
    if snapshot:
        return bool(snapshot.get("isCompiling")), bool(snapshot.get("isCompiling"))

    return False, False


def poll_async_job(host: str, port: int, command: str, params: dict, timeout_ms: int, project_path: str | None) -> dict:
    """非同期ジョブを開始→ポーリング→完了結果を返す"""
    current_port = port
    started_at = time.time()
    request_timeout_ms = max(30000, min(int(timeout_ms), 120000))
    request_timeout_s = request_timeout_ms / 1000.0 + 5.0
    start_retry_deadline = time.time() + min(20.0, max(timeout_ms / 1000.0, 5.0))

    # 1. ジョブ開始リクエスト（即座に返る）
    while True:
        start_req = build_request(command, params, request_timeout_ms, False)
        try:
            resp = send_with_retry(
                host,
                current_port,
                start_req,
                timeout_s=request_timeout_s,
                project_path=project_path,
            )
        except socket.timeout:
            if time.time() < start_retry_deadline:
                time.sleep(0.5)
                continue
            return {"ok": False, "error": {"code": "timeout", "message": "Start request timed out"}}
        if resp.get("ok"):
            break
        error = resp.get("error", {}) if isinstance(resp, dict) else {}
        # precondition_failed (PlayMode中) はループ脱出してリカバリパスへ
        if error.get("code") == "precondition_failed":
            break
        if error.get("code") == "timeout" and time.time() < start_retry_deadline:
            time.sleep(0.5)
            continue
        return resp

    result = resp.get("result") or {}
    error = resp.get("error") or {}

    # PlayMode中の場合（compile_check）— precondition_failed エラーまたは result.isPlaying
    if result.get("isPlaying") or error.get("code") == "precondition_failed":
        print("Play mode detected, stopping and retrying...", file=sys.stderr)
        try:
            stop_req = build_request("stop", {}, 10000, False)
            send_with_retry(
                host,
                current_port,
                stop_req,
                timeout_s=15,
                project_path=project_path,
                exit_on_error=False,
            )
        except Exception:
            pass
        hb = wait_for_connection(project_path, require_tcp=True)
        if not hb:
            return {"ok": False, "error": {"code": "reconnect_failed", "message": "Failed to reconnect after stopping Play mode"}}
        if hb.get("port"):
            current_port = int(hb["port"])
        # リトライ
        retry_req = build_request(command, params, request_timeout_ms, False)
        resp = send_with_retry(
            host,
            current_port,
            retry_req,
            timeout_s=request_timeout_s,
            project_path=project_path,
            exit_on_error=False,
        )
        if not resp.get("ok"):
            return resp
        result = resp.get("result", {})

    # ジョブが即座に完了した場合（not_found 等）
    if result.get("status") != "running":
        if command == "wait_idle":
            wait_result = dict(result)
            is_compiling, is_updating = extract_wait_idle_state(wait_result, project_path)
            wait_result["isCompiling"] = is_compiling
            wait_result["isUpdating"] = is_updating
            wait_result["idle"] = not is_compiling and not is_updating
            wait_result.setdefault("status", "completed")
            resp["result"] = wait_result
        return resp

    job_id = result.get("jobId")
    if not job_id:
        return resp

    # 2. ポーリングループ
    deadline = time.time() + timeout_ms / 1000 + 10
    poll_params = {**params, "jobId": job_id}

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)

        poll_req = build_request(command, poll_params, request_timeout_ms, False)
        try:
            resp = send_request_to_current_transport(
                project_path,
                poll_req,
                timeout_s=request_timeout_s,
                fallback_host=host,
                fallback_port=current_port,
            )
        except (ConnectionRefusedError, ConnectionError, ConnectionResetError, socket.timeout) as ex:
            # ドメインリロード中: プロセス生存 + heartbeat を複合判定してから復帰待ち
            print(f"Connection lost during poll ({ex}), checking Unity state...", file=sys.stderr)
            poll_hb = find_heartbeat(project_path)
            root = find_project_root(project_path)
            process_alive = is_unity_process_running(root) if root else is_unity_process_running()
            if not process_alive and (not poll_hb or not check_heartbeat_fresh(poll_hb)):
                print("Unity process not found and heartbeat stale, falling back to local data", file=sys.stderr)
                break
            remaining = max(int((deadline - time.time()) / CONNECTION_RETRY_INTERVAL), 1)
            hb = wait_for_connection(project_path, max_retries=min(remaining, CONNECTION_RETRY_MAX), require_tcp=True)
            if hb:
                if hb.get("port"):
                    current_port = int(hb["port"])
                continue
            print("Unity reconnect timed out during poll, will determine final state from heartbeat/log", file=sys.stderr)
            break
        except OSError as ex:
            print(f"Socket error during poll ({ex}), retrying...", file=sys.stderr)
            continue
        except RuntimeError as ex:
            print(f"Protocol error during poll ({ex}), retrying...", file=sys.stderr)
            continue

        if not resp.get("ok"):
            error = resp.get("error", {}) if isinstance(resp, dict) else {}
            if error.get("code") == "timeout":
                continue
            return resp

        poll_result = resp.get("result", {})
        if poll_result.get("status") == "not_found" and command in ("wait_idle", "compile_check"):
            # ドメインリロード直後は async-job 復元前に一時的に not_found が返ることがある。
            # 即座に終端扱いすると compile_check が途中状態で抜けるため、期限までは再ポーリングする。
            continue
        if poll_result.get("status") != "running":
            if command == "wait_idle":
                wait_result = dict(poll_result)
                is_compiling, is_updating = extract_wait_idle_state(wait_result, project_path)
                wait_result["isCompiling"] = is_compiling
                wait_result["isUpdating"] = is_updating
                wait_result["idle"] = not is_compiling and not is_updating
                resp["result"] = wait_result
            return resp

    elapsed_ms = int((time.time() - started_at) * 1000)
    if command == "wait_idle":
        is_compiling, is_updating = extract_wait_idle_state({}, project_path)
        return {
            "ok": True,
            "result": {
                "status": "completed",
                "timedOut": True,
                "elapsedMs": elapsed_ms,
                "idle": not is_compiling and not is_updating,
                "isCompiling": is_compiling,
                "isUpdating": is_updating,
                "source": "async-job poll timeout",
            },
        }

    # compile_check のタイムアウト時: compile-errors.json / Editor.log から状態を補完する
    is_compiling, is_updating = extract_wait_idle_state({}, project_path)
    snapshot = parse_project_editor_log_snapshot(project_path)
    session_state = str(snapshot.get("sessionState", "")).lower() if snapshot else ""
    compile_started = bool(
        is_compiling
        or is_updating
        or session_state in ("running", "success", "failed")
    )
    compile_finished = bool(
        session_state in ("success", "failed")
        and not is_compiling
        and not is_updating
    )
    fallback_result = {
        "status": "completed",
        "timedOut": not compile_finished,
        "compiled": compile_finished,
        "idle": not is_compiling and not is_updating,
        "isCompiling": is_compiling,
        "isUpdating": is_updating,
        "compileStarted": compile_started,
        "compileStartObservedAtMs": 0 if compile_started else None,
        "hasErrors": False,
        "errors": [],
        "warnings": [],
        "errorCount": 0,
        "warningCount": 0,
        "elapsedMs": elapsed_ms,
        "source": "editor_log (async job timed out)" if snapshot else "async-job poll timeout",
    }
    compile_errors = read_compile_errors(project_path)
    if compile_errors:
        error_entries = [e for e in compile_errors if e.get("level") == "error"]
        warning_entries = [e for e in compile_errors if e.get("level") == "warning"]
        if error_entries:
            fallback_result["hasErrors"] = True
            fallback_result["compiled"] = compile_finished or (not is_compiling and not is_updating)
            fallback_result["errorCount"] = len(error_entries)
            fallback_result["warningCount"] = len(warning_entries)
            fallback_result["errors"] = [{"message": e.get("message", ""), "file": e.get("file", ""), "line": e.get("line", 0)} for e in error_entries]
            fallback_result["warnings"] = [{"message": e.get("message", ""), "file": e.get("file", ""), "line": e.get("line", 0)} for e in warning_entries]
            fallback_result["source"] = "compile-errors.json (async job timed out)"
    return {"ok": True, "result": fallback_result}
