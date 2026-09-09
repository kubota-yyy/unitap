"""Agent discovery from the real CLI parser and live backend schemas.

Discovery never focuses the Editor, waits for an operation lock or retries a
request. Offline CLI discovery remains usable without Unity or UniCLI installed.
"""
import argparse
import json

from .commands import print_unitap_response
from .editor_lock import command_requires_editor_lock
from .transport import build_request, send_request_to_current_transport
from .unicli_bridge import run_unicli




def register(subparsers, dispatch_table, parser):
    for name, help_text in (
        ("commands", "Discover CLI commands; --live includes project tools and UniCLI"),
        ("describe", "Describe cli:NAME, tool:NAME or unicli:NAME with parameters"),
    ):
        p = subparsers.add_parser(name, help=help_text)
        if name == "commands":
            p.add_argument("--live", action="store_true", help="Query Editor tools and UniCLI without focusing or retrying")
            p.add_argument("--backend", choices=("all", "cli", "tool", "unicli"), default="all")
            p.add_argument("--search", default="", help="Case-insensitive name/description/module filter")
        else:
            p.add_argument("name", help="Qualified name from commands, e.g. unicli:GameObject.Find")
        p.add_argument("--timeout-ms", type=int, default=5000, help="Timeout per live backend in milliseconds")
        p.set_defaults(_skip_heartbeat=True, _skip_history=True)
        dispatch_table[name] = lambda args, _port: do_discovery(args, parser, subparsers)


def _parameters(parser):
    """Keep argparse introspection in one place, including extension arguments."""
    result = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        item = {
            "name": action.dest, "flags": action.option_strings,
            "required": action.required, "description": action.help or "",
            "type": getattr(action.type, "__name__", "bool" if action.nargs == 0 else "str"),
            "nargs": action.nargs,
        }
        if action.default != argparse.SUPPRESS:
            try:
                json.dumps(action.default)
                item["default"] = action.default
            except (TypeError, ValueError):
                pass
        if action.choices is not None:
            item["choices"] = list(action.choices)
        result.append(item)
    return result


def _cli_entries(subparsers):
    helps = {a.dest: a.help for a in subparsers._choices_actions}
    entries = []
    for name, parser in subparsers.choices.items():
        defaults = dict(parser._defaults)
        defaults["command"] = name
        lock = command_requires_editor_lock(argparse.Namespace(**defaults))
        entries.append({
            "id": f"cli:{name}", "name": name, "backend": "cli",
            "description": parser.description or helps.get(name) or "See command help",
            "invocation": [name], "parameters": _parameters(parser),
            "lockPolicy": "depends_on_operation_or_tool" if name in ("unicli", "tool_exec") else ("exclusive" if lock else "none"),
            "help": parser.format_help(),
        })
    return entries


def _live_entries(project, backend, timeout_ms):
    if not project:
        return [], {"available": False, "error": {"code": "project_not_found", "message": "Specify --project to discover live commands."}}
    if backend == "unicli":
        payload = run_unicli(project, "commands", timeout_ms=timeout_ms)
    else:
        try:
            payload = send_request_to_current_transport(
                project, build_request("tool_list", {}, timeout_ms, retryable=False),
                timeout_s=timeout_ms / 1000,
            )
        except (OSError, ValueError) as error:
            payload = {"ok": False, "error": {"code": "tool_discovery_unavailable", "message": str(error)}}
    invalid = {"available": False, "error": {"code": "invalid_catalog", "message": "Backend did not return a command list."}}
    if not isinstance(payload, dict):
        return [], invalid
    if not payload.get("ok"):
        return [], {"available": False, "error": payload.get("error")}
    result = payload.get("result")
    if not isinstance(result, dict):
        return [], invalid
    if backend == "unicli":
        response = result.get("response")
        raw = response.get("data") if isinstance(response, dict) else None
    else:
        raw = result.get("tools")
    if not isinstance(raw, list) or any(not isinstance(e, dict) or not isinstance(e.get("name"), str) for e in raw):
        return [], invalid
    entries = []
    for entry in raw:
        name = entry["name"]
        entries.append({
            **entry, "id": f"{backend}:{name}", "backend": backend,
            "invocation": ["exec", name] if backend == "unicli" else ["tool_exec", "--tool", name, "--params", "{}"],
            "parameters": entry.get("requestFields", []) if backend == "unicli" else entry.get("parameters", []),
            "lockPolicy": "exclusive" if backend == "unicli" or command_requires_editor_lock(
                argparse.Namespace(command="tool_exec", tool=name)) else "none",
        })
    return entries, {"available": True, "count": len(entries)}


def do_discovery(args, parser, subparsers):
    def fail(code, message, details=None):
        print_unitap_response(args, {"ok": False, "error": {"code": code, "message": message, "details": details}})

    if args.timeout_ms <= 0:
        return fail("invalid_timeout", "--timeout-ms must be > 0")
    describe = args.command == "describe"
    if describe:
        backend, separator, name = args.name.partition(":")
        if not separator:
            backend, name = "cli", args.name
        if backend not in ("cli", "tool", "unicli") or not name:
            return fail("invalid_command_name", "Use cli:NAME, tool:NAME or unicli:NAME from commands.")
        live = backend != "cli"
    else:
        backend, live = args.backend, args.live
    entries = _cli_entries(subparsers) if backend in ("all", "cli") else []
    sources = {"cli": {"available": True, "count": len(entries)}} if entries else {}
    for source in ("tool", "unicli"):
        if backend not in ("all", source):
            continue
        if live:
            found, state = _live_entries(args.project, source, args.timeout_ms)
            entries.extend(found)
            sources[source] = state
        else:
            sources[source] = {"available": None, "hint": "Use commands --live with --project to query this backend."}
    if describe:
        matches = [e for e in entries if e["name"] == name]
        if len(matches) > 1:
            return fail("ambiguous_command", f"Multiple commands registered as {backend}:{name}.",
                        {"candidates": matches})
        entry = matches[0] if matches else None
        if entry is None:
            unavailable = sources.get(backend, {}).get("available") is False
            return fail("backend_unavailable" if unavailable else "command_not_found",
                        f"Cannot describe {backend}:{name}.", {"sources": sources, "hint": "Run commands --live --search NAME."})
        print_unitap_response(args, {"ok": True, "result": {
            "schemaVersion": 1, "command": entry, "globalOptions": _parameters(parser),
        }})
        return
    query = args.search.casefold()
    entries = [e for e in entries if query in " ".join(str(e.get(k, "")) for k in ("id", "description", "module")).casefold()]
    # Keep listings compact; full schemas and nested response types live in describe.
    summary_keys = ("id", "name", "backend", "description", "invocation", "module", "lockPolicy")
    print_unitap_response(args, {"ok": True, "result": {
        "schemaVersion": 1, "projectPath": args.project,
        "commands": [{k: e[k] for k in summary_keys if k in e} for e in sorted(entries, key=lambda e: e["id"])],
        "count": len(entries), "sources": sources,
        "partial": any(s.get("available") is False for s in sources.values()),
        "next": "Use describe ID for parameters. Global --project and --json go before the command.",
        "guide": "docs/agent-guide.md",
    }})
