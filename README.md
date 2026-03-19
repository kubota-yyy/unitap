<p align="center">
  <img src="banner.png" alt="Unitap" width="640">
</p>

<p align="center">
  Control Unity Editor via TCP / Pipe / File transport from external tools.<br>
  Zero-config local IPC server + Python CLI for automation.
</p>

## Features

- **Zero config**: TCP / Pipe / File transport starts automatically via `[InitializeOnLoad]`
- **Heartbeat monitoring**: Detects stale connections and domain reloads
- **Editor.log fallback**: Works even when TCP is unavailable (compiling, frozen)
- **Sandbox-friendly local IPC**: `UNITAP_TRANSPORT=file` or auto fallback when TCP / AF_UNIX socket access is blocked
- **Cross-platform**: macOS, Windows, Linux
- **No external dependencies**: Python stdlib only (CLI), Newtonsoft.Json only (C#, bundled with Unity)
- **Custom tool system**: Register your own tools via `[McpForUnityTool]` attribute

## Why not MCP?

MCP (Model Context Protocol) is the standard for connecting AI tools, but Unity Editor has unique constraints that make it a poor fit:

| Challenge | MCP (stdio) | Unitap (local transports) |
|-----------|-------------|---------------------|
| **Domain Reload** | Server process dies, client gets EOF, manual restart needed | Heartbeat detects reload → CLI waits → auto-reconnects on the recovered transport |
| **Editor frozen / compiling** | stdio blocks, client hangs indefinitely | File-based fallback reads `Editor.log` and `compile-errors.json` |
| **Liveness detection** | No built-in mechanism | Heartbeat file updated every 0.8s; stale = editor is dead |
| **Sandboxed clients** | Host/sandbox dependent | Auto-selects TCP / Pipe / File transport based on what the client can use |
| **Multiple editors** | One server per stdio pipe | Port auto-scan (6400-6409) + per-project heartbeat |
| **Non-AI clients** | Requires MCP-compatible host | Any language with local file / socket access works |
| **Extra process** | Needs a bridge process between AI host and Unity | CLI talks directly to Unity, nothing in between |

Unitap is designed for **resilience in hostile conditions** — compilation pauses, domain reloads, and frozen editors are normal in Unity workflows. The heartbeat + fallback architecture keeps the CLI functional even when the editor is temporarily unreachable.

## Requirements

- **Unity**: 2021.3 or later
- **Python**: 3.10 or later (for CLI)

## Installation

### Unity Package Manager (UPM)

Add to your `Packages/manifest.json`:

```json
{
    "dependencies": {
        "com.nilone.unitap": "https://github.com/kubota-yyy/unitap.git#v0.1.0"
    }
}
```

### Local development

```json
{
    "dependencies": {
        "com.nilone.unitap": "file:../path/to/unitap"
    }
}
```

## CLI Usage

Global options must appear before the subcommand:

```bash
python3 cli/unitap.py --wait-lock --lock-timeout 900 compile_check --timeout 60000
```

Exclusive commands keep a project-scoped lock under `Library/Unitap/.editor-op.lock`. If another long-running or destructive operation is already using Unitap, the default behavior is to fail fast with `Error [editor_busy]`. Add `--wait-lock` when you want the caller to queue behind the current lock holder instead of failing immediately.

```bash
# Check editor status
python3 cli/unitap.py status
UNITAP_TRANSPORT=file python3 cli/unitap.py status

# Play/Stop
python3 cli/unitap.py play
python3 cli/unitap.py stop

# Execute menu item
python3 cli/unitap.py execute_menu --menuPath "Assets/Refresh"

# Compile check (clear -> refresh -> wait -> extract errors)
python3 cli/unitap.py compile_check --timeout 60000
python3 cli/unitap.py --wait-lock compile_check --timeout 60000

# Console
python3 cli/unitap.py read_console --type error
python3 cli/unitap.py clear_console

# Capture GameView
python3 cli/unitap.py capture --output /tmp/test.png --superSize 2

# Launch Unity
python3 cli/unitap.py launch
python3 cli/unitap.py launch --restart

# Custom tools
python3 cli/unitap.py tool_list
python3 cli/unitap.py tool_exec --tool find_assets --params '{"query": "Panel", "type": "Prefab"}'
```

For long-running test flows, prefer wrapper commands such as `run_automate_test --wait`, `run_automate_batch`, and `run_playmode_test --wait` instead of raw `tool_exec --tool run_automate_test` / `run_playmode_test`. The wrapper commands hold the CLI lock for the full wait lifecycle, which prevents other clients from injecting `clear`, `stop`, or a second test start midway through the run.

## Heartbeat

`Library/Unitap/.heartbeat.json` exposes the current editor process and available transports.

Current fields include:

- `pid`
- `port`
- `pipeName`
- `pipeSocketPath`
- `fileTransportDir`
- `availableTransports`
- `pidFile`
- `projectPath`
- `projectName`
- `unityVersion`
- `lastHeartbeat`
- `isCompiling`
- `isPlaying`
- `hasErrors`
- `errorCount`

The CLI uses this file to decide whether Unity is alive, whether a domain reload is in progress, and which transport it should try next.

## Custom Tools

Register custom tools that can be invoked via `tool_exec`:

```csharp
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;

[McpForUnityTool("my_tool")]
public static class MyTool
{
    public static object HandleCommand(JObject @params)
    {
        var name = @params["name"]?.ToString() ?? "World";
        return new SuccessResponse($"Hello, {name}!");
    }
}
```

## Built-in Tools

| Tool | Description |
|------|-------------|
| `find_assets` | Search AssetDatabase |
| `inspect_hierarchy` | Get scene GameObject tree |
| `inspect_component` | Read SerializeField values |
| `set_component` | Write SerializeField values (Undo-safe) |
| `validate_prefab` | Detect missing scripts/references |
| `open_scene` | Open scene without dialog |
| `get_project_settings` | Read project settings |
| `capture_gameview` | Capture GameView screenshot |
| `capture_editor_window` | Capture any EditorWindow |
| `capture_sceneview` | Capture SceneView |

## Extension System

The CLI supports extensions via `unitap_ext` Python package. Create a `unitap_ext/` directory with:

```python
# unitap_ext/__init__.py
def register(subparsers, dispatch_table):
    """Register additional CLI commands."""
    p = subparsers.add_parser("my_command")
    p.add_argument("--option", default="value")
    dispatch_table["my_command"] = do_my_command

def do_my_command(args, port):
    pass
```

Optional hooks:
- `pre_heartbeat_hook(args, project_root)` - Called before heartbeat check
- `stale_heartbeat_hook(args, project_root, heartbeat, state)` - Called on stale heartbeat

## Architecture

```
Unity Editor (C#)              CLI (Python)
+------------------+           +------------------+
| UnitapEntry      |           | unitap.py        |
| UnitapTcpHost /  | <--local-> | transport.py    |
| UnitapPipeHost / |           |                 |
| UnitapFileHost   |           |                 |
| UnitapDispatcher |           | cli.py           |
| Commands/        |           | commands.py      |
| Tools/           |           | heartbeat.py     |
+------------------+           +------------------+
```

## Transport Selection

- Default: `auto`. Normally Unitap uses TCP. When localhost TCP is blocked it can fall back to Pipe, and when socket-based IPC is blocked it can fall back to File transport.
- 明示的に Pipe を使う: `UNITAP_TRANSPORT=pipe python3 tools/unitap/unitap.py status`
- 明示的に File transport を使う: `UNITAP_TRANSPORT=file python3 tools/unitap/unitap.py status`
- 明示的に TCP を使う: `UNITAP_TRANSPORT=tcp python3 tools/unitap/unitap.py status`

### File transport

File transport uses:

- `Library/Unitap/file-transport/requests`
- `Library/Unitap/file-transport/processing`
- `Library/Unitap/file-transport/responses`

This is intended for environments where TCP localhost access and AF_UNIX socket connect are both restricted, such as heavily sandboxed automation clients.

## License

MIT
