using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build;
using UnityEngine;

namespace Unitap
{
    [InitializeOnLoad]
    public static class UnitapBuildTargetGuard
    {
        [Serializable]
        sealed class UnitapSettings
        {
            public string expectedBuildTarget;
        }

        static bool _switching;
        static double _nextRetryAt;
        static string _retryReason;

        static UnitapBuildTargetGuard()
        {
            EditorApplication.delayCall += () => EnsureExpectedBuildTarget("Unitap startup");
        }

        public static void EnsureExpectedBuildTarget(string reason)
        {
            if (_switching)
                return;

            if (EditorApplication.isCompiling || EditorApplication.isUpdating)
            {
                ScheduleRetry(reason);
                return;
            }

            var expected = ReadExpectedBuildTarget();
            if (expected == null)
                return;

            var current = EditorUserBuildSettings.activeBuildTarget;
            if (current == expected.Value)
                return;

            Debug.LogError($"[Unitap] Active build target drift detected during {reason}: {current} -> {expected.Value}. Restoring expected target.");

            try
            {
                _switching = true;
                var group = BuildPipeline.GetBuildTargetGroup(expected.Value);
                if (!EditorUserBuildSettings.SwitchActiveBuildTarget(group, expected.Value))
                    Debug.LogError($"[Unitap] Failed to restore active build target to {expected.Value}");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Unitap] Failed to restore active build target: {ex}");
                ScheduleRetry(reason);
            }
            finally
            {
                _switching = false;
            }
        }

        static void ScheduleRetry(string reason)
        {
            _retryReason = reason;
            _nextRetryAt = EditorApplication.timeSinceStartup + 1.0;
            EditorApplication.update -= RetryBuildTargetGuard;
            EditorApplication.update += RetryBuildTargetGuard;
        }

        static void RetryBuildTargetGuard()
        {
            if (EditorApplication.timeSinceStartup < _nextRetryAt)
                return;

            EditorApplication.update -= RetryBuildTargetGuard;
            EnsureExpectedBuildTarget(_retryReason);
        }

        static BuildTarget? ReadExpectedBuildTarget()
        {
            var env = Environment.GetEnvironmentVariable("UNITAP_EXPECTED_BUILD_TARGET");
            if (TryParseBuildTarget(env, out var envTarget))
                return envTarget;

            var settingsPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "ProjectSettings", "UnitapSettings.json"));
            if (!File.Exists(settingsPath))
                return null;

            try
            {
                var settings = JsonUtility.FromJson<UnitapSettings>(File.ReadAllText(settingsPath));
                if (settings != null && TryParseBuildTarget(settings.expectedBuildTarget, out var configuredTarget))
                    return configuredTarget;
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Unitap] Failed to read UnitapSettings.json: {ex}");
            }

            return null;
        }

        static bool TryParseBuildTarget(string raw, out BuildTarget target)
        {
            target = default;
            if (string.IsNullOrWhiteSpace(raw))
                return false;

            var normalized = raw.Trim();
            if (string.Equals(normalized, "iPhone", StringComparison.OrdinalIgnoreCase))
                normalized = "iOS";

            if (Enum.TryParse(normalized, ignoreCase: true, out target))
                return true;

            Debug.LogError($"[Unitap] Unknown expectedBuildTarget: {raw}");
            return false;
        }
    }

    public sealed class UnitapBuildTargetChangeGuard : IActiveBuildTargetChanged
    {
        public int callbackOrder => int.MaxValue;

        public void OnActiveBuildTargetChanged(BuildTarget previousTarget, BuildTarget newTarget)
        {
            UnitapBuildTargetGuard.EnsureExpectedBuildTarget($"active target changed ({previousTarget} -> {newTarget})");
        }
    }
}
