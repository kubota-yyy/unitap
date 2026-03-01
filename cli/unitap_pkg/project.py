from __future__ import annotations

from pathlib import Path


class ProjectResolutionError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _normalize_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded


def is_unity_project_root(path: Path) -> bool:
    normalized = _normalize_path(path)
    return (normalized / "Assets").exists() and (normalized / "ProjectSettings").exists()


def _iter_path_and_parents(path: Path):
    normalized = _normalize_path(path)
    yield normalized
    for parent in normalized.parents:
        yield parent


def _script_project_root() -> Path | None:
    # tools/unitap/unitap_pkg/project.py -> project root is parents[3]
    try:
        root = _normalize_path(Path(__file__)).parents[3]
    except (OSError, IndexError):
        return None
    return root if is_unity_project_root(root) else None


def _discover_running_unity_project_roots() -> list[Path]:
    from .unity import list_unity_processes

    roots: list[Path] = []
    seen: set[str] = set()

    for proc in list_unity_processes():
        raw = proc.get("projectPath")
        if not raw:
            continue
        normalized = _normalize_path(Path(raw))
        if not is_unity_project_root(normalized):
            continue
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        roots.append(normalized)

    return roots


def resolve_project_root(
    project_path: str | None = None,
    *,
    allow_process_discovery: bool = False,
) -> Path | None:
    # 1) explicit --project (or its parents) has top priority
    if project_path:
        explicit = Path(project_path)
        for candidate in _iter_path_and_parents(explicit):
            if is_unity_project_root(candidate):
                return candidate
        raise ProjectResolutionError(
            "project_not_found",
            f"Unity project not found for --project: {project_path}",
            details={"projectPath": project_path},
        )

    # 2) script location (CWD independent)
    script_root = _script_project_root()
    if script_root:
        return script_root

    # 3) cwd and its parents
    cwd = Path.cwd()
    for candidate in _iter_path_and_parents(cwd):
        if is_unity_project_root(candidate):
            return candidate

    # 4) running Unity processes (single unique candidate only)
    if allow_process_discovery:
        running_roots = _discover_running_unity_project_roots()
        if len(running_roots) == 1:
            return running_roots[0]
        if len(running_roots) > 1:
            raise ProjectResolutionError(
                "project_resolution_ambiguous",
                "Multiple running Unity projects found. Specify --project explicitly.",
                details={"runningProjects": [str(p) for p in running_roots]},
            )

    return None


def resolve_project_path_arg(
    project_path: str | None = None,
    *,
    allow_process_discovery: bool = False,
) -> str | None:
    root = resolve_project_root(project_path, allow_process_discovery=allow_process_discovery)
    return str(root) if root else None
