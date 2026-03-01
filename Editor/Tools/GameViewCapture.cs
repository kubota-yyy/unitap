using System.IO;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Unitap.Tools
{
    /// <summary>
    /// GameViewをキャプチャするツール
    /// MenuItemとMCPカスタムツールの両方で利用可能
    /// </summary>
    [McpForUnityTool("capture_gameview")]
    public static class GameViewCapture
    {
        private const string DefaultOutputPath = "/tmp/unity_gameview.png";

        [MenuItem("Unitap/Capture/GameView")]
        public static void CaptureGameView()
        {
            Capture(DefaultOutputPath, 1);
        }

        /// <summary>
        /// MCP経由でのキャプチャ
        /// </summary>
        public static object HandleCommand(JObject @params)
        {
            var outputPath = @params["outputPath"]?.ToString() ?? DefaultOutputPath;
            var superSize = @params["superSize"]?.ToObject<int>() ?? 1;

            if (!Application.isPlaying)
            {
                return new ErrorResponse("Playモード中のみキャプチャ可能です。先にmanage_editor(action='play')を実行してください。");
            }

            Capture(outputPath, superSize);

            return new SuccessResponse($"GameViewをキャプチャしました: {outputPath}");
        }

        /// <summary>
        /// キャプチャ実行
        /// </summary>
        /// <param name="outputPath">出力パス</param>
        /// <param name="superSize">解像度倍率 (1-4)</param>
        private static void Capture(string outputPath, int superSize)
        {
            // ディレクトリが存在しない場合は作成
            var dir = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
            {
                Directory.CreateDirectory(dir);
            }

            superSize = Mathf.Clamp(superSize, 1, 4);
            ScreenCapture.CaptureScreenshot(outputPath, superSize);
            Debug.Log($"GameView captured to: {outputPath} (superSize: {superSize})");
        }
    }
}
