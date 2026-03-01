using System.IO;
using UnityEngine;

namespace Unitap.Commands
{
    public sealed class CaptureCommand : IUnitapCommand
    {
        const string DefaultOutputPath = "/tmp/unity_gameview.png";

        public object Execute(UnitapRequest request)
        {
            if (!Application.isPlaying)
            {
                return new { success = false, isPlaying = false, error = "PlayMode required" };
            }

            var outputPath = request.Params?["outputPath"]?.ToObject<string>() ?? DefaultOutputPath;
            var superSize = Mathf.Clamp(request.Params?["superSize"]?.ToObject<int>() ?? 1, 1, 4);

            // 前回のファイルを削除（Python側のポーリングで新規書き込みを検知するため）
            if (File.Exists(outputPath))
                File.Delete(outputPath);

            var dir = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                Directory.CreateDirectory(dir);

            ScreenCapture.CaptureScreenshot(outputPath, superSize);

            return new { success = true, outputPath, superSize, requested = true };
        }
    }
}
