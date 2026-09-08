def classify_unity_unavailability(
    process_running: bool,
    editor_activity: dict | None = None,
    *,
    state: str | None = None,
) -> dict:
    details: dict = {}
    if state:
        details["state"] = state

    if isinstance(editor_activity, dict):
        for key in (
            "logPath",
            "logAgeSeconds",
            "recentActivity",
            "sessionState",
            "isCompiling",
            "hasErrors",
            "errorCount",
            "warningCount",
        ):
            if key in editor_activity and editor_activity[key] is not None:
                details[key] = editor_activity[key]

    if not process_running:
        return {
            "code": "unity_not_running",
            "message": "Unity is not running.",
            "details": details,
        }

    if isinstance(editor_activity, dict) and editor_activity.get("recentActivity") is False:
        return {
            "code": "unity_unresponsive",
            "message": (
                "Unity process exists but Unitap heartbeat is unavailable and Editor.log is not updating. "
                "Unity is likely hung or crashed."
            ),
            "details": details,
        }

    return {
        "code": "unity_running_but_unitap_unavailable",
        "message": "Unity process is running but Unitap heartbeat/TCP is unavailable.",
        "details": details,
    }
