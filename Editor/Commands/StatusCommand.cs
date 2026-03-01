using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace Unitap.Commands
{
    public sealed class StatusCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var console = UnitapEntry.Console;
            int errorCount = 0, warningCount = 0;
            if (console != null)
            {
                var entries = console.GetEntries(null, 5000);
                foreach (var e in entries)
                {
                    if (e.Type == LogType.Error || e.Type == LogType.Exception || e.Type == LogType.Assert)
                        errorCount++;
                    else if (e.Type == LogType.Warning)
                        warningCount++;
                }
            }

            // CompilationPipeline 経由のコンパイルエラーを加算
            var compileEntries = UnitapCompileErrorCapture.GetEntries();
            foreach (var ce in compileEntries)
            {
                if (ce.Level == "error") errorCount++;
                else if (ce.Level == "warning") warningCount++;
            }

            return new
            {
                isPlaying = EditorApplication.isPlaying,
                isCompiling = EditorApplication.isCompiling,
                isUpdating = EditorApplication.isUpdating,
                isPaused = EditorApplication.isPaused,
                activeScene = EditorSceneManager.GetActiveScene().path,
                unityVersion = Application.unityVersion,
                platform = EditorUserBuildSettings.activeBuildTarget.ToString(),
                hasErrors = errorCount > 0,
                errorCount,
                warningCount,
                loadedSceneCount = UnityEngine.SceneManagement.SceneManager.loadedSceneCount,
                timeSinceStartup = EditorApplication.timeSinceStartup
            };
        }
    }
}
