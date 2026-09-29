"""複数 Editor・複数セッション運用のためのコマンド (lease / editors / quit)。"""
from __future__ import annotations

import sys
from pathlib import Path

from .commands import print_unitap_response
from .editor_lock import get_editor_operation_lock_snapshot
from .heartbeat import check_heartbeat_fresh, find_heartbeat
from .lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    EditorLeasedError,
    acquire_lease,
    lease_snapshot,
    release_lease,
    resolve_session_owner,
)
from .project import is_unity_project_root
from .unity import get_process_rss_mb, kill_unity_processes, list_unity_processes


def register(subparsers, dispatch_table) -> None:
    p_lease = subparsers.add_parser(
        "lease",
        help="Reserve this project's Editor for the current session (acquire/renew/release/status)",
    )
    p_lease.add_argument("action", choices=("acquire", "renew", "release", "status"))
    p_lease.add_argument("--ttl", type=int, default=DEFAULT_LEASE_TTL_SECONDS, help="Lease lifetime in seconds (renewed by each exclusive command)")
    p_lease.add_argument("--note", default=None, help="What this session is doing (shown to waiting sessions)")
    p_lease.add_argument("--owner", default=None, help="Owner id (default: UNITAP_SESSION or the agent session id)")
    p_lease.add_argument("--force", action="store_true", help="release: remove another session's lease (only when it is gone)")
    p_lease.set_defaults(_skip_heartbeat=True)
    dispatch_table["lease"] = do_lease

    p_editors = subparsers.add_parser(
        "editors",
        help="List every running Unity Editor on this machine with heartbeat, lock and lease holders",
    )
    p_editors.set_defaults(_skip_heartbeat=True, _skip_history=True)
    dispatch_table["editors"] = do_editors

    p_quit = subparsers.add_parser(
        "quit",
        help="Quit only this project's Unity Editor (other projects' Editors are left running)",
    )
    p_quit.set_defaults(_skip_heartbeat=True)
    dispatch_table["quit"] = do_quit


def _owner_from_args(args) -> str | None:
    return (getattr(args, "owner", None) or "").strip() or resolve_session_owner()


def do_lease(args, _port=None) -> None:
    project_root = Path(args.project) if getattr(args, "project", None) else None
    if project_root is None:
        print_unitap_response(args, {"ok": False, "error": {"code": "project_not_found", "message": "Specify --project."}})
        return

    owner = _owner_from_args(args)
    if args.action == "status":
        print_unitap_response(args, {"ok": True, "result": {
            "owner": owner,
            "lease": lease_snapshot(project_root, owner),
        }})
        return

    if args.action in ("acquire", "renew") and not owner:
        print_unitap_response(args, {"ok": False, "error": {
            "code": "session_unknown",
            "message": "Cannot identify this session. Set UNITAP_SESSION=<name> or pass --owner.",
        }})
        return
    if args.ttl <= 0:
        print_unitap_response(args, {"ok": False, "error": {"code": "invalid_ttl", "message": "--ttl must be > 0"}})
        return

    try:
        if args.action == "release":
            result = release_lease(project_root, owner, force=bool(args.force))
        else:
            if args.action == "renew":
                current = lease_snapshot(project_root, owner)
                if not current or not current.get("active") or not current.get("ownedByCaller"):
                    print_unitap_response(args, {"ok": False, "error": {
                        "code": "lease_not_held",
                        "message": "This session does not hold an active lease. Use `lease acquire`.",
                        "details": {"lease": current},
                    }})
                    return
            current = lease_snapshot(project_root, owner)
            if current and current.get("active") and not current.get("ownedByCaller") and getattr(args, "wait_lock", False):
                print(
                    f"[unitap] waiting for the Editor lease held by {current.get('owner')} "
                    f"({current.get('note') or 'no note'}, {current.get('remainingSeconds')}s left)...",
                    file=sys.stderr,
                )
            result = {"lease": acquire_lease(
                project_root,
                owner,
                ttl_seconds=int(args.ttl),
                note=args.note,
                wait=bool(getattr(args, "wait_lock", False)) and args.action == "acquire",
                timeout_s=float(getattr(args, "lock_timeout", 0.0) or 0.0),
            )}
    except EditorLeasedError as ex:
        print_unitap_response(args, {"ok": False, "error": {"code": ex.code, "message": ex.message, "details": ex.details}})
        return
    print_unitap_response(args, {"ok": True, "result": result})


def collect_editors() -> list[dict]:
    editors: list[dict] = []
    for proc in list_unity_processes():
        raw_path = proc.get("projectPath")
        root = Path(raw_path) if raw_path else None
        entry = {
            "pid": proc.get("pid"),
            "projectPath": raw_path,
            "rssMb": get_process_rss_mb(int(proc["pid"])) if proc.get("pid") else None,
        }
        if root is not None and is_unity_project_root(root):
            hb = find_heartbeat(str(root))
            if hb:
                entry["heartbeat"] = {
                    "fresh": check_heartbeat_fresh(hb),
                    "pidMatches": hb.get("pid") == proc.get("pid"),
                    "transportKind": hb.get("transportKind"),
                    "port": hb.get("port"),
                    "isPlaying": hb.get("isPlaying"),
                    "isCompiling": hb.get("isCompiling"),
                    "unityVersion": hb.get("unityVersion"),
                }
            lock = get_editor_operation_lock_snapshot(root)
            if lock and lock.get("held"):
                entry["operationLock"] = lock.get("holder")
            lease = lease_snapshot(root, resolve_session_owner())
            if lease and lease.get("active"):
                entry["lease"] = {
                    key: lease.get(key)
                    for key in ("owner", "note", "expiresAt", "remainingSeconds", "ownedByCaller")
                }
        editors.append(entry)
    return editors


def do_editors(args, _port=None) -> None:
    editors = collect_editors()
    total_rss = sum(e["rssMb"] for e in editors if isinstance(e.get("rssMb"), (int, float)))
    print_unitap_response(args, {"ok": True, "result": {
        "count": len(editors),
        "totalRssMb": round(total_rss, 1),
        "session": resolve_session_owner(),
        "editors": editors,
    }})


def do_quit(args, _port=None) -> None:
    project_root = Path(args.project) if getattr(args, "project", None) else None
    if project_root is None:
        print_unitap_response(args, {"ok": False, "error": {"code": "project_not_found", "message": "Specify --project."}})
        return
    running = list_unity_processes(project_root)
    if not running:
        print_unitap_response(args, {"ok": True, "result": {"quit": False, "message": "Unity is not running for this project."}})
        return
    killed = kill_unity_processes(project_root)
    remaining = list_unity_processes(project_root)
    if remaining:
        print_unitap_response(args, {"ok": False, "error": {
            "code": "unity_quit_failed",
            "message": "Unity for this project is still running.",
            "details": {"remainingPids": [p.get("pid") for p in remaining]},
        }})
        return
    print(f"Quit Unity for {project_root}: {killed}", file=sys.stderr)
    print_unitap_response(args, {"ok": True, "result": {"quit": True, "pids": killed, "projectPath": str(project_root)}})
