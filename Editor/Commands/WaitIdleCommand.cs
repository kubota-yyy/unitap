namespace Unitap.Commands
{
    /// <summary>
    /// !isCompiling && !isUpdating を1秒間隔で連続3回確認して安定判定。
    /// 非同期ジョブとして即座にレスポンスを返し、CLIがポーリングで完了を待つ。
    /// </summary>
    public sealed class WaitIdleCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            // ポーリング: jobId指定があれば状態を返す
            var jobId = request.Params?["jobId"]?.ToObject<string>();
            if (!string.IsNullOrEmpty(jobId))
                return UnitapAsyncJob.GetStatus(jobId);

            // 既にジョブ実行中なら既存のjobIdを返す
            if (UnitapAsyncJob.HasRunningJob(out var existingJobId))
                return new { jobId = existingJobId, status = "running" };

            var timeoutMs = request.Params?["timeoutMs"]?.ToObject<int>() ?? 30000;

            // 非同期ジョブ開始（即座に返る）
            var newJobId = UnitapAsyncJob.StartNew("wait_idle", timeoutMs);
            return new { jobId = newJobId, status = "running" };
        }
    }
}
