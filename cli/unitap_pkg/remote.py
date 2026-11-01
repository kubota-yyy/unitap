"""Run unitap on whichever Mac of the pool has room (hosts.json), with the local working tree mirrored there.

Agents keep calling the project's `scripts/unitap`. With `--host auto` (the default inside a Claude Code /
Codex session once ~/.unitap/hosts.json exists) a command that would start Unity work stays on this Mac while
it has a free Editor slot, and otherwise goes to the pool Mac with the most room. The choice is remembered per
agent session and project ("placement"), so the following commands of that session (status, wait_result,
quit, ...) reach the same Editor without repeating the flag.

Before each command that reads the project, the working tree (uncommitted and untracked files included) is
rsynced into the session's mirror on that Mac; Library stays warm there. Paths in the output that point into
the mirror are rewritten to the local tree, and the files they name (QA runs, reports) are copied back when
they are not tracked by git.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import remote_helper

HOSTS_FILE_ENV = "UNITAP_HOSTS_FILE"
HOST_ENV = "UNITAP_HOST"
PROJECT_CONFIG = "unitap-remote.json"
PLACEMENT_TTL_SECONDS = 12 * 3600
MIN_LOCAL_FREE_PERCENT = 20.0
MIN_REMOTE_FREE_PERCENT = 25.0
ACTIVE_USER_SECONDS = 600
REMOTE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=4"]
MAX_PULLED_PATHS = 20
# The mirror Editor's own state; copying it back would mix two Editors' Library/transport files.
EDITOR_STATE_DIRS = {"Library", "Temp", "Logs", "obj", "UserSettings", "Packages", "ProjectSettings"}

# Never forwarded: they manage placement or this Mac's own records.
LOCAL_ONLY_COMMANDS = {"hosts", "remote", "clone", "test_runs", "commands", "describe"}
# Without a placement these run here; they never start new Unity work, so they never pick a host.
NO_PLACEMENT_COMMANDS = {
    "editors", "doctor", "lease", "heartbeat", "status", "quit", "stop", "cancel", "read_console", "clear_console",
    "wait_result", "wait_idle", "wait_fsm", "focus", "dismiss_dialogs", "tool_list", "diagnose", "capture",
    "capture_editor", "undo", "redo",
}
# Forwarded without syncing the tree first (they only observe the running Editor).
NO_SYNC_COMMANDS = NO_PLACEMENT_COMMANDS
# Session ids for placements: the agent session first, so helper scripts with their own UNITAP_SESSION
# (admin/check.py, ...) inside one agent session reuse that session's Editor.
_AGENT_SESSION_KEYS = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_THREAD_ID", "codex"),
    ("CODEX_SESSION_ID", "codex"),
    ("STARSESSION_SESSION_ID", "starsession"),
)
PASS_THROUGH_ENV = ("UNITAP_TEST_TRIGGER", "UNITAP_ALLOW_SUBSECOND_WAIT_IDLE", "UNITAP_TRANSPORT_PREFERENCE")

DEFAULT_EXCLUDES = (
    "/Library/", "/Temp/", "/Logs/", "/obj/", "/.tmp/", "/Build/", "/Builds/", "/IOS_Distribution*/",
    "/.vs/", "/.idea/", "/*.csproj", "/*.sln", ".git", ".DS_Store", "/.unitap-mirror.json",
    "/UserSettings/",
)
# Written by the mirror's own Editor: copied once with a new mirror, then never synced or deleted. A clone
# without UserSettings made `rsync --delete` remove the Editor's folder, and Unity stopped on "Moving file
# failed ... UserSettings/EditorUserSettings.asset" (eipoc, 2026-11-01).
FIRST_COPY_ONLY = ("/UserSettings/",)
LIBRARY_EXCLUDES = (
    "/Bee/", "/PlayerDataCache/", "/Unitap/", "/Search/", "/ArtifactDB-lock", "/SourceAssetDB-lock",
    "/EditorInstance.json", "/il2cpp_cache/", "/BuildPlayerData/", "/BuildCache/",
)


class RemoteError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


# --- configuration -------------------------------------------------------------------------------------

def hosts_file() -> Path:
    return Path(os.environ.get(HOSTS_FILE_ENV) or (Path.home() / ".unitap" / "hosts.json")).expanduser()


def load_hosts() -> dict:
    """{name: config}. A host without "ssh" is reached locally only when it is this Mac."""
    try:
        data = json.loads(hosts_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    return {str(k).lower(): (v if isinstance(v, dict) else {}) for k, v in (hosts or {}).items()}


def local_host_name() -> str:
    return socket.gethostname().split(".")[0].lower()


def placement_session(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for key, prefix in _AGENT_SESSION_KEYS:
        value = (env.get(key) or "").strip()
        if value:
            return f"{prefix}:{value}"
    value = (env.get("UNITAP_SESSION") or "").strip()
    return value or None


def agent_session_present(environ: dict | None = None) -> bool:
    env = os.environ if environ is None else environ
    return any((env.get(key) or "").strip() for key, _ in _AGENT_SESSION_KEYS)


def requested_host(args, environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = getattr(args, "host", None) or (env.get(HOST_ENV) or "").strip() or None
    return value.lower() if value else None


# --- project layout --------------------------------------------------------------------------------------

def _file_package_targets(project: Path) -> list[Path]:
    try:
        deps = json.loads((project / "Packages" / "manifest.json").read_text(encoding="utf-8")).get("dependencies") or {}
    except (OSError, ValueError, AttributeError):
        return []
    targets = []
    for ref in deps.values():
        if isinstance(ref, str) and ref.startswith("file:"):
            raw = ref[len("file:"):]
            target = (Path(raw) if Path(raw).is_absolute() else project / "Packages" / raw).resolve()
            if target.exists():
                targets.append(target)
    return targets


def project_layout(project: Path) -> dict:
    """What to mirror: the project, `file:` packages outside it and the project's extra entries.

    anchor = the common parent, so the mirror keeps every relative reference. key groups clones of one
    project (`nek-unity--duo-walk` -> `nek-unity`, `rwd--daily-release` -> `rwd`) so they share warm mirrors.
    """
    project = Path(project).resolve()
    config = {}
    try:
        loaded = json.loads((project / PROJECT_CONFIG).read_text(encoding="utf-8"))
        config = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    items: list[Path] = [project]
    for target in _file_package_targets(project):
        if target != project and project not in target.parents:
            items.append(target)
    for entry in config.get("sync") or []:
        target = (project / str(entry)).resolve()
        if target.exists() and target not in items:
            items.append(target)
    anchor = Path(os.path.commonpath([str(p if p.is_dir() else p.parent) for p in items]))
    key = re.sub(r"--.*$", "", anchor.name) or anchor.name
    key = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:64] or "project"
    return {
        "project": project,
        "anchor": anchor,
        "key": key,
        "projectRel": os.path.relpath(project, anchor),
        "items": [{"path": p, "rel": os.path.relpath(p, anchor), "dir": p.is_dir()} for p in items],
        "excludes": list(DEFAULT_EXCLUDES) + [str(x) for x in config.get("exclude") or []],
    }


def entry_rel(layout: dict) -> str:
    """The CLI entry script the remote runs: the same wrapper as here (RWD loads unitap_ext through it)."""
    anchor = layout["anchor"]
    candidates = [Path(sys.argv[0]).resolve()]
    from .identity import cli_package_root
    candidates.append(cli_package_root() / "cli" / "unitap.py")
    for candidate in candidates:
        if candidate.is_file() and anchor in candidate.parents:
            return os.path.relpath(candidate, anchor)
    raise RemoteError("remote_entry_outside_mirror", "The unitap CLI is not inside the mirrored project tree.",
                      {"argv0": sys.argv[0], "anchor": str(anchor)})


# --- placements (per agent session and project, on this Mac) ---------------------------------------------

def _placements_path() -> Path:
    return Path.home() / ".unitap" / "remote-placements.json"


@contextlib.contextmanager
def _placements_locked():
    path = _placements_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and now - float(v.get("updatedAt") or 0) < PLACEMENT_TTL_SECONDS}
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _placement_key(session: str, project: Path) -> str:
    return f"{session}|{Path(project).resolve()}"


def get_placement(session: str | None, project: Path) -> dict | None:
    if not session:
        return None
    with _placements_locked() as data:
        value = data.get(_placement_key(session, project))
    return dict(value) if value else None


def save_placement(session: str, project: Path, placement: dict) -> None:
    with _placements_locked() as data:
        data[_placement_key(session, project)] = dict(placement, updatedAt=time.time())


def drop_placement(session: str | None, project: Path) -> dict | None:
    if not session:
        return None
    with _placements_locked() as data:
        return data.pop(_placement_key(session, project), None)


# --- probing and choosing ---------------------------------------------------------------------------------

def call_helper(config: dict, action: str, params: dict, timeout: float = 60) -> dict:
    target = config.get("ssh")
    if not target:
        return remote_helper.ACTIONS[action](params)
    source = Path(remote_helper.__file__).read_text(encoding="utf-8")
    command = f"PATH={REMOTE_PATH} python3 - {shlex.quote(action)} {shlex.quote(json.dumps(params))}"
    try:
        result = subprocess.run(["ssh", *SSH_OPTIONS, target, command], input=source, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "host_timeout", "message": f"{target}: no answer in {timeout:g}s"}}
    except OSError as ex:
        return {"ok": False, "error": {"code": "ssh_failed", "message": str(ex)}}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        data = json.loads(lines[-1]) if lines else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": {"code": "host_unreachable" if result.returncode == 255 else "helper_failed",
                                       "message": (result.stderr or result.stdout).strip()[-400:] or f"exit {result.returncode}"}}
    return data


def _minutes(text: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text or "").strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def reserved_now(config: dict, now: time.struct_time | None = None) -> dict | None:
    now = now or time.localtime()
    current = now.tm_hour * 60 + now.tm_min
    for window in config.get("reserved") or []:
        start, end = _minutes(window.get("from")), _minutes(window.get("to"))
        if start is None or end is None:
            continue
        inside = start <= current < end if start <= end else (current >= start or current < end)
        if inside:
            return window
    return None


def evaluate(name: str, config: dict, probe: dict, *, version: str | None, session: str | None,
             is_local: bool, now: time.struct_time | None = None) -> dict:
    """Eligibility and score of one host for starting a new Editor."""
    reasons, notes = [], []
    if not probe.get("ok"):
        return {"host": name, "eligible": False, "score": None,
                "reasons": [(probe.get("error") or {}).get("message") or "unreachable"], "notes": []}
    memory = probe.get("memory") or {}
    free_percent = memory.get("freePercent")
    free_slots = int(probe.get("freeSlots") or 0)
    window = reserved_now(config, now)
    if window and not is_local:
        reasons.append(f"reserved {window.get('from')}-{window.get('to')}: {window.get('why', '')}".strip())
    if version and version not in (probe.get("unityVersions") or []):
        reasons.append(f"Unity {version} not installed")
    if free_slots < 1:
        reasons.append(f"no free Editor slot ({probe.get('editorCount')}/{probe.get('maxEditors')})")
    minimum = MIN_LOCAL_FREE_PERCENT if is_local else MIN_REMOTE_FREE_PERCENT
    if free_percent is None or free_percent < minimum:
        reasons.append(f"memory free {free_percent}% < {minimum:g}%")
    if config.get("disabled"):
        reasons.append("disabled in hosts.json")
    score = free_slots * 10 + (free_percent or 0) / 10 + float(config.get("priority") or 0) * 5
    idle = probe.get("idleSeconds")
    if config.get("interactive") and idle is not None and idle < ACTIVE_USER_SECONDS:
        notes.append(f"someone is using it (idle {idle}s); Editor windows open on its screen")
        if config.get("avoidWhenActive"):
            reasons.append("in use by a person")
        score -= 15
    mirrors = probe.get("mirrors") or []
    if any((m.get("owner") or {}).get("session") == session for m in mirrors if session):
        score += 5
        notes.append("this session already has a mirror here")
    elif any(m.get("libraryReady") for m in mirrors):
        score += 3
        notes.append("warm mirror available")
    if is_local:
        score += 20  # no sync, results already here
    return {"host": name, "eligible": not reasons, "score": round(score, 2), "reasons": reasons, "notes": notes}


def probe_hosts(hosts: dict, *, key: str | None, timeout: float = 15) -> dict:
    local = local_host_name()

    def one(item):
        name, config = item
        params = {"key": key, "maxEditors": config.get("maxEditors")}
        if name == local or not config.get("ssh"):
            if name != local:
                return name, {"ok": False, "error": {"message": "no ssh target in hosts.json"}}
            return name, remote_helper.probe(params)
        return name, call_helper(config, "probe", params, timeout=timeout)

    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        return dict(pool.map(one, hosts.items()))


def rank_hosts(hosts: dict, probes: dict, *, version: str | None, session: str | None) -> list[dict]:
    local = local_host_name()
    ranked = [evaluate(name, hosts[name], probes[name], version=version, session=session, is_local=name == local)
              for name in hosts]
    ranked.sort(key=lambda r: (not r["eligible"], -(r["score"] or -1e9)))
    return ranked


def _project_version(project: Path) -> str | None:
    try:
        text = (project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"m_EditorVersion:\s*(\S+)", text)
    return match.group(1) if match else None


def local_has_room(project: Path) -> tuple[bool, str]:
    """True when this Mac should take the work: its Editor for the project already runs, or a slot is free."""
    from .editor_slots import editor_slot_snapshot
    from .lease import lease_is_active, read_lease, resolve_session_owner
    from .unity import _is_same_project_path, list_unity_processes

    try:
        running = any(_is_same_project_path(p.get("projectPath"), project) for p in list_unity_processes())
    except Exception:  # noqa: BLE001 - a failed ps must not block the command
        running = False
    if running:
        lease = read_lease(project)
        if lease_is_active(lease) and lease.get("owner") != resolve_session_owner():
            return False, f"this project's Editor here is leased by {lease.get('owner')}"
        return True, "this project's Editor already runs here"
    snapshot = editor_slot_snapshot(project)
    free = (snapshot.get("memory") or {}).get("freePercent")
    if snapshot.get("available") and free is not None and free >= MIN_LOCAL_FREE_PERCENT:
        return True, "free Editor slot here"
    return False, "; ".join(snapshot.get("reasons") or []) or f"memory free {free}%"


def decide(args, project: Path) -> dict | None:
    """The placement to forward this command to, or None to run it here."""
    command = getattr(args, "command", None)
    if command in LOCAL_ONLY_COMMANDS:
        return None
    hosts = load_hosts()
    local = local_host_name()
    wanted = requested_host(args)
    if wanted is None:
        if not hosts or not agent_session_present():
            return None
        wanted = "auto"
    if wanted in ("local", local):
        return None
    session = placement_session() or f"pid:{os.getppid()}"
    placement = get_placement(session, project)
    if wanted == "auto":
        if placement and placement.get("host") in hosts:
            return placement
        if command in NO_PLACEMENT_COMMANDS:
            return None
        room, why = local_has_room(project)
        if room:
            return None
        layout = project_layout(project)
        others = {k: v for k, v in hosts.items() if k != local}
        if not others:
            return None
        probes = probe_hosts(others, key=layout["key"])
        ranked = rank_hosts(others, probes, version=_project_version(project), session=session)
        best = next((r for r in ranked if r["eligible"]), None)
        if best is None:
            print("[unitap] no pool Mac has room (" + "; ".join(f"{r['host']}: {', '.join(r['reasons'])}" for r in ranked)
                  + f"); staying here ({why})", file=sys.stderr, flush=True)
            return None
        print(f"[unitap] this Mac is busy ({why}); running on {best['host']}"
              + (f" ({'; '.join(best['notes'])})" if best["notes"] else ""), file=sys.stderr, flush=True)
        return {"host": best["host"], "label": None, "session": session}
    if wanted not in hosts:
        raise RemoteError("unknown_host", f"Host '{wanted}' is not in {hosts_file()}", {"hosts": sorted(hosts)})
    if placement and placement.get("host") == wanted:
        return placement
    return {"host": wanted, "label": None, "session": session}


# --- transfer and execution -------------------------------------------------------------------------------

def _rsync_shell() -> str:
    return "ssh " + " ".join(SSH_OPTIONS)


def _rsync(args: list[str], timeout: float = 3600) -> None:
    result = subprocess.run(["rsync", "-e", _rsync_shell(), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode not in (0, 24):  # 24: files vanished during transfer (Editor writes); harmless
        raise RemoteError("rsync_failed", (result.stderr or result.stdout).strip()[-600:] or f"rsync exit {result.returncode}",
                          {"args": args[-2:]})


def anchor_links(layout: dict) -> list[list[str]]:
    """The project's symlinks that point into the anchor by absolute path: [path under the project, target under the anchor].

    rsync copies a link's text as is, so every sync turned these back into links to this Mac's paths until the
    helper relinked them. In between, the mirror's running Editor met missing files and stopped on "Opening file
    failed" (RWD MasterDB -> rwd-master on eipoc, 2026-11-01). They are now left out of rsync and kept by relink.
    """
    anchor = str(layout["anchor"]).rstrip("/")
    project = layout["project"]
    links = []
    for top in ("Assets", "Packages", "ProjectSettings"):
        base = project / top
        if not base.is_dir():
            continue
        for folder, dirs, names in os.walk(base, followlinks=False):
            for name in [*dirs, *names]:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    continue
                target = os.readlink(path)
                if target.startswith(anchor + "/") and not any(c in path for c in "*?[\\"):
                    links.append([os.path.relpath(path, project), target[len(anchor) + 1:]])
    return links


def sync_tree(layout: dict, target: str, root: str, links: list[list[str]] | None = None) -> list[str]:
    synced = []
    for item in layout["items"]:
        remote = f"{target}:{root}/{item['rel']}"
        if item["dir"]:
            excludes = layout["excludes"] if item["path"] == layout["project"] else [".git", ".DS_Store"]
            args = ["-a", "--delete", *[f"--exclude={e}" for e in excludes]]
            if links and item["path"] == layout["project"]:
                # Excluded entries are neither copied nor deleted, so the relinked mirror links stay put.
                listing = Path(tempfile.mkstemp(prefix="unitap-links-", suffix=".txt")[1])
                listing.write_text("".join(f"/{rel}\n" for rel, _ in links), encoding="utf-8")
                args.append(f"--exclude-from={listing}")
            try:
                _rsync([*args, str(item["path"]) + "/", remote + "/"])
            finally:
                if links and item["path"] == layout["project"]:
                    listing.unlink(missing_ok=True)
        else:
            _rsync(["-a", str(item["path"]), remote])
        synced.append(item["rel"])
    return synced


def _tar_copy(source: Path, target: str, dest: str, excludes: list[str], timeout: float) -> None:
    """One tar stream over ssh: far faster than openrsync for a fresh copy of many small files (Library)."""
    patterns = [f"--exclude=./{e.strip('/')}" if e.startswith("/") else f"--exclude={e.strip('/')}" for e in excludes]
    pack = subprocess.Popen(["tar", "-cf", "-", *patterns, "-C", str(source), "."], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    unpack = subprocess.run(["ssh", *SSH_OPTIONS, target, shlex.join(["mkdir", "-p", dest]) + " && "
                             + shlex.join(["tar", "-xf", "-", "-C", dest])], stdin=pack.stdout,
                            capture_output=True, text=True, timeout=timeout)
    pack.stdout.close()
    _, pack_err = pack.communicate(timeout=60)
    if unpack.returncode != 0:
        raise RemoteError("copy_failed", (unpack.stderr or "").strip()[-600:] or f"remote tar exit {unpack.returncode}",
                          {"source": str(source)})
    if pack.returncode not in (0, 1):  # 1: a file changed while it was read (Editor running here)
        raise RemoteError("copy_failed", pack_err.decode(errors="replace").strip()[-600:], {"source": str(source)})


def seed_library(layout: dict, target: str, root: str) -> bool:
    library = layout["project"] / "Library"
    if not library.is_dir():
        return False
    _tar_copy(library, target, f"{root}/{layout['projectRel']}/Library", list(LIBRARY_EXCLUDES), timeout=7200)
    return True


def rewrite_args(argv: list[str], anchor: Path, root: str) -> list[str]:
    """Drop --project/--host (the remote gets its own) and point absolute local tree paths into the mirror."""
    out, skip = [], False
    anchor_text = str(anchor)
    for token in argv:
        if skip:
            skip = False
            continue
        if token in ("--project", "--host"):
            skip = True
            continue
        if token.startswith("--project=") or token.startswith("--host="):
            continue
        if anchor_text in token:
            token = re.sub(re.escape(anchor_text) + r"(?![\w.-])", root, token)
        out.append(token)
    return out


def _git_value(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _tracked(path: Path) -> bool:
    parent = path if path.is_dir() else path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return _git_value(parent, "ls-files", "--error-unmatch", "--", str(path)) is not None


def pull_paths(layout: dict, target: str, root: str, rels: list[str]) -> tuple[list[str], list[str]]:
    """Copy mirror paths back to the same place in the local tree; never over git-tracked files."""
    pulled, skipped = [], []
    item_rels = [Path(i["rel"]) for i in layout["items"]]
    project_parts = Path(layout.get("projectRel") or ".").parts
    project_parts = () if project_parts in ((), (".",)) else project_parts
    for rel in rels[:MAX_PULLED_PATHS]:
        rel = os.path.normpath(rel.strip("/")) if rel.strip("/") else ""
        rel_path = Path(rel)
        inside_project = (rel_path.parts[len(project_parts):]
                          if rel_path.parts[:len(project_parts)] == project_parts else ())
        if (not rel or rel == "." or rel.startswith("..")
                or any(rel_path == i or rel_path in i.parents for i in item_rels)
                or (inside_project and inside_project[0] in EDITOR_STATE_DIRS)):
            skipped.append(rel)
            continue
        local = layout["anchor"] / rel
        if _tracked(local):
            skipped.append(rel)
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rsync(["-a", f"{target}:{root}/{rel}", str(local.parent) + "/"], timeout=1800)
            pulled.append(str(local))
        except RemoteError:
            skipped.append(rel)
    return pulled, skipped


def _mirror_paths_in(text: str, root: str) -> list[str]:
    found = []
    for match in re.finditer(re.escape(root) + r"(/[^\s\"'<>|,;\]\)\}]*)", text):
        rel = match.group(1).rstrip(".:")
        if rel not in found:
            found.append(rel)
    return found


def _claim(config: dict, layout: dict, placement: dict, session: str) -> dict:
    mkdirs = sorted({str(Path(i["rel"]).parent) if not i["dir"] else i["rel"] for i in layout["items"]})
    claim = call_helper(config, "claim", {
        "key": layout["key"], "session": session, "origin": local_host_name(), "sourcePath": str(layout["project"]),
        "label": placement.get("label"), "projectRel": layout["projectRel"], "entryRel": entry_rel(layout),
        "mkdirs": mkdirs, "clearOnSeed": [os.path.join(layout["projectRel"], ".tmp")],
    }, timeout=1800)
    if not claim.get("ok"):
        error = claim.get("error") or {}
        raise RemoteError(error.get("code") or "claim_failed", error.get("message") or "Could not claim a mirror",
                          {"host": placement.get("host")})
    return claim


def prepare(placement: dict, layout: dict, *, sync: bool) -> dict:
    """Claim the session's mirror on the placement host and bring the tree (and first Library) over."""
    hosts = load_hosts()
    host = placement["host"]
    config = hosts.get(host) or {}
    target = config.get("ssh")
    if not target:
        raise RemoteError("host_not_remote", f"Host '{host}' has no ssh target in {hosts_file()}")
    session = placement.get("session") or placement_session() or f"pid:{os.getppid()}"
    claim = _claim(config, layout, placement, session)
    root = claim["root"]
    state = {"host": host, "target": target, "label": claim["label"], "root": root, "session": session,
             "created": claim.get("created"), "seeded": claim.get("seeded"), "reaped": claim.get("reaped") or []}
    touch = {"key": layout["key"], "label": claim["label"], "session": session}
    if sync or claim.get("created"):
        started = time.time()
        if claim.get("created") and not claim.get("seeded"):
            print(f"[unitap] first use of {host} for {layout['key']}: copying the project and its Library "
                  "(several minutes once)", file=sys.stderr, flush=True)
            for item in layout["items"]:
                if item["dir"]:  # bulk first copy; the rsync below then only checks
                    excludes = ([e for e in layout["excludes"] if e not in FIRST_COPY_ONLY]
                                if item["path"] == layout["project"] else [".git", ".DS_Store"])
                    _tar_copy(item["path"], target, f"{root}/{item['rel']}", excludes, timeout=7200)
        wanted = anchor_links(layout)
        sync_tree(layout, target, root, wanted)
        links = call_helper(config, "relink", {"root": root, "projectRel": layout["projectRel"],
                                               "source": str(layout["anchor"]), "links": wanted}, timeout=300)
        if not links.get("ok"):
            raise RemoteError("relink_failed", str((links.get("error") or {}).get("message") or links), {"host": host})
        if links.get("danglingCount"):
            print(f"[unitap] {links['danglingCount']} symlink(s) in the {host} mirror point outside the mirrored tree "
                  f"and dangle there, e.g. {links['dangling'][0]}; add their target to the project's sync list",
                  file=sys.stderr, flush=True)
        state["relinked"] = links.get("relinked", 0)
        if not claim.get("libraryReady"):
            seed_library(layout, target, root)
            touch["libraryReady"] = True
        touch["lastSyncAt"] = time.time()
        state["syncSeconds"] = round(time.time() - started, 2)
    call_helper(config, "touch", touch)
    save_placement(session, layout["project"], {"host": host, "label": claim["label"], "root": root,
                                                "session": session, "key": layout["key"]})
    return state


