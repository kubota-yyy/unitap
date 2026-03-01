import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    HEARTBEAT_READ_RETRY_COUNT,
    HEARTBEAT_READ_RETRY_INTERVAL,
    HEARTBEAT_STALE_SECONDS,
    HEARTBEAT_STALE_SECONDS_COMPILING,
)
from .project import ProjectResolutionError, resolve_project_root


def _script_project_root() -> Path | None:
    try:
        root = Path(__file__).resolve().parents[3]
    except (OSError, IndexError):
        return None

    if (root / "Assets").exists() and (root / "ProjectSettings").exists():
        return root
    return None


def _heartbeat_candidates(project_path: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    # 1) 明示project（解決済み）を優先
    if project_path:
        try:
            root = resolve_project_root(project_path, allow_process_discovery=False)
        except ProjectResolutionError:
            root = None
        if root:
            hb = root / "Library" / "Unitap" / ".heartbeat.json"
            key = str(hb)
            if key not in seen:
                candidates.append(hb)
                seen.add(key)

    # 2) unitap.py の配置パス基準
    script_root = _script_project_root()
    if script_root:
        hb = script_root / "Library" / "Unitap" / ".heartbeat.json"
        key = str(hb)
        if key not in seen:
            candidates.append(hb)
            seen.add(key)

    # 3) 後方互換: cwd 起点探索
    cwd = Path.cwd()
    for p in (cwd, *cwd.parents):
        hb = p / "Library" / "Unitap" / ".heartbeat.json"
        key = str(hb)
        if key in seen:
            continue
        candidates.append(hb)
        seen.add(key)

    return candidates


def _load_heartbeat_json(path: Path) -> dict | None:
    for attempt in range(HEARTBEAT_READ_RETRY_COUNT):
        try:
            raw = path.read_text()
        except OSError:
            return None

        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            try:
                data["_heartbeatMtimeEpoch"] = path.stat().st_mtime
            except OSError:
                pass
            data["_heartbeatPath"] = str(path)
            return data
        except json.JSONDecodeError:
            if attempt + 1 >= HEARTBEAT_READ_RETRY_COUNT:
                return None
            time.sleep(HEARTBEAT_READ_RETRY_INTERVAL)

    return None


def find_heartbeat(project_path: str | None = None) -> dict | None:
    """heartbeat.json を探して読み込む"""
    for path in _heartbeat_candidates(project_path):
        if path.exists():
            data = _load_heartbeat_json(path)
            if data:
                return data
    return None


def check_heartbeat_fresh(heartbeat: dict) -> bool:
    """heartbeat の lastHeartbeat が新鮮かチェック"""
    threshold = HEARTBEAT_STALE_SECONDS_COMPILING if heartbeat.get("isCompiling") else HEARTBEAT_STALE_SECONDS

    ts_str = heartbeat.get("lastHeartbeat", "")
    if ts_str:
        try:
            # C# DateTime は 7桁精度（100ns）だが Python は 6桁（μs）まで。
            # 小数部を 6桁に切り詰めてからパースする。
            normalized = re.sub(r"(\.\d{6})\d+", r"\1", ts_str)
            ts = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age = (now - ts).total_seconds()
            return age < threshold
        except (ValueError, TypeError):
            pass

    # lastHeartbeat が壊れている場合の保険として mtime も評価する
    mtime_epoch = heartbeat.get("_heartbeatMtimeEpoch")
    if isinstance(mtime_epoch, (int, float)):
        age = max(0.0, time.time() - float(mtime_epoch))
        return age < threshold

    return False


