import unittest
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import editor_log, transport


class EditorLogFallbackTests(unittest.TestCase):
    def test_wait_idle_state_ignores_snapshot_from_other_project(self):
        project_root = Path("/tmp/unitap-project-a")
        snapshot = {
            "source": "editor_log",
            "logPath": "/tmp/global/Editor.log",
            "detectedProjectPaths": ["/tmp/unitap-project-b"],
            "sessionState": "success",
            "isCompiling": True,
            "hasErrors": False,
            "errorCount": 0,
        }

        with patch.object(transport, "find_heartbeat", return_value=None), \
             patch.object(editor_log, "parse_editor_log_snapshot", return_value=snapshot), \
             patch.object(editor_log, "find_project_root", return_value=project_root), \
             patch.object(editor_log, "list_unity_processes", return_value=[]):
            is_compiling, is_updating = transport.extract_wait_idle_state({}, str(project_root))

        self.assertFalse(is_compiling)
        self.assertFalse(is_updating)


if __name__ == "__main__":
    unittest.main()