def _remote_env(layout: dict, state: dict) -> list[str]:
    from .lease import resolve_session_owner
    env = {
        "PATH": REMOTE_PATH,
        HOST_ENV: "local",
        "UNITAP_REMOTE_ORIGIN": local_host_name(),
        "UNITAP_REMOTE_MIRROR": f"{state['host']}:{state['label']}",
        "UNITAP_SESSION": resolve_session_owner() or state["session"],
    }
    if (load_hosts().get(state["host"]) or {}).get("interactive"):
        env["UNITAP_NO_FOCUS"] = "1"  # a person uses that screen: never bring the Editor to the front
    commit = _git_value(layout["project"], "rev-parse", "--short=12", "HEAD")
    branch = _git_value(layout["project"], "rev-parse", "--abbrev-ref", "HEAD")
    if commit:
        env["UNITAP_SOURCE_COMMIT"] = commit
    if branch:
        env["UNITAP_SOURCE_BRANCH"] = branch
    for key in PASS_THROUGH_ENV:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return [f"{k}={v}" for k, v in env.items()]


def run_remote(layout: dict, state: dict, argv: list[str]) -> tuple[int, str]:
    root = state["root"]
    command = ["env", *_remote_env(layout, state), "python3", f"{root}/{entry_rel(layout)}",
               "--project", f"{root}/{layout['projectRel']}", *rewrite_args(argv, layout["anchor"], root)]
    process = subprocess.run(["ssh", *SSH_OPTIONS, state["target"], shlex.join(command)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    return process.returncode, process.stdout


def forward(args, placement: dict, project: Path, argv: list[str] | None = None) -> int:
    """Run this unitap invocation in the placement's mirror and print its (path-rewritten) output."""
    from . import qa_repeat
    from .lease import resolve_session_owner

    argv = list(sys.argv[1:] if argv is None else argv)
    layout = project_layout(project)
    command = args.command
    menu = getattr(args, "menuPath", None) if command == "execute_menu" else None
    if menu and qa_repeat.is_qa_menu(menu):
        # The mirror has no .git, so the repeat guard runs here against the real tree.
        from .commands import _check_qa_repeat
        _check_qa_repeat(args, menu)
        if "--allow-repeat" not in argv:
            argv.append("--allow-repeat")
    state = prepare(placement, layout, sync=command not in NO_SYNC_COMMANDS)
    code, stdout = run_remote(layout, state, argv)
    root = state["root"]
    pulled, skipped = pull_paths(layout, state["target"], root, _mirror_paths_in(stdout, root))
    text = stdout.replace(root, str(layout["anchor"]))
    info = {"host": state["host"], "label": state["label"], "mirror": root, "localTree": str(layout["anchor"])}
    for field in ("syncSeconds", "created", "seeded", "reaped"):
        if state.get(field):
            info[field] = state[field]
    if pulled:
        info["pulled"] = pulled
    if skipped:
        info["notPulled"] = skipped
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        data["remote"] = info
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(text)
        print("[unitap] ran on " + json.dumps(info, ensure_ascii=False), file=sys.stderr)
    if code == 0 and menu and isinstance(data, dict) and data.get("ok"):
        qa_repeat.record_run(project, menu, resolve_session_owner())
    return code


# --- commands: hosts / remote --------------------------------------------------------------------------

def _emit(args, payload: dict, code: int = 0) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    setattr(args, "_last_unitap_response", payload)
    if code:
        sys.exit(code)


def _project_from_args(args) -> Path | None:
    return Path(args.project).resolve() if getattr(args, "project", None) else None


def do_hosts(args, _port=None) -> None:
    hosts = load_hosts()
    if not hosts:
        _emit(args, {"ok": False, "error": {"code": "no_hosts", "message": f"No hosts configured: {hosts_file()}"}}, 1)
    project = _project_from_args(args)
    key = project_layout(project)["key"] if project else None
    version = _project_version(project) if project else None
    session = placement_session()
    probes = probe_hosts(hosts, key=key)
    ranked = rank_hosts(hosts, probes, version=version, session=session)
    rows = []
    for rank in ranked:
        probe = probes.get(rank["host"]) or {}
        memory = probe.get("memory") or {}
        rows.append({
            **rank,
            "reachable": bool(probe.get("ok")),
            "cpuCount": probe.get("cpuCount"),
            "memoryGiB": round((memory.get("physicalBytes") or 0) / 1024 ** 3),
            "freePercent": memory.get("freePercent"),
            "swapUsedGiB": round((memory.get("swapUsedBytes") or 0) / 1024 ** 3, 1),
            "loadAverage": probe.get("loadAverage"),
            "editors": probe.get("editorCount"),
            "maxEditors": probe.get("maxEditors"),
            "idleSeconds": probe.get("idleSeconds"),
            "unityVersions": probe.get("unityVersions"),
            "mirrors": [{k: m.get(k) for k in ("label", "ownerActive", "editorPids", "libraryReady")}
                        for m in probe.get("mirrors") or []],
        })
    placement = get_placement(session, project) if project and session else None
    # Same order as `decide`: this Mac while it has room, then the best other Mac.
    local = local_host_name()
    local_ok = any(r["host"] == local and r["eligible"] for r in ranked)
    best = local if local_ok else next((r["host"] for r in ranked if r["eligible"]), None)
    _emit(args, {"ok": True, "local": local_host_name(), "projectVersion": version, "recommended": best,
                 "placement": placement, "hosts": rows})


def do_remote(args, _port=None) -> None:
    project = _project_from_args(args)
    action = args.action
    hosts = load_hosts()
    if action == "prune":
        names = [args.target_host] if args.target_host else [h for h, c in hosts.items() if c.get("ssh")]
        results = {name: call_helper(hosts.get(name) or {}, "prune", {"days": args.days, "dryRun": args.dry_run},
                                     timeout=600) for name in names}
        _emit(args, {"ok": all(r.get("ok") for r in results.values()), "result": results})
        return
    if project is None:
        _emit(args, {"ok": False, "error": {"code": "project_not_found", "message": "Unity project not found"}}, 1)
    session = placement_session() or f"pid:{os.getppid()}"
    placement = get_placement(session, project)
    if action == "place":
        # Decide now, as `launch` would, so a script can take its lease where the Editor will run.
        import argparse
        chosen = decide(argparse.Namespace(command="launch", host=getattr(args, "host", None)), project)
        if chosen is None:
            _emit(args, {"ok": True, "where": local_host_name(), "placement": None})
            return
        state = prepare(chosen, project_layout(project), sync=True)
        _emit(args, {"ok": True, "where": state["host"], "placement": get_placement(state["session"], project),
                     "syncSeconds": state.get("syncSeconds"), "created": state.get("created")})
        return
    if action == "status":
        detail = None
        if placement:
            listing = call_helper(hosts.get(placement["host"]) or {}, "list", {"key": placement.get("key")}, timeout=30)
            mirrors = (listing.get("mirrors") or {}).get(placement.get("key")) or []
            detail = next((m for m in mirrors if m.get("label") == placement.get("label")), None)
        _emit(args, {"ok": True, "session": session, "placement": placement, "mirror": detail,
                     "where": placement["host"] if placement else local_host_name()})
        return
    if placement is None:
        _emit(args, {"ok": True, "placement": None, "message": "No remote placement for this session; nothing to do"})
        return
    layout = project_layout(project)
    config = hosts.get(placement["host"]) or {}
    if action == "release":
        result = call_helper(config, "release", {"key": layout["key"], "label": placement["label"], "session": session,
                                                 "quit": not args.keep_editor}, timeout=300)
        if result.get("ok"):
            drop_placement(session, project)
        _emit(args, {"ok": bool(result.get("ok")), "host": placement["host"], "label": placement["label"],
                     "result": result}, 0 if result.get("ok") else 1)
        return
    target = config.get("ssh")
    if action == "sync":
        state = prepare(placement, layout, sync=True)
        _emit(args, {"ok": True, "host": state["host"], "label": state["label"], "syncSeconds": state.get("syncSeconds")})
        return
    if action == "pull":
        root = placement["root"]
        rels = []
        for raw in args.paths or []:
            path = Path(raw)
            path = (path if path.is_absolute() else Path.cwd() / path).resolve()
            try:
                rels.append(os.path.relpath(path, layout["anchor"]))
            except ValueError:
                continue
        pulled, skipped = pull_paths(layout, target, root, [r for r in rels if not r.startswith("..")])
        _emit(args, {"ok": True, "host": placement["host"], "pulled": pulled, "notPulled": skipped})
        return


def register(subparsers, dispatch_table) -> None:
    p_hosts = subparsers.add_parser("hosts", help="Show free Editor slots, memory and load of every pool Mac (hosts.json)")
    p_hosts.set_defaults(_skip_heartbeat=True)
    dispatch_table["hosts"] = do_hosts

    p_remote = subparsers.add_parser(
        "remote", help="This session's placement on a pool Mac: place/status/release/sync/pull/prune")
    p_remote.add_argument("action", choices=("place", "status", "release", "sync", "pull", "prune"))
    p_remote.add_argument("paths", nargs="*", help="pull: local paths to fetch from the mirror")
    p_remote.add_argument("--keep-editor", action="store_true", help="release: leave the remote Editor running")
    p_remote.add_argument("--days", type=float, default=14, help="prune: remove mirrors unused this many days")
    p_remote.add_argument("--dry-run", action="store_true", help="prune: list only")
    p_remote.add_argument("--target-host", default=None, help="prune: only this host")
    p_remote.set_defaults(_skip_heartbeat=True)
    dispatch_table["remote"] = do_remote
