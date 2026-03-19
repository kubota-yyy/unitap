# Changelog

## [Unreleased]

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
