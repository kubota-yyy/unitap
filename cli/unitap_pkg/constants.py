import os
import re

HEADER_SIZE = 8
PIPE_HEADER_SIZE = 4
DEFAULT_TIMEOUT_MS = 30000
HEARTBEAT_STALE_SECONDS = 10
HEARTBEAT_STALE_SECONDS_COMPILING = 120
HEARTBEAT_READ_RETRY_COUNT = 3
HEARTBEAT_READ_RETRY_INTERVAL = 0.05
CONNECTION_RETRY_MAX = 45
CONNECTION_RETRY_INTERVAL = 2
LIVENESS_RECONNECT_RETRY_MAX = 5
TCP_TEST_TIMEOUT = 2
PIPE_TEST_TIMEOUT = 2
FILE_TEST_TIMEOUT = 2
POLL_INTERVAL = 1.0
FILE_RESPONSE_POLL_INTERVAL = 0.05
EDITOR_LOG_TAIL_BYTES = 12 * 1024 * 1024
EDITOR_LOG_MAX_AGE_SECONDS = 180
DOTNET_TICKS_AT_UNIX_EPOCH = 621355968000000000
UNITAP_TRANSPORT_ENV = "UNITAP_TRANSPORT"

# Configurable via environment variable
PLAYMODE_RESULTS_DEFAULT_RELATIVE = os.environ.get(
    "UNITAP_PLAYMODE_RESULTS",
    "Library/TestResults.xml",
)

EDITOR_LOG_FALLBACK_COMMANDS = {
    "status",
    "wait_idle",
    "read_console",
    "compile_check",
    "diagnose",
    "heartbeat",
}

COMPILER_MESSAGE_PATTERN = re.compile(
    r"^(?P<file>(?:Assets|Packages|Library)/.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<level>error|warning)\s+(?P<code>\w+):\s+(?P<msg>.+)$",
    re.IGNORECASE,
)
SCRIPT_COMPILATION_START_PATTERN = re.compile(r"^\[ScriptCompilation\] Requested script compilation\b")
SCRIPT_COMPILATION_RESULT_PATTERN = re.compile(r"^\*\*\* Tundra build (?P<result>success|failed)\b", re.IGNORECASE)
SCRIPT_COMPILATION_PROGRESS_PATTERN = re.compile(r"^\[\d+/\d+\s+\d+s\]")
AUTOMATE_TIMESTAMP_PATTERN = re.compile(r"^\d{8}_\d{6}(?:_\d{3})?$")
