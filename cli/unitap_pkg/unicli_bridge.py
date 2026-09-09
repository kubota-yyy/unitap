"""UniCLIへの明示的な委譲。失敗時に別経路で同じ変更を再送しない。"""
import argparse
import json
import os
import shutil
import subprocess

from .commands import print_unitap_response


def register(subparsers, dispatch_table):
    parser = subparsers.add_parser("unicli", help="Run UniCLI through unitap's project lock and result envelope")
    parser.add_argument("--timeout-ms", type=int, default=90000, help="Command timeout in milliseconds (before the operation)")
    parser.add_argument("operation", choices=("check", "status", "commands", "exec", "eval"))
    parser.add_argument("unicli_args", nargs=argparse.REMAINDER, help="UniCLI command/code and its arguments")
    parser.set_defaults(_skip_heartbeat=True)
    dispatch_table["unicli"] = do_unicli
    for operation, help_text in (
        ("exec", "Execute a UniCLI command with Unitap project lock and JSON results"),
        ("eval", "Evaluate C# via UniCLI with Unitap project lock and JSON results"),
    ):
        alias = subparsers.add_parser(operation, help=help_text)
        alias.add_argument("--timeout-ms", type=int, default=90000, help="Timeout in milliseconds; put before command/code")
        alias.add_argument("unicli_args", nargs=argparse.REMAINDER, help="Command/code and UniCLI arguments")
        alias.set_defaults(_skip_heartbeat=True, operation=operation)
        dispatch_table[operation] = do_unicli


def run_unicli(project, operation, forwarded=(), timeout_ms=90000):
    """Return an envelope without printing; callers own locking and history."""
    def fail(code, message, details=None):
        payload = {"ok": False, "backend": "unicli", "error": {"code": code, "message": message}}
        if details is not None:
            payload["error"]["details"] = details
        return payload

    if timeout_ms <= 0:
        return fail("invalid_timeout", "--timeout-ms must be > 0")
    if not project:
        return fail("project_not_found", "Specify the Unity project with unitap --project.")
    binary = shutil.which(os.environ.get("UNITAP_UNICLI_BIN", "unicli"))
    if not binary:
        return fail("unicli_not_found", "Install UniCLI or set UNITAP_UNICLI_BIN to its executable.")
    forwarded = list(forwarded)
    if operation in ("exec", "eval") and not forwarded:
        return fail("missing_argument", "exec requires a command; eval requires C# code.")
    # 対象projectとtimeoutは入口で一元管理し、ロック対象との食い違いを防ぐ。
    if any(value.split("=", 1)[0] in ("--project", "--timeout") for value in forwarded):
        return fail("conflicting_option", "Use unitap --project and unicli --timeout-ms before the operation.")
    command = [binary, operation, *forwarded]
    if "--json" not in forwarded:
        command.append("--json")
    if operation in ("exec", "eval"):
        command.extend(["--timeout", str(timeout_ms)])
    if operation in ("exec", "eval", "commands") and "--no-focus" not in forwarded:
        command.append("--no-focus")
    env = dict(os.environ, UNICLI_PROJECT=str(project))
    try:
        completed = subprocess.run(command, cwd=project, env=env, capture_output=True,
                                   text=True, timeout=timeout_ms / 1000 + 5, check=False)
    except subprocess.TimeoutExpired:
        return fail("unicli_timeout", "UniCLI timed out. Unity may still be executing; inspect state before retrying.")
    except OSError as error:
        return fail("unicli_launch_failed", str(error))
    try:
        response = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return fail("unicli_invalid_response", "UniCLI did not return JSON.", {
            "exitCode": completed.returncode, "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]})
    if not isinstance(response, dict):
        return fail("unicli_invalid_response", "Expected a UniCLI JSON object.")
    if completed.returncode != 0 or response.get("success") is False:
        return fail("unicli_failed", response.get("message", "UniCLI command failed."), {
            "exitCode": completed.returncode, "response": response, "stderr": completed.stderr[-8000:]})
    return {"ok": True, "result": {
        "backend": "unicli", "operation": operation, "exitCode": completed.returncode,
        "response": response}}


def do_unicli(args, _port=None):
    print_unitap_response(args, run_unicli(
        args.project, args.operation, args.unicli_args, args.timeout_ms))
