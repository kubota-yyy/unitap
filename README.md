<p align="center">
  <img src="banner.png" alt="Unitap" width="640">
</p>

<p align="center">
  Control Unity Editor via TCP / Pipe / File transport from external tools.<br>
  Zero-config local IPC server + Python CLI for automation.
</p>

## Features

- **Zero config**: Local transport starts automatically via `[InitializeOnLoad]`
- **Heartbeat monitoring**: Detects stale connections and domain reloads
- **Editor.log fallback**: Works even when TCP is unavailable (compiling, frozen)
- **Cross-platform**: macOS, Windows, Linux
- **No external dependencies**: Python stdlib only (CLI), Newtonsoft.Json only (C#, bundled with Unity)
- **Agent discovery**: `commands --live --search ...` combines CLI, project tools and UniCLI; `describe ID` returns parameters and types
- **UniCLI integration**: `exec` / `eval` reuse UniCLI through Unitap's project lock, history and JSON envelope (optional dependency)
- **Custom tool system**: Register tools with descriptions and parameter schemas via attributes

## Why not MCP?

MCP (Model Context Protocol) is the standard for connecting AI tools, but Unity Editor has unique constraints that make it a poor fit:

| Challenge | MCP (stdio) | Unitap (direct TCP) |
|-----------|-------------|---------------------|
| **Domain Reload** | Server process dies, client gets EOF, manual restart needed | Heartbeat detects reload → CLI waits → auto-reconnects on new port |
| **Editor frozen / compiling** | stdio blocks, client hangs indefinitely | File-based fallback reads `Editor.log` and `compile-errors.json` |
| **Liveness detection** | No built-in mechanism | Heartbeat file updated every 0.8s; stale = editor is dead |
| **Multiple editors** | One server per stdio pipe | Port auto-scan (6400-6409) discovers all running editors |
| **Non-AI clients** | Requires MCP-compatible host | Any language with a TCP socket works |
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

## UniCLI を unitap から使う

既存の unitap 操作を維持し、汎用コマンドや一時的な C# 評価は `unitap unicli` から明示的に呼び出せます。プロジェクト指定、排他ロック、実行履歴、JSON の成功・失敗形式は unitap に統一します。UniCLI は任意の追加依存です。

[使い分け・導入・検証済みコマンド](docs/unicli-bridge.md)

## Codex / AI agents

Start with the actual capability catalog, then request only the schema you need:

```sh
python3 cli/unitap.py --json commands
python3 cli/unitap.py --project /path/to/UnityProject --json commands --live --search prefab
python3 cli/unitap.py --project /path/to/UnityProject --json describe unicli:GameObject.Find
python3 cli/unitap.py --project /path/to/UnityProject --json exec GameObject.Find '{"name":"Main Camera"}'
python3 cli/unitap.py --project /path/to/UnityProject --json describe tool:find_assets
```

Offline discovery needs neither Unity nor UniCLI. Live discovery reports unavailable backends in `sources` and `partial`, preserves namespaced command IDs, and includes project extensions. It does not focus Unity or wait for an operation lock. Listings are compact; `describe` retains full parameter and nested response schemas.

See [Codex agent guide](docs/agent-guide.md) for workflows, completion checks, timeout handling, extension metadata, and the discovery instruction to place in a consuming project's AGENTS.md. This repository's [AGENTS.md](AGENTS.md) provides the entry point for agents working on Unitap itself.

## CLI Usage

Global options must appear before the subcommand:

```bash
python3 cli/unitap.py --wait-lock --lock-timeout 900 compile_check --timeout 60000
```

Exclusive commands keep a project-scoped lock under `Library/Unitap/.editor-op.lock`. If another long-running or destructive operation is already using Unitap, the default behavior is to wait for the lock. Add `--no-wait-lock` to fail fast with `Error [editor_busy]`, or set `--lock-timeout` to bound the wait.

```bash
# Check editor status
python3 cli/unitap.py status

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

For long-running test flows, prefer wrapper commands such as `run_automate_test --wait`, `run_automate_batch`, and `run_playmode_test --wait` instead of raw `tool_exec --tool run_automate_test` / `run_playmode_test`.

- `run_automate_test --wait` / `run_automate_batch` keep the CLI lock for the wait lifecycle.
- `run_playmode_test --wait` acquires the lock only for `clear` / test start / fallback clear. The wait loop itself does not hold the CLI lock, so `diagnose`, `status`, and other lock-aware callers can keep observing the editor while PlayMode wait is in progress.
- `run_playmode_test --wait` can finish in two fallback modes: `TestResults.xml` completion fallback, or `stale_idle` error when Unity is idle and `TestResults.xml` has not advanced for the configured stale window.

## Custom Tools

Register custom tools that can be invoked via `tool_exec`:

```csharp
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;

[McpForUnityTool("my_tool", Description = "Return a greeting")]
[Unitap.UnitapToolParameter("name", "string", "Name to greet", DefaultValue = "World")]
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
| `invoke_inspector_action` | Invoke component methods, menu items, or UI buttons |
| `list_custom_tools` | List project tools and resources with metadata |

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
| UnitapTcpHost    | <--TCP--> | transport.py     |
| UnitapDispatcher |           | cli.py           |
| Commands/        |           | commands.py      |
| Tools/           |           | heartbeat.py     |
+------------------+           +------------------+
```

## License

MIT
