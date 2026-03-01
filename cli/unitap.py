#!/usr/bin/env python3
"""
unitap - Unity Editor control CLI

Usage:
    unitap status
    unitap play
    unitap stop
    unitap execute_menu --menuPath "Assets/Refresh"
    unitap refresh
    unitap focus
    unitap wait_idle [--timeout 30000]
    unitap read_console [--type error] [--limit 100] [--since-last-clear | --since 2026-02-15T00:00:00Z]
    unitap clear_console
    unitap cancel
    unitap tool_list
    unitap tool_exec --tool capture_gameview --params '{"outputPath":"/tmp/test.png"}'
    unitap compile_check [--timeout 60000] [--focus-unity|--no-focus-unity] [--focus-wait-ms 350]
    unitap capture [--output /tmp/test.png] [--superSize 2]
    unitap capture_editor [--output /tmp/editor.png] [--window "Inspector"]
    unitap launch [--restart] [--ignore-compiler-errors] [--no-kill] [--kill-project-only] [--no-wait] [--wait-timeout 180]
"""

from unitap_pkg.cli import main

if __name__ == "__main__":
    main()
