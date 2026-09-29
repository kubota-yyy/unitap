"""同一プロジェクトを複数セッションで並行して使うための作業用プロジェクト複製。

Unity は 1 つのプロジェクトフォルダを 1 つの Editor でしか開けない。セッションごとに
git worktree (または作業ツリーのスナップショット) を作り、Library を APFS clone で
複製すると、再インポートをほぼ省いて独立した Editor を並行起動できる。

複製先は元プロジェクト (worktree ではリポジトリ) の兄弟に置く。Packages/manifest.json の
`file:../../pkg` のような相対参照が、元と同じ場所を指し続けるようにするため。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .commands import print_unitap_response
from .project import is_unity_project_root
from .unity import list_unity_processes

CLONE_MARKER_NAME = "clone.json"
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# 複製しない (Unity が再生成する / 元の Editor 固有の状態)
_SNAPSHOT_EXCLUDES = {"Library", "Temp", "Logs", "obj"}
# Library 内で複製後に消すもの。Unitap の状態は元 Editor を指しているため必ず消す。
_LIBRARY_RESET = ("Unitap", "ArtifactDB-lock", "SourceAssetDB-lock")


def register(subparsers, dispatch_table) -> None:
    p_clone = subparsers.add_parser(
        "clone",
        help="Create/list/remove a per-session copy of this project (git worktree + APFS-cloned Library)",
    )
    p_clone.add_argument("action", choices=("create", "list", "remove"))
    p_clone.add_argument("--name", default=None, help="create: clone label (default: timestamp)")
    p_clone.add_argument("--dest", default=None, help="create: destination (default: sibling '<name>--<label>'); remove: clone project path")
    p_clone.add_argument(
        "--mode",
        choices=("auto", "worktree", "snapshot"),
        default="auto",
        help="worktree: git worktree at HEAD (default in git repos); snapshot: APFS copy of the working tree",
    )
    p_clone.add_argument("--branch", default=None, help="create/worktree: create this branch instead of a detached HEAD")
    p_clone.add_argument(
        "--include-uncommitted",
        action="store_true",
        help="create/worktree: also copy modified/untracked files so the clone matches the current working tree",
    )
    p_clone.add_argument("--no-library", action="store_true", help="create: do not copy Library (full reimport on first launch)")
    p_clone.add_argument("--force", action="store_true", help="remove: delete even with uncommitted changes or unreferenced commits")
    p_clone.set_defaults(_skip_heartbeat=True)
    dispatch_table["clone"] = do_clone


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)


def _git_toplevel(path: Path) -> Path | None:
    result = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def clone_tree(src: Path, dest: Path) -> str:
    """APFS では clonefile(2) で複製する (容量をほぼ使わず数秒)。失敗時は通常コピー。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        result = _run(["cp", "-cRp", str(src), str(dest)])
        if result.returncode == 0:
            return "clonefile"
        # 途中まで作られた複製を消してから通常コピーへ
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest, ignore_errors=True)
        elif dest.exists() or dest.is_symlink():
            dest.unlink(missing_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest)
    return "copy"


_libc_clonefile = None


def _clonefile_fn():
    global _libc_clonefile
    if _libc_clonefile is None:
        _libc_clonefile = False
        if sys.platform == "darwin":
            try:
                import ctypes

                libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                fn = libc.clonefile
                fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
                fn.restype = ctypes.c_int
                _libc_clonefile = fn
            except (OSError, AttributeError):
                _libc_clonefile = False
    return _libc_clonefile or None


