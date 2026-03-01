using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Unitap
{
    /// <summary>
    /// Suppresses the "Scene has been modified externally. Reload?" dialog
    /// and automatically reloads the scene when modified by external processes.
    /// </summary>
    [InitializeOnLoad]
    public static class AutoReloadExternalSceneChanges
    {
        private static string _trackedScenePath;
        private static long _lastWriteTicks;
        private static bool _ignoreNextChange;

        static AutoReloadExternalSceneChanges()
        {
            EditorApplication.update += OnUpdate;
            EditorSceneManager.sceneSaved += OnSceneSaved;
        }

        private static void OnSceneSaved(Scene scene)
        {
            if (scene.path == _trackedScenePath)
            {
                _ignoreNextChange = true;
                UpdateTimestamp(scene.path);
            }
        }

        private static void OnUpdate()
        {
            if (EditorApplication.isCompiling || EditorApplication.isUpdating) return;

            var scene = EditorSceneManager.GetActiveScene();
            if (string.IsNullOrEmpty(scene.path)) return;

            if (_trackedScenePath != scene.path)
            {
                _trackedScenePath = scene.path;
                UpdateTimestamp(scene.path);
                _ignoreNextChange = false;
                return;
            }

            var fullPath = Path.GetFullPath(scene.path);
            if (!File.Exists(fullPath)) return;

            var currentTicks = File.GetLastWriteTimeUtc(fullPath).Ticks;
            if (currentTicks == _lastWriteTicks) return;

            _lastWriteTicks = currentTicks;

            if (_ignoreNextChange)
            {
                _ignoreNextChange = false;
                return;
            }

            var scenePath = scene.path;
            Debug.Log($"[AutoReload] Scene modified externally, reloading: {scenePath}");
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
        }

        private static void UpdateTimestamp(string scenePath)
        {
            var fullPath = Path.GetFullPath(scenePath);
            _lastWriteTicks = File.Exists(fullPath) ? File.GetLastWriteTimeUtc(fullPath).Ticks : 0;
        }
    }
}
