import unittest

from unitap_pkg.constants import EDITOR_LOG_MAX_AGE_SECONDS
from unitap_pkg.editor_log import summarize_editor_activity_snapshot
from unitap_pkg.runtime_diagnostics import classify_unity_unavailability


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_classify_unity_not_running(self):
        result = classify_unity_unavailability(False, None, state="missing_heartbeat")

        self.assertEqual("unity_not_running", result["code"])
        self.assertEqual("missing_heartbeat", result["details"]["state"])

    def test_classify_unity_unresponsive_when_log_is_stale(self):
        result = classify_unity_unavailability(
            True,
            {
                "recentActivity": False,
                "logPath": "/tmp/Editor.log",
                "logAgeSeconds": 321.0,
            },
            state="launch_timeout",
        )

        self.assertEqual("unity_unresponsive", result["code"])
        self.assertEqual("/tmp/Editor.log", result["details"]["logPath"])
        self.assertFalse(result["details"]["recentActivity"])

    def test_classify_unitap_unavailable_when_log_is_recent(self):
        result = classify_unity_unavailability(
            True,
            {
                "recentActivity": True,
                "logPath": "/tmp/Editor.log",
                "logAgeSeconds": 4.0,
            },
            state="stale",
        )

        self.assertEqual("unity_running_but_unitap_unavailable", result["code"])
        self.assertEqual("stale", result["details"]["state"])

    def test_summarize_editor_activity_snapshot_marks_recent_log(self):
        summary = summarize_editor_activity_snapshot(
            {
                "logPath": "/tmp/Editor.log",
                "logAgeSeconds": EDITOR_LOG_MAX_AGE_SECONDS - 1,
                "sessionState": "running",
                "isCompiling": True,
                "hasErrors": False,
                "errorCount": 0,
                "warningCount": 0,
            }
        )

        self.assertTrue(summary["recentActivity"])
        self.assertEqual("running", summary["sessionState"])

    def test_summarize_editor_activity_snapshot_marks_stale_log(self):
        summary = summarize_editor_activity_snapshot(
            {
                "logPath": "/tmp/Editor.log",
                "logAgeSeconds": EDITOR_LOG_MAX_AGE_SECONDS + 1,
                "sessionState": "unknown",
                "isCompiling": False,
                "hasErrors": True,
                "errorCount": 2,
                "warningCount": 1,
            }
        )

        self.assertFalse(summary["recentActivity"])
        self.assertTrue(summary["hasErrors"])
        self.assertEqual(2, summary["errorCount"])


if __name__ == "__main__":
    unittest.main()
