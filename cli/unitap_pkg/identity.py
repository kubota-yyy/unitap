"""実行中の CLI が、対象プロジェクトの Unity が読み込む unitap と同じコピーかを確かめる。

unitap は各プロジェクトが submodule として固定したコピーを使う。別プロジェクトのコピーや
古い vendor コピーの CLI で操作すると、Editor 側 (C#) と CLI 側の実装がずれ、
kill 範囲やロック方針が食い違う。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PACKAGE_NAME = "com.nilone.unitap"
ALLOW_FOREIGN_ENV = "UNITAP_ALLOW_FOREIGN_CLI"


def cli_package_root() -> Path:
    # cli/unitap_pkg/identity.py -> package root
    return Path(__file__).resolve().parents[2]


def project_package_reference(project_root: Path) -> dict | None:
    """Packages/manifest.json の com.nilone.unitap 参照を解決する。未使用なら None。"""
    project_root = Path(project_root)
    embedded = project_root / "Packages" / PACKAGE_NAME
    if (embedded / "package.json").exists():
        return {"kind": "embedded", "reference": str(embedded), "root": str(embedded.resolve())}

    manifest = project_root / "Packages" / "manifest.json"
    try:
        deps = json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    ref = deps.get(PACKAGE_NAME)
    if not isinstance(ref, str):
        return None
    if not ref.startswith("file:"):
        return {"kind": "remote", "reference": ref, "root": None}
    raw = ref[len("file:"):]
    target = Path(raw) if Path(raw).is_absolute() else project_root / "Packages" / raw
    return {"kind": "file", "reference": ref, "root": str(target.resolve())}


def check_cli_identity(project_root: Path | None) -> dict:
    cli_root = cli_package_root()
    result = {"cliRoot": str(cli_root), "projectPackage": None, "matches": None}
    if project_root is None:
        return result
    reference = project_package_reference(project_root)
    result["projectPackage"] = reference
    if reference is None or reference.get("root") is None:
        return result  # unitap を使っていない / 取得元が git URL 等で比較できない
    result["matches"] = Path(reference["root"]) == cli_root
    return result


def foreign_cli_allowed() -> bool:
    return os.environ.get(ALLOW_FOREIGN_ENV, "").strip() not in ("", "0", "false")


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def package_git_state(package_root: Path) -> dict:
    """package のチェックアウト状態と、親リポジトリが固定しているコミットを返す。"""
    state: dict = {"path": str(package_root)}
    head = _git(package_root, "rev-parse", "HEAD")
    if head is None:
        state["git"] = False
        return state
    state["git"] = True
    state["head"] = head
    state["branch"] = _git(package_root, "symbolic-ref", "-q", "--short", "HEAD")
    state["dirty"] = bool(_git(package_root, "status", "--porcelain", "--untracked-files=no"))
    state["origin"] = _git(package_root, "remote", "get-url", "origin")
    own_top = _git(package_root, "rev-parse", "--show-toplevel")
    superproject = _git(package_root, "rev-parse", "--show-superproject-working-tree")
    if superproject:
        rel = os.path.relpath(own_top or str(package_root), superproject)
        pinned = _git(Path(superproject), "ls-tree", "HEAD", "--", rel)
        pinned_sha = pinned.split()[2] if pinned and len(pinned.split()) >= 3 else None
        state.update({
            "layout": "submodule",
            "superproject": superproject,
            "submodulePath": rel,
            "pinned": pinned_sha,
            "matchesPinned": pinned_sha == head if pinned_sha else None,
        })
    else:
        state["layout"] = "standalone-clone"
    return state
