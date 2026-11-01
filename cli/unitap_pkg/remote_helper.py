"""Runs on each pool Mac (sent over ssh as `python3 - <action> <json>`): resources and project mirrors.

It must work before any unitap copy exists on that Mac, so it uses only the standard library and stays
Python 3.9 compatible. The local unitap also imports it to probe its own Mac the same way.

Mirrors live in ~/.unitap/remote/<key>/<label>/ (the source's anchor layout, so `file:../x` packages resolve)
with an owner file <label>.owner.json beside it. A mirror belongs to one agent session at a time; an owner
not touched for OWNER_STALE_SECONDS gives the mirror up, and its idle Editor is quit to free the slot.
"""
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path.home() / ".unitap" / "remote"
OWNER_STALE_SECONDS = 2 * 3600
GIB = 1024 ** 3
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Library contents tied to the Editor that wrote them; never carried into another mirror.
LIBRARY_RESET = ("Unitap", "ArtifactDB-lock", "SourceAssetDB-lock", "EditorInstance.json", "Search")


def _out(cmd, timeout=8):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def unity_editors():
    """Running Unity Editors (AssetImportWorkers excluded), with their -projectPath."""
    editors = []
    for line in _out(["ps", "-axww", "-o", "pid=,rss=,command="]).splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if not match:
            continue
        command = match.group(3)
        if "/Unity.app/Contents/MacOS/Unity" not in command or "AssetImportWorker" in command:
            continue
        if "-adb2" in command and "-batchMode" in command and "Worker" in command:
            continue
        project = re.search(r"-projectPath\s+(.+?)(?=\s+-[A-Za-z]|$)", command, re.I)
        editors.append({
            "pid": int(match.group(1)),
            "rssBytes": int(match.group(2)) * 1024,
            "projectPath": project.group(1).strip().strip('"') if project else None,
            "batchMode": "-batchmode" in command.lower(),
        })
    return editors


def _memory():
    physical = _out(["sysctl", "-n", "hw.memsize"]).strip()
    pressure = re.search(r"free percentage:\s*(\d+(?:\.\d+)?)%", _out(["memory_pressure"]), re.I)
    swap = re.search(r"used\s*=\s*([\d.]+)([KMGT]?)", _out(["sysctl", "vm.swapusage"]), re.I)
    swap_used = None
    if swap:
        swap_used = round(float(swap.group(1)) * 1024 ** (" KMGT".index(swap.group(2).upper() or " ")))
    return {
        "physicalBytes": int(physical) if physical.isdigit() else None,
        "freePercent": float(pressure.group(1)) if pressure else None,
        "swapUsedBytes": swap_used,
    }


def _idle_seconds():
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', _out(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    return int(match.group(1)) // 1_000_000_000 if match else None


def _unity_versions():
    hub = Path("/Applications/Unity/Hub/Editor")
    try:
        return sorted(p.name for p in hub.iterdir() if (p / "Unity.app/Contents/MacOS/Unity").exists())
    except OSError:
        return []


