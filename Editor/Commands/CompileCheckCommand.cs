using System.Collections.Generic;
using UnityEditor;
using UnityEditor.Compilation;

namespace Unitap.Commands
{
    public sealed class CompileCheckCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            // ポーリング: jobId指定があれば状態を返す
            var jobId = request.Params?["jobId"]?.ToObject<string>();
            if (!string.IsNullOrEmpty(jobId))
                return UnitapAsyncJob.GetStatus(jobId);

            // PlayMode中はコンパイルチェック不可（CLI側で自動stop→リトライする）
            if (EditorApplication.isPlaying)
            {
                return new
                {
                    compiled = false,
                    hasErrors = true,
                    errors = new[] { new { file = "", line = 0, column = 0, code = "PLAY_MODE",
                        message = "Cannot compile check while in Play mode. Stop Play mode first." } },
                    warnings = new List<object>(),
                    errorCount = 1,
                    warningCount = 0,
                    elapsedMs = 0L,
                    timedOut = false,
                    compileStarted = false,
                    compileStartObservedAtMs = (long?)null,
                    isPlaying = true
                };
            }

            // 既にジョブ実行中なら既存のjobIdを返す
            if (UnitapAsyncJob.HasRunningJob(out var existingJobId))
                return new { jobId = existingJobId, status = "running" };

            var timeoutMs = request.Params?["timeoutMs"]?.ToObject<int>() ?? 60000;

            // コンソールクリア + コンパイルエラーキャプチャクリア + コンパイルトリガー
            UnitapNativeConsole.Clear();
            UnitapEntry.Console?.Clear();
            UnitapCompileErrorCapture.Clear();
            AssetDatabase.Refresh();
            // 明示的にコンパイルを要求（非フォーカス時のコンパイル開始遅延を防止）
            CompilationPipeline.RequestScriptCompilation();

            // 非同期ジョブ開始（即座に返る）
            var newJobId = UnitapAsyncJob.StartNew("compile_check", timeoutMs);
            return new { jobId = newJobId, status = "running" };
        }
    }
}
