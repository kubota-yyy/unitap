"""Start the Unity Editor inside the logged-in user's GUI session when unitap runs over ssh.

A GUI app spawned directly from an ssh session cannot reach the WindowServer and dies at once, so remote
launches (pool Macs, see remote.py) go through a one-shot launchd job in the `gui/<uid>` domain instead.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

LABEL_PREFIX = "com.nilone.unitap.editor."
ENV_SWITCH = "UNITAP_GUI_LAUNCH"  # 1: always through launchd, 0: never


def wanted(environ: dict | None = None) -> bool:
    env = os.environ if environ is None else environ
    if sys.platform != "darwin":
        return False
    switch = (env.get(ENV_SWITCH) or "").strip()
    if switch in ("0", "false"):
        return False
    if switch in ("1", "true"):
        return True
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY"))


def _launchctl(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)


def _job_pid(domain: str, label: str) -> int | None:
    result = _launchctl("print", f"{domain}/{label}")
    match = re.search(r"^\s*pid = (\d+)", result.stdout or "", re.M)
    return int(match.group(1)) if result.returncode == 0 and match else None


def _cleanup_finished(domain: str, prefix: str) -> None:
    """Unload earlier one-shot jobs of this project whose Editor has exited (never one that still runs)."""
    listing = _launchctl("list").stdout or ""
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(prefix) and parts[0] == "-":
            _launchctl("bootout", f"{domain}/{parts[2]}")


# Keeps a starting Editor from taking the screen of a person using that Mac: while the Editor loads, whenever a
# Unity process becomes frontmost, the app that was in front before the launch is brought back.
_GIVE_BACK_FOCUS = r'''
import subprocess, sys, time
previous, seconds = sys.argv[1], float(sys.argv[2])
def front():
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()
end = time.monotonic() + seconds
while time.monotonic() < end:
    try:
        if front() in ("Unity", "Unity Hub"):  # Unity 6.4+ also starts the Hub for its licence
            subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of process "%s" to true' % previous],
                           capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(1)
'''


def _frontmost_app() -> str | None:
    try:
        result = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first '
                                 'application process whose frontmost is true'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = result.stdout.strip()
    return name if result.returncode == 0 and name and name not in ("Unity", "Unity Hub") and '"' not in name else None


def give_back_focus(seconds: float = 240) -> None:
    previous = _frontmost_app()
    if previous:
        subprocess.Popen([sys.executable, "-c", _GIVE_BACK_FOCUS, previous, str(seconds)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def spawn(command: list[str], env: dict, project_root: Path) -> int:
    uid = os.getuid()
    domain = f"gui/{uid}"
    digest = hashlib.sha1(str(Path(project_root).resolve()).encode()).hexdigest()[:10]
    prefix = f"{LABEL_PREFIX}{digest}."
    label = f"{prefix}{int(time.time() * 1000)}"
    directory = Path.home() / ".unitap" / "gui-launch"
    directory.mkdir(parents=True, exist_ok=True)
    _cleanup_finished(domain, prefix)
    for old in directory.glob(prefix + "*.plist"):
        if _job_pid(domain, old.stem) is None:
            old.unlink(missing_ok=True)
    environment = {k: v for k, v in env.items() if not k.startswith("SSH_") and isinstance(v, str)}
    plist = directory / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(x) for x in command],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "AbandonProcessGroup": True,
        "ProcessType": "Interactive",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }))
    if (env.get("UNITAP_NO_FOCUS") or "").strip() not in ("", "0", "false"):
        give_back_focus()
    result = _launchctl("bootstrap", domain, str(plist))
    if result.returncode != 0:
        raise OSError(f"launchctl bootstrap {domain} failed: {(result.stderr or result.stdout).strip()}"
                      " (is this user logged in at the Mac's screen?)")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        pid = _job_pid(domain, label)
        if pid:
            return pid
        time.sleep(0.25)
    raise OSError(f"Unity did not start through launchd job {label}")
