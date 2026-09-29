# Changelog

## [Unreleased]

- Single source of truth: consumers pin this repository as a `<repo>/unitap` submodule and run `<repo>/scripts/unitap`. Commands fail with `unitap_copy_mismatch` when the CLI is not the copy the project's Unity loads (`UNITAP_ALLOW_FOREIGN_CLI=1` overrides); `doctor` reports the CLI/package link, the pinned vs checked-out submodule commit and dirty state.
- `clone create` checks out submodules (e.g. unitap itself) locally in the clone, and `clone remove` refuses to drop unpushed submodule work.
- Run several Unity Editors side by side: `launch` no longer aborts when another project's Editor is running and only terminates this project's Editor (`--kill-all` is an explicit opt-in; `--kill-project-only` is now the default and kept for compatibility).
- `launch --restart` restarts only an unresponsive Editor (stale or frozen heartbeat); add `--force-restart` to restart a healthy one.
- Match Unity processes by exact `-projectPath` (a sibling such as `game--clone` no longer matches `game`, paths with spaces are parsed); focus only the target project's PID instead of falling back to any Unity.
- Ignore heartbeats written for another project (e.g. copied with Library) and stop falling back to cwd/script heartbeats when `--project` is given, so commands never reach a different Editor.
- Add `lease acquire|renew|release|status` to reserve a project's Editor for one session; exclusive commands from other sessions wait or fail with `editor_leased` (`--ignore-lease` overrides).
- Add `clone create|list|remove`: per-session git worktree (or snapshot) next to the source with an APFS-cloned Library and reset Unitap state; removal refuses uncommitted work or unreferenced commits.
- Add `editors` (all running Editors with memory, heartbeat, lock and lease holders) and `quit` (this project's Editor only).
- `compile_check` no longer stops a Play Mode started by another session (`play_mode_in_use`; `--stop-foreign-play` to override).
- Treat `invoke_inspector_action` and `ui_pointer` input actions as exclusive (status/inspect polling stays lock-free).
- Execution history records the session id, caller PID and lock/lease wait times, and rotates at 8MB (`UNITAP_HISTORY_MAX_BYTES`); the Editor rotates `processed-journal.jsonl` and `async-job-history.jsonl` the same way.
- Agent misuse from execution history: `tool_exec compile_check|get_editor_state|set_play_mode|...` point to the CLI command, `execute_menu File/Quit` redirects to `quit`, missing menu items return `menu_not_found` with `didYouMean`, and `unicli_not_found` explains the alternatives.

- Add `wait_result`: wait for a QA script's done/fail file and stop early on new console errors, Play Mode exit (`--require-playing`) or a stale progress file (`--progress --stall`).
- Keep captured console logs across domain reloads (Play enter/exit, compilation) so Play Mode exceptions stay readable after stopping.
- Fix `read_console --since` dropping the UTC `Z` (Json.NET date conversion) and filtering 9 hours early in JST; apply type filters before `--limit`.
- Add Codex discovery: offline `commands`, live cross-backend search, and `describe` parameter/response schemas including project extensions.
- Add `exec` / `eval` aliases sharing the UniCLI bridge, project locks and redacted request history; allow connection and catalog reads during exclusive operations.
- Consolidate C# tool discovery and invocation using Unity TypeCache; document parameters for all 12 built-in tools and reject duplicate tool names.
- Add repository agent instructions and a consumer integration guide.

- Sync the RWD-tested implementation: Unity 6.6 support, operation locks, diagnostics, execution history, UniCLI bridge and git autosync tooling.
- Select one local transport using `UNITAP_TRANSPORT_PREFERENCE` (default file, pipe, tcp); CLI follows the project heartbeat. This replaces the March simultaneous transport selection.


## [2026-03-19]

### Added
- Pipe + file transport alongside the existing TCP transport
- `UNITAP_TRANSPORT=file|pipe|tcp|auto`
- Heartbeat fields for `pipeName`, `pipeSocketPath`, `fileTransportDir`, `availableTransports`, and `pidFile`
- File-based request/response transport for sandboxed clients that cannot open localhost TCP or AF_UNIX sockets

### Changed
- CLI auto transport selection now prefers a usable local transport instead of assuming TCP only
- Compile error capture ignores stale entries after the source file has changed
- Reconnect handling now works across TCP, pipe, and file transport

## [0.1.0] - 2026-03-01

### Added
- Initial public release
- TCP server with automatic startup via `[InitializeOnLoad]`
- Heartbeat monitoring with domain reload recovery
- Editor.log fallback for offline operation
- Python CLI with 18 built-in commands
- Custom tool system via `[McpForUnityTool]` attribute
- 12 built-in tools (find_assets, inspect_hierarchy, capture_gameview, etc.)
- Extension system via `unitap_ext` package
- Cross-platform support (macOS, Windows, Linux)
