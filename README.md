# Unitap

Control Unity Editor via TCP from external tools. Unitap is a zero-config TCP server that runs inside the Unity Editor, paired with a Python CLI for automation.

## Features

- **Zero config**: TCP server starts automatically via `[InitializeOnLoad]`
- **Heartbeat monitoring**: Detects stale connections and domain reloads
- **Editor.log fallback**: Works even when TCP is unavailable (compiling, frozen)
- **Cross-platform**: macOS, Windows, Linux
- **No external dependencies**: Python stdlib only (CLI), Newtonsoft.Json only (C#, bundled with Unity)
- **Custom tool system**: Register your own tools via `[McpForUnityTool]` attribute

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
| UnitapTcpHost    | <--TCP--> | transport.py     |
| UnitapDispatcher |           | cli.py           |
| Commands/        |           | commands.py      |
| Tools/           |           | heartbeat.py     |
+------------------+           +------------------+
```

## License

MIT