def clone_file(src: Path, dest: Path) -> bool:
    """1 ファイルを clonefile(2) で複製する。できなければ通常コピー。clone できたら True。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fn = _clonefile_fn()
    if fn is not None:
        # CLONE_NOFOLLOW (0x1): symlink はリンク自体を複製する
        if fn(os.fsencode(src), os.fsencode(dest), 0x1) == 0:
            return True
    if src.is_symlink():
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest)
    return False


def _git_paths_z(repo_root: Path, *args: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(repo_root), *args, "-z"], capture_output=True, check=False)
    if result.returncode != 0:
        return []
    return [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


def _populate_worktree(repo_root: Path, dest_repo: Path, include_uncommitted: bool) -> dict:
    """git checkout の代わりに作業ツリーのファイルを clonefile で置き、git で HEAD に整合させる。

    通常の checkout は追跡ファイル全体の実コピー (数 GB) になる。clone なら容量をほぼ使わない。
    """
    paths = set(_git_paths_z(repo_root, "ls-tree", "-r", "--name-only", "HEAD"))
    if include_uncommitted:
        paths |= set(_git_paths_z(repo_root, "ls-files", "--others", "--exclude-standard"))
        paths |= set(_git_paths_z(repo_root, "diff", "--cached", "--name-only", "--diff-filter=A"))
    cloned = copied = 0
    for rel in sorted(paths):
        src = repo_root / rel
        if not src.exists() and not src.is_symlink():
            continue  # 元で削除済み。既定では下の checkout が HEAD から戻す
        if src.is_dir() and not src.is_symlink():
            continue  # submodule 等
        if clone_file(src, dest_repo / rel):
            cloned += 1
        else:
            copied += 1
    # index を HEAD に合わせる (作業ツリーは触らない)
    # submodule.recurse=true の環境でも、未作成の submodule へ再帰させない (後で個別に用意する)
    result = _run(["git", "-c", "submodule.recurse=false", "-C", str(dest_repo), "reset", "-q", "--mixed", "HEAD"])
    if result.returncode != 0:
        raise RuntimeError(f"git reset failed in clone: {result.stderr.strip()}")
    if not include_uncommitted:
        # 元の未コミット変更・削除を HEAD の内容へ戻す (差分のあるファイルだけ書き換わる)
        result = _run(["git", "-c", "submodule.recurse=false", "-C", str(dest_repo), "checkout", "-q", "--", "."])
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed in clone: {result.stderr.strip()}")
    return {"cloned": cloned, "copied": copied}


def _gitlinks(repo_root: Path) -> list[tuple[str, str]]:
    """HEAD の submodule (mode 160000) を (path, commit) で返す。"""
    result = subprocess.run(["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "HEAD"], capture_output=True, check=False)
    links = []
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, name = item.partition(b"\t")
        parts = meta.split()
        if len(parts) == 3 and parts[0] == b"160000":
            links.append((os.fsdecode(name), parts[2].decode()))
    return links


def _checkout_submodules(repo_root: Path, dest_repo: Path, include_uncommitted: bool) -> list[str]:
    """submodule を元のチェックアウトからローカル clone する (ネットワーク不要)。

    unitap のような file: パッケージが submodule の場合、これが無いと複製先で解決できない。
    """
    notes = []
    for rel, pinned in _gitlinks(repo_root):
        src = repo_root / rel
        dest = dest_repo / rel
        if not (src / ".git").exists():
            notes.append(f"submodule {rel}: not initialized in the source; skipped")
            continue
        if dest.exists():
            shutil.rmtree(dest)
        result = _run(["git", "clone", "--quiet", "--local", "--no-checkout", str(src), str(dest)])
        if result.returncode != 0:
            raise RuntimeError(f"submodule clone failed for {rel}: {result.stderr.strip()}")
        target = pinned
        if include_uncommitted:
            target = _run(["git", "-C", str(src), "rev-parse", "HEAD"]).stdout.strip() or pinned
        result = _run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", target])
        if result.returncode != 0:
            raise RuntimeError(f"submodule checkout failed for {rel}: {result.stderr.strip()}")
        origin = _run(["git", "-C", str(src), "remote", "get-url", "origin"]).stdout.strip()
        if origin:
            _run(["git", "-C", str(dest), "remote", "set-url", "origin", origin])
        notes.append(f"submodule {rel} at {target[:10]}")
    return notes


def _nested_repo_problems(repo_root: Path) -> list[str]:
    """clone 内の submodule に、削除すると失われる作業 (未コミット・未 push コミット) が無いか。"""
    problems = []
    for rel, pinned in _gitlinks(repo_root):
        sub = repo_root / rel
        if not (sub / ".git").exists():
            continue
        if _git_lines(sub, "status", "--porcelain"):
            problems.append(f"{rel}: uncommitted changes")
        local_only = _git_lines(sub, "log", "--oneline", "--branches", "--not", "--remotes")
        if local_only:
            problems.append(f"{rel}: {len(local_only)} local branch commit(s) not pushed")
        head = _run(["git", "-C", str(sub), "rev-parse", "HEAD"]).stdout.strip()
        detached = _run(["git", "-C", str(sub), "symbolic-ref", "-q", "HEAD"]).returncode != 0
        if detached and head and head != pinned and not _git_lines(sub, "branch", "--all", "--contains", head):
            problems.append(f"{rel}: detached commit {head[:10]} is not on any branch")
    return problems


def _retarget_symlinks(dest_project: Path, roots: list[tuple[Path, Path]]) -> dict:
    """複製内の絶対パス symlink が元を指していたら、複製内の同じ場所へ張り替える。

    そのままだと複製の Editor が元リポジトリのファイルを読み込み、.meta 等を元へ書き込む。
    """
    retargeted = 0
    external: list[str] = []
    for top in ("Assets", "Packages", "ProjectSettings"):
        base = dest_project / top
        if not base.is_dir():
            continue
        for folder, dirs, names in os.walk(base, followlinks=False):
            for name in [*dirs, *names]:
                link = Path(folder) / name
                if not link.is_symlink():
                    continue
                target = os.readlink(link)
                if not os.path.isabs(target):
                    continue
                for src_root, dst_root in roots:
                    try:
                        rel = Path(target).relative_to(src_root)
                    except ValueError:
                        continue
                    link.unlink()
                    os.symlink(str(dst_root / rel), link)
                    retargeted += 1
                    break
                else:
                    external.append(str(link.relative_to(dest_project)))
    return {"retargeted": retargeted, "external": external}


def _reset_library_state(project: Path) -> list[str]:
    removed = []
    library = project / "Library"
    for name in _LIBRARY_RESET:
        target = library / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(str(target))
        elif target.exists():
            target.unlink(missing_ok=True)
            removed.append(str(target))
    return removed


def unresolved_file_packages(project: Path) -> list[dict]:
    manifest = project / "Packages" / "manifest.json"
    try:
        deps = json.loads(manifest.read_text(encoding="utf-8")).get("dependencies", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    missing = []
    for name, ref in (deps or {}).items():
        if not isinstance(ref, str) or not ref.startswith("file:"):
            continue
        raw = ref[len("file:"):]
        target = Path(raw) if Path(raw).is_absolute() else (project / "Packages" / raw)
        if not target.exists():
            missing.append({"package": name, "reference": ref, "resolved": str(target.resolve())})
    return missing


def _default_label() -> str:
    return datetime.now().strftime("clone-%Y%m%d-%H%M%S")


def _marker_path(project: Path) -> Path:
    return project / "Library" / "Unitap" / CLONE_MARKER_NAME


def read_clone_marker(project: Path) -> dict | None:
    try:
        data = json.loads(_marker_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _copy_git_paths(repo_root: Path, dest_repo: Path, paths: list[str]) -> list[str]:
    copied = []
    for rel in paths:
        rel = rel.rstrip("/")
        if not rel:
            continue
        src = repo_root / rel
        dst = dest_repo / rel
        if not src.exists() and not src.is_symlink():
            if dst.exists() and not dst.is_dir():
                dst.unlink(missing_ok=True)  # 元で削除済みのファイル
            continue
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists() or dst.is_symlink():
            dst.unlink(missing_ok=True)
        clone_tree(src, dst)
        copied.append(rel)
    return copied


def _git_lines(repo_root: Path, *args: str) -> list[str]:
    result = _run(["git", "-C", str(repo_root), *args])
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def create_clone(
    src: Path,
    *,
    label: str,
    mode: str = "auto",
    dest: Path | None = None,
    branch: str | None = None,
    include_uncommitted: bool = False,
    copy_library: bool = True,
) -> dict:
    src = src.resolve()
    repo_root = _git_toplevel(src)
    if mode == "auto":
        mode = "worktree" if repo_root else "snapshot"
    if mode == "worktree" and repo_root is None:
        raise ValueError("worktree mode requires the project to be inside a git repository")

    warnings: list[str] = []
    steps: list[str] = []

    if mode == "worktree":
        rel = src.relative_to(repo_root)
        dest_repo = (dest.resolve() if dest else repo_root.parent / f"{repo_root.name}--{label}")
        dest_project = dest_repo / rel
        if dest_repo.exists():
            raise FileExistsError(f"Destination already exists: {dest_repo}")
        cmd = ["git", "-c", "submodule.recurse=false", "-C", str(repo_root), "worktree", "add", "--no-checkout"]
        cmd += ["-b", branch] if branch else ["--detach"]
        cmd += [str(dest_repo), "HEAD"]
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")
        counts = _populate_worktree(repo_root, dest_repo, include_uncommitted)
        steps.append(f"git worktree add (files: {counts['cloned']} cloned, {counts['copied']} copied)")
        for note in _checkout_submodules(repo_root, dest_repo, include_uncommitted):
            (warnings if "skipped" in note else steps).append(note)
        if counts["copied"]:
            warnings.append(f"{counts['copied']} file(s) were copied without APFS clonefile.")

        # Unity が必要とする gitignore 済みファイル (SDK 等) は worktree に来ないので複製する
        ignored = _git_lines(
            repo_root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "--",
            *(str(rel / d) for d in ("Assets", "Packages", "ProjectSettings")),
        )
        ignored = [p for p in ignored if not p.rstrip("/").endswith(".DS_Store")]
        if ignored:
            _copy_git_paths(repo_root, dest_repo, ignored)
            steps.append(f"copied {len(ignored)} ignored path(s) under Assets/Packages/ProjectSettings")

        if include_uncommitted:
            steps.append("kept uncommitted changes and untracked files from the source working tree")
    else:
        dest_project = dest.resolve() if dest else src.parent / f"{src.name}--{label}"
        dest_repo = None
        if dest_project.exists():
            raise FileExistsError(f"Destination already exists: {dest_project}")
        dest_project.mkdir(parents=True)
        methods = set()
        for entry in sorted(src.iterdir()):
            if entry.name in _SNAPSHOT_EXCLUDES:
                continue
            methods.add(clone_tree(entry, dest_project / entry.name))
        steps.append(f"snapshot copy ({'/'.join(sorted(methods)) or 'empty'})")

    link_roots = [(repo_root, dest_repo)] if mode == "worktree" else [(src, dest_project)]
    links = _retarget_symlinks(dest_project, link_roots)
    if links["retargeted"]:
        steps.append(f"retargeted {links['retargeted']} absolute symlink(s) from the source into the clone")
    if links["external"]:
        warnings.append(
            f"{len(links['external'])} absolute symlink(s) point outside the source and stay shared with it, "
            f"e.g. {links['external'][0]}"
        )

    if copy_library and (src / "Library").is_dir():
        if list_unity_processes(src):
            warnings.append(
                "Library was copied while the source Editor is running. If the clone reports a corrupted "
                "Library, recreate it with --no-library."
            )
        method = clone_tree(src / "Library", dest_project / "Library")
        steps.append(f"Library copied ({method})")
        if method != "clonefile":
            warnings.append("Library was copied without APFS clonefile; this used real disk space.")
    _reset_library_state(dest_project)

    marker = {
        "source": str(src),
        "mode": mode,
        "label": label,
        "branch": branch,
        "gitRoot": str(dest_repo) if dest_repo else None,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "includeUncommitted": bool(include_uncommitted),
        "libraryCopied": bool(copy_library),
    }
    _marker_path(dest_project).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(dest_project).write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")

    missing = unresolved_file_packages(dest_project)
    if missing:
        warnings.append("Some file: packages do not resolve from the clone; fix Packages/manifest.json or choose --dest next to the source.")

    return {
        "projectPath": str(dest_project),
        "gitRoot": str(dest_repo) if dest_repo else None,
        "mode": mode,
        "label": label,
        "steps": steps,
        "unresolvedFilePackages": missing,
        "warnings": warnings,
        "next": [
            f"unitap --project {dest_project} launch",
            f"unitap --project {src} clone remove --dest {dest_project}",
        ],
    }


def list_clones(src: Path) -> list[dict]:
    src = src.resolve()
    repo_root = _git_toplevel(src)
    candidates: list[Path] = []
    for parent, prefix, rel in (
        (src.parent, f"{src.name}--", Path()),
        *(((repo_root.parent, f"{repo_root.name}--", src.relative_to(repo_root)),) if repo_root else ()),
    ):
        for entry in sorted(parent.glob(f"{prefix}*")):
            project = entry / rel
            if project not in candidates:
                candidates.append(project)
    clones = []
    for project in candidates:
        marker = read_clone_marker(project)
        if not marker or Path(marker.get("source", "")).resolve() != src:
            continue
        clones.append({
            "projectPath": str(project),
            "running": bool(list_unity_processes(project)),
            **{k: marker.get(k) for k in ("mode", "label", "branch", "createdAt", "gitRoot")},
        })
    return clones


def remove_clone(src: Path, target: Path, *, force: bool = False) -> dict:
    src = src.resolve()
    target = target.resolve()
    marker = read_clone_marker(target)
    if not marker or Path(marker.get("source", "")).resolve() != src:
        raise PermissionError(f"{target} is not a clone created from {src}; refusing to delete it.")
    if list_unity_processes(target):
        raise RuntimeError("Unity is running for the clone. Run `unitap --project <clone> quit` first.")

    git_root = Path(marker["gitRoot"]) if marker.get("gitRoot") else None
    check_root = git_root or (target if (target / ".git").exists() else None)
    if check_root is not None and not force:
        nested = _nested_repo_problems(check_root)
        if nested:
            raise RuntimeError("Submodule work would be lost: " + "; ".join(nested) + ". Push it or pass --force.")
        dirty = _git_lines(check_root, "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise RuntimeError(
                f"Clone has {len(dirty)} uncommitted change(s). Commit/push them or pass --force to discard."
            )
        detached = _run(["git", "-C", str(check_root), "symbolic-ref", "-q", "HEAD"]).returncode != 0
        if detached:
            head = _run(["git", "-C", str(check_root), "rev-parse", "HEAD"]).stdout.strip()
            containing = _git_lines(check_root, "branch", "--all", "--contains", head)
            if head and not containing:
                raise RuntimeError(
                    f"Detached HEAD {head[:10]} is not on any branch. Create a branch for it or pass --force."
                )

    if marker.get("mode") == "worktree" and git_root is not None:
        source_repo = _git_toplevel(src)
        cmd = ["git", "-C", str(source_repo), "worktree", "remove", "--force", str(git_root)]
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree remove failed: {result.stderr.strip()}")
        removed = str(git_root)
    else:
        shutil.rmtree(target)
        removed = str(target)
    return {"removed": removed, "mode": marker.get("mode"), "branchKept": marker.get("branch")}


def do_clone(args, _port=None) -> None:
    project = Path(args.project) if getattr(args, "project", None) else None
    if project is None or not is_unity_project_root(project):
        print_unitap_response(args, {"ok": False, "error": {"code": "project_not_found", "message": "Specify the source project with --project."}})
        return

    try:
        if args.action == "list":
            result = {"source": str(project.resolve()), "clones": list_clones(project)}
        elif args.action == "remove":
            if not args.dest:
                raise ValueError("clone remove requires --dest <clone project path>")
            result = remove_clone(project, Path(args.dest), force=bool(args.force))
        else:
            label = args.name or _default_label()
            if not _LABEL_PATTERN.match(label):
                raise ValueError("--name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
            marker = read_clone_marker(project)
            if marker:
                raise ValueError(f"{project} is itself a clone of {marker.get('source')}; clone the source instead.")
            result = create_clone(
                project,
                label=label,
                mode=args.mode,
                dest=Path(args.dest) if args.dest else None,
                branch=args.branch,
                include_uncommitted=bool(args.include_uncommitted),
                copy_library=not args.no_library,
            )
    except (ValueError, FileExistsError, PermissionError, RuntimeError, OSError) as ex:
        code = {
            FileExistsError: "destination_exists",
            PermissionError: "not_a_clone",
            ValueError: "invalid_clone_args",
        }.get(type(ex), "clone_failed")
        print_unitap_response(args, {"ok": False, "error": {"code": code, "message": str(ex)}})
        return
    print_unitap_response(args, {"ok": True, "result": result})