def _lock():
    BASE.mkdir(parents=True, exist_ok=True)
    handle = open(BASE / ".lock", "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _owner_path(key, label):
    return BASE / key / (label + ".owner.json")


def _owner_active(owner, now):
    return bool(owner) and now - float(owner.get("updatedAt") or 0) < OWNER_STALE_SECONDS


def _mirror_editors(root, editors):
    base = str(root).rstrip("/")
    return [e for e in editors if (e.get("projectPath") or "").rstrip("/") == base
            or (e.get("projectPath") or "").startswith(base + "/")]


def _mirrors(key, editors=None, now=None):
    now = time.time() if now is None else now
    editors = unity_editors() if editors is None else editors
    folder = BASE / key
    result = []
    if not folder.is_dir():
        return result
    for root in sorted(p for p in folder.iterdir() if p.is_dir() and _LABEL.match(p.name)):
        owner = _read_json(_owner_path(key, root.name))
        meta = _read_json(root / ".unitap-mirror.json") or {}
        running = _mirror_editors(root, editors)
        result.append({
            "label": root.name,
            "root": str(root),
            "owner": owner,
            "ownerActive": _owner_active(owner, now),
            "editorPids": [e["pid"] for e in running],
            "lastSyncAt": meta.get("lastSyncAt"),
            "libraryReady": bool(meta.get("libraryReady")),
        })
    return result


def probe(params):
    now = time.time()
    editors = unity_editors()
    memory = _memory()
    limit = params.get("maxEditors")
    if not isinstance(limit, int) or limit < 1:
        limit = max(1, int(memory["physicalBytes"] or 0) // (24 * GIB))
    data = {
        "ok": True,
        "host": socket.gethostname().split(".")[0].lower(),
        "time": now,
        "localTime": time.strftime("%H:%M"),
        "cpuCount": os.cpu_count(),
        "loadAverage": [round(x, 2) for x in os.getloadavg()],
        "memory": memory,
        "maxEditors": limit,
        "editors": editors,
        "editorCount": len(editors),
        "freeSlots": max(0, limit - len(editors)),
        "idleSeconds": _idle_seconds(),
        "consoleUser": _out(["stat", "-f", "%Su", "/dev/console"]).strip() or None,
        "user": os.environ.get("USER"),
        "unityVersions": _unity_versions(),
        "home": str(Path.home()),
    }
    key = params.get("key")
    if isinstance(key, str) and _KEY.match(key):
        data["mirrors"] = _mirrors(key, editors, now)
    return data


def _quit_editor(mirror):
    """Ask the mirror's own unitap to quit its Editor (graceful); fall back to SIGTERM."""
    owner = mirror.get("owner") or {}
    entry, project = owner.get("entry"), owner.get("project")
    if entry and project and Path(entry).exists():
        env = dict(os.environ, UNITAP_HOST="local", UNITAP_SESSION=str(owner.get("session") or "unitap-remote-reap"))
        try:
            subprocess.run([sys.executable, entry, "--project", project, "--json", "--ignore-lease", "quit"],
                           capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for pid in mirror.get("editorPids") or []:
        try:
            os.kill(pid, 0)
            os.kill(pid, 15)
        except OSError:
            pass


def _reset_library(project):
    library = Path(project) / "Library"
    for name in LIBRARY_RESET:
        target = library / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass


def claim(params):
    key, session = params.get("key"), params.get("session")
    if not (isinstance(key, str) and _KEY.match(key)) or not session:
        return {"ok": False, "error": {"code": "invalid_claim", "message": "key and session are required"}}
    project_rel = params.get("projectRel") or "."
    handle = _lock()
    try:
        now = time.time()
        mirrors = _mirrors(key, now=now)
        reaped = []
        for mirror in mirrors:
            if mirror["editorPids"] and not mirror["ownerActive"] and (mirror.get("owner") or {}).get("session") != session:
                _quit_editor(mirror)
                reaped.append(mirror["label"])
        # The same agent session may drive several clones (subagents share the session id): each clone keeps its own
        # mirror, or one clone's sync overwrites the other's tree mid-run (eipoc m2, 2026-11-01).
        source_path = params.get("sourcePath")
        chosen = next((m for m in mirrors if (m.get("owner") or {}).get("session") == session
                       and (m.get("owner") or {}).get("sourcePath") in (None, source_path)), None)
        wanted = params.get("label")
        if chosen is None and wanted:
            chosen = next((m for m in mirrors if m["label"] == wanted and not m["ownerActive"]), None)
        if chosen is None:
            free = [m for m in mirrors if not m["ownerActive"]]
            free.sort(key=lambda m: (bool(m["editorPids"]), -float(m.get("lastSyncAt") or 0)))
            chosen = free[0] if free else None
        created = seeded = False
        if chosen is None:
            used = {m["label"] for m in mirrors}
            number = 1
            while ("m%d" % number) in used:
                number += 1
            label = "m%d" % number
            root = BASE / key / label
            ready = [m for m in mirrors if m.get("libraryReady")]
            ready.sort(key=lambda m: (bool(m["editorPids"]), -float(m.get("lastSyncAt") or 0)))
            if ready:
                # APFS clone: a warm Library in seconds without using disk until files change.
                result = subprocess.run(["cp", "-cR", ready[0]["root"], str(root)], capture_output=True, text=True)
                if result.returncode == 0:
                    seeded = True
                    _reset_library(root / project_rel)
                    for rel in params.get("clearOnSeed") or []:
                        target = root / rel
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target, ignore_errors=True)
                    meta = _read_json(root / ".unitap-mirror.json") or {}
                    meta.update(seededFrom=ready[0]["label"], seededAt=now)
                    _write_json(root / ".unitap-mirror.json", meta)
                else:
                    shutil.rmtree(root, ignore_errors=True)
            if not seeded:
                root.mkdir(parents=True, exist_ok=True)
                _write_json(root / ".unitap-mirror.json", {"createdAt": now, "libraryReady": False})
            created = True
            chosen = {"label": label, "root": str(root), "libraryReady": seeded}
        root = Path(chosen["root"])
        for rel in params.get("mkdirs") or []:
            (root / rel).mkdir(parents=True, exist_ok=True)
        owner = {
            "session": session,
            "origin": params.get("origin"),
            "sourcePath": params.get("sourcePath"),
            "entry": str(root / params["entryRel"]) if params.get("entryRel") else None,
            "project": str(root / project_rel),
            "claimedAt": ((chosen.get("owner") or {}).get("claimedAt") if (chosen.get("owner") or {}).get("session") == session else None) or now,
            "updatedAt": now,
        }
        _write_json(_owner_path(key, chosen["label"]), owner)
        return {"ok": True, "label": chosen["label"], "root": str(root), "created": created, "seeded": seeded,
                "libraryReady": bool(chosen.get("libraryReady")), "reaped": reaped}
    finally:
        handle.close()


def touch(params):
    key, label, session = params.get("key"), params.get("label"), params.get("session")
    if not (isinstance(key, str) and _KEY.match(key) and isinstance(label, str) and _LABEL.match(label)):
        return {"ok": False, "error": {"code": "invalid_mirror", "message": "bad key/label"}}
    handle = _lock()
    try:
        path = _owner_path(key, label)
        owner = _read_json(path)
        if not owner or owner.get("session") != session:
            return {"ok": False, "error": {"code": "mirror_not_owned", "message": "This session no longer owns the mirror"},
                    "owner": owner}
        owner["updatedAt"] = time.time()
        _write_json(path, owner)
        root = BASE / key / label
        meta = _read_json(root / ".unitap-mirror.json") or {}
        for field in ("lastSyncAt", "libraryReady"):
            if field in params:
                meta[field] = params[field]
        _write_json(root / ".unitap-mirror.json", meta)
        return {"ok": True}
    finally:
        handle.close()


def release(params):
    key, label, session = params.get("key"), params.get("label"), params.get("session")
    if not (isinstance(key, str) and _KEY.match(key) and isinstance(label, str) and _LABEL.match(label)):
        return {"ok": False, "error": {"code": "invalid_mirror", "message": "bad key/label"}}
    handle = _lock()
    try:
        mirror = next((m for m in _mirrors(key) if m["label"] == label), None)
        if mirror is None:
            return {"ok": True, "released": False, "message": "mirror not found"}
        owner = mirror.get("owner") or {}
        if owner.get("session") not in (session, None) and not params.get("force"):
            return {"ok": False, "error": {"code": "mirror_not_owned", "message": "Owned by another session"}, "owner": owner}
        if params.get("quit") and mirror["editorPids"]:
            _quit_editor(mirror)
        try:
            _owner_path(key, label).unlink()
        except OSError:
            pass
        return {"ok": True, "released": True, "quitEditor": bool(params.get("quit") and mirror["editorPids"])}
    finally:
        handle.close()


def list_mirrors(params):
    editors = unity_editors()
    keys = [params["key"]] if params.get("key") else sorted(p.name for p in BASE.iterdir() if p.is_dir()) if BASE.is_dir() else []
    return {"ok": True, "host": socket.gethostname().split(".")[0].lower(),
            "mirrors": {key: _mirrors(key, editors) for key in keys if _KEY.match(key)}}


def prune(params):
    """Delete mirrors unused for `days` (no owner, no Editor). Never touches anything outside BASE."""
    days = float(params.get("days") or 14)
    handle = _lock()
    try:
        now, removed = time.time(), []
        for key in (sorted(p.name for p in BASE.iterdir() if p.is_dir()) if BASE.is_dir() else []):
            if not _KEY.match(key):
                continue
            for mirror in _mirrors(key, now=now):
                last = float(mirror.get("lastSyncAt") or 0) or (Path(mirror["root"]).stat().st_mtime)
                if mirror["ownerActive"] or mirror["editorPids"] or now - last < days * 86400:
                    continue
                if params.get("dryRun"):
                    removed.append(mirror["root"])
                    continue
                shutil.rmtree(mirror["root"], ignore_errors=True)
                try:
                    _owner_path(key, mirror["label"]).unlink()
                except OSError:
                    pass
                removed.append(mirror["root"])
        return {"ok": True, "removed": removed, "dryRun": bool(params.get("dryRun"))}
    finally:
        handle.close()


def relink(params):
    """Point the mirror's absolute symlinks at the mirror instead of the source Mac's paths.

    rsync -a copies a link's text as is, so a link to /source/anchor/x dangles here (RWD MasterDB -> rwd-master:
    every Automate failed with "master not found" on eipoc, 2026-11-01). Links into the anchor are rewritten to
    root/x; links elsewhere are reported, since the mirror cannot reach them.
    """
    root, source = Path(params["root"]), str(params["source"]).rstrip("/")
    project = root / params["projectRel"]
    relinked, dangling = 0, []
    for top in ("Assets", "Packages", "ProjectSettings"):
        base = project / top
        if not base.is_dir():
            continue
        for folder, dirs, names in os.walk(base, followlinks=False):
            for name in [*dirs, *names]:
                link = Path(folder) / name
                if not link.is_symlink():
                    continue
                target = os.readlink(link)
                if not target.startswith(source + "/"):
                    if os.path.isabs(target) and not os.path.exists(target):
                        dangling.append(str(link.relative_to(project)))
                    continue
                link.unlink()
                os.symlink(str(root / target[len(source) + 1:]), link)
                relinked += 1
    return {"ok": True, "relinked": relinked, "dangling": dangling[:20], "danglingCount": len(dangling)}


ACTIONS = {"probe": probe, "claim": claim, "touch": touch, "release": release, "list": list_mirrors, "prune": prune,
           "relink": relink}


def main(argv):
    action = argv[1] if len(argv) > 1 else "probe"
    params = json.loads(argv[2]) if len(argv) > 2 else {}
    handler = ACTIONS.get(action)
    if handler is None:
        print(json.dumps({"ok": False, "error": {"code": "unknown_action", "message": action}}))
        return 2
    print(json.dumps(handler(params), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
