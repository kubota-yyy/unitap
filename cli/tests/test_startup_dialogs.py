import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unitap_pkg.unity import _SAFE_MODE_DIALOG_MATCHER, _STARTUP_DIALOG_DISMISS_SCRIPT


@unittest.skipUnless(sys.platform == "darwin", "AppleScript requires macOS")
class SafeModeDialogTests(unittest.TestCase):
    def test_dialog_detection_with_missing_or_generic_window_titles(self):
        cases = [
            # Unity's native alert may expose no meaningful AX window title.
            ("", ["Ignore", "Quit", "Enter Safe Mode"], True),
            ("Unity", ["Ignore", "Quit", "Enter Safe Mode"], True),
            ("Unity Enter Safe Mode?", ["Ignore", "Quit"], True),
            ("Enter Safe Mode?", ["Quit", "Enter Safe Mode"], False),
            ("Scene changed on disk", ["Reload", "Ignore"], False),
            ("Save the Safe Mode scene?", ["Save", "Ignore"], False),
            ("", ["Ignore"], False),
        ]
        for text, buttons, expected in cases:
            with self.subTest(text=text, buttons=buttons):
                button_list = "{" + ", ".join(json.dumps(b) for b in buttons) + "}"
                source = (
                    _SAFE_MODE_DIALOG_MATCHER
                    + f"\nreturn isSafeModeDialog({json.dumps(text)}, {button_list})\n"
                )
                result = subprocess.run(
                    ["osascript", "-e", source],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(str(expected).lower(), result.stdout.strip())

    def test_complete_watcher_script_compiles(self):
        # Compile only: never click a live Editor's dialogs during this test.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["osacompile", "-o", str(Path(tmp) / "watcher.scpt"),
                 "-e", _STARTUP_DIALOG_DISMISS_SCRIPT],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(0, result.returncode, result.stderr)
