using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;

namespace Unitap.Tools
{
    /// <summary>
    /// シーンを開くMCPカスタムツール
    /// </summary>
    [McpForUnityTool("open_scene")]
    public static class OpenSceneTool
    {
        public static object HandleCommand(JObject @params)
        {
            var scenePath = @params["scenePath"]?.ToString();
            if (string.IsNullOrEmpty(scenePath))
            {
                return new ErrorResponse("scenePath is required");
            }

            // シーンファイルの存在確認
            if (!System.IO.File.Exists(scenePath))
            {
                // Assets/から始まる相対パスの場合
                var fullPath = System.IO.Path.Combine(
                    System.IO.Path.GetDirectoryName(UnityEngine.Application.dataPath),
                    scenePath
                );
                if (!System.IO.File.Exists(fullPath))
                {
                    return new ErrorResponse($"Scene not found: {scenePath}");
                }
            }

            // 現在のシーンに変更があれば自動保存（ダイアログなし）
            if (EditorSceneManager.GetActiveScene().isDirty)
            {
                EditorSceneManager.SaveOpenScenes();
            }

            // シーンを開く（ダイアログなし）
            EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);

            return new SuccessResponse($"Scene opened: {scenePath}");
        }
    }
}
