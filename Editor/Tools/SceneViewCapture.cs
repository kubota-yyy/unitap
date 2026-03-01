using System.IO;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Unitap.Tools
{
    /// <summary>
    /// SceneViewをキャプチャするツール
    /// </summary>
    [McpForUnityTool("capture_sceneview")]
    public static class SceneViewCapture
    {
        private const string DefaultOutputPath = "/tmp/unity_sceneview.png";

        [MenuItem("Unitap/Capture/SceneView")]
        public static void CaptureSceneViewMenu()
        {
            Capture(DefaultOutputPath, 1920, 1080);
        }

        public static object HandleCommand(JObject @params)
        {
            var outputPath = @params["outputPath"]?.ToString() ?? DefaultOutputPath;
            var width = @params["width"]?.ToObject<int>() ?? 1920;
            var height = @params["height"]?.ToObject<int>() ?? 1080;

            var result = Capture(outputPath, width, height);
            if (result)
                return new SuccessResponse($"SceneViewをキャプチャしました: {outputPath}");
            else
                return new ErrorResponse("SceneViewが見つかりません");
        }

        private static bool Capture(string outputPath, int width, int height)
        {
            var sceneView = SceneView.lastActiveSceneView;
            if (sceneView == null)
            {
                Debug.LogError("SceneViewが見つかりません");
                return false;
            }

            var dir = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                Directory.CreateDirectory(dir);

            var camera = sceneView.camera;
            var rt = new RenderTexture(width, height, 24);
            camera.targetTexture = rt;
            camera.Render();

            RenderTexture.active = rt;
            var texture = new Texture2D(width, height, TextureFormat.RGB24, false);
            texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            texture.Apply();

            camera.targetTexture = null;
            RenderTexture.active = null;

            var bytes = texture.EncodeToPNG();
            File.WriteAllBytes(outputPath, bytes);

            Object.DestroyImmediate(rt);
            Object.DestroyImmediate(texture);

            Debug.Log($"SceneView captured to: {outputPath}");
            return true;
        }
    }
}
