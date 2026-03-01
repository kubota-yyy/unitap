using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// Application.logMessageReceivedThreaded をスレッドセーフに蓄積する。
    /// 固定上限 (5000件) でリングバッファ的に古いものを捨てる。
    /// </summary>
    public sealed class UnitapConsoleCapture : IDisposable
    {
        const int MaxEntries = 5000;

        public struct LogEntry
        {
            public string Message;
            public string StackTrace;
            public LogType Type;
            public DateTime Timestamp;
        }

        readonly ConcurrentQueue<LogEntry> _queue = new();
        int _count;
        int _errorCount;
        int _exceptionCount;
        long _lastClearedAtTicks = DateTime.UtcNow.Ticks;

        public void Start()
        {
            Application.logMessageReceivedThreaded += OnLog;
        }

        public void Dispose()
        {
            Application.logMessageReceivedThreaded -= OnLog;
        }

        public int ErrorCount => System.Threading.Volatile.Read(ref _errorCount)
                                + System.Threading.Volatile.Read(ref _exceptionCount);

        public bool HasErrors => ErrorCount > 0;

        public DateTime LastClearedAtUtc =>
            new(System.Threading.Interlocked.Read(ref _lastClearedAtTicks), DateTimeKind.Utc);

        public DateTime Clear()
        {
            while (_queue.TryDequeue(out _)) { }
            _count = 0;
            _errorCount = 0;
            _exceptionCount = 0;
            var clearedAt = DateTime.UtcNow;
            System.Threading.Interlocked.Exchange(ref _lastClearedAtTicks, clearedAt.Ticks);
            return clearedAt;
        }

        public List<LogEntry> GetEntries(LogType? filter = null, int limit = 200, DateTime? sinceUtc = null)
        {
            var all = _queue.ToArray();
            var result = new List<LogEntry>();
            // 最新 limit 件を返す
            int start = Math.Max(0, all.Length - limit);
            for (int i = start; i < all.Length; i++)
            {
                if (filter.HasValue && all[i].Type != filter.Value) continue;
                if (sinceUtc.HasValue && all[i].Timestamp < sinceUtc.Value) continue;
                result.Add(all[i]);
            }
            return result;
        }

        void OnLog(string message, string stackTrace, LogType type)
        {
            var entry = new LogEntry
            {
                Message = message,
                StackTrace = stackTrace,
                Type = type,
                Timestamp = DateTime.UtcNow
            };
            _queue.Enqueue(entry);

            if (type == LogType.Error) System.Threading.Interlocked.Increment(ref _errorCount);
            else if (type == LogType.Exception) System.Threading.Interlocked.Increment(ref _exceptionCount);

            var c = System.Threading.Interlocked.Increment(ref _count);
            // 上限を超えたら古いものを破棄
            while (c > MaxEntries && _queue.TryDequeue(out var removed))
            {
                if (removed.Type == LogType.Error) System.Threading.Interlocked.Decrement(ref _errorCount);
                else if (removed.Type == LogType.Exception) System.Threading.Interlocked.Decrement(ref _exceptionCount);
                System.Threading.Interlocked.Decrement(ref _count);
                c--;
            }
        }
    }
}
