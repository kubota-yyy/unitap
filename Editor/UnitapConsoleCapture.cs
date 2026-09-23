using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// Application.logMessageReceivedThreaded をスレッドセーフに蓄積する。
    /// 固定上限 (5000件) でリングバッファ的に古いものを捨てる。
    /// ドメインリロード (Play 開始/停止・コンパイル) をまたいで失われないよう、
    /// リロード直前に Persist し、再起動後に Restore する。
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
            // 条件に合うものの最新 limit 件を返す (先に件数で切ると、大量の Log に埋もれた Warning 等が 0 件になる)
            for (int i = all.Length - 1; i >= 0 && result.Count < limit; i--)
            {
                if (filter.HasValue && all[i].Type != filter.Value) continue;
                if (sinceUtc.HasValue && all[i].Timestamp < sinceUtc.Value) continue;
                result.Add(all[i]);
            }
            result.Reverse();
            return result;
        }

        [Serializable] sealed class PersistedEntry { public string m; public string s; public int t; public long at; }
        [Serializable] sealed class PersistedBuffer { public long clearedAt; public List<PersistedEntry> entries = new(); }

        /// <summary>ドメインリロード直前に呼ぶ。Play Mode 中の例外が停止後も read_console で読めるようにする。</summary>
        public void Persist(string path)
        {
            try
            {
                var buffer = new PersistedBuffer { clearedAt = System.Threading.Interlocked.Read(ref _lastClearedAtTicks) };
                foreach (var e in _queue.ToArray())
                    buffer.entries.Add(new PersistedEntry { m = e.Message, s = e.StackTrace, t = (int)e.Type, at = e.Timestamp.Ticks });
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(path, JsonUtility.ToJson(buffer));
            }
            catch (Exception) { /* ログ保存の失敗でリロードを止めない */ }
        }

        /// <summary>Persist したファイルを読み込み、削除する (エディタ再起動時に古いログを復元しないため)。</summary>
        public void Restore(string path)
        {
            try
            {
                if (!File.Exists(path)) return;
                var buffer = JsonUtility.FromJson<PersistedBuffer>(File.ReadAllText(path));
                File.Delete(path);
                if (buffer?.entries == null) return;
                System.Threading.Interlocked.Exchange(ref _lastClearedAtTicks, buffer.clearedAt);
                var live = _queue.ToArray();
                while (_queue.TryDequeue(out _)) { }
                _count = 0; _errorCount = 0; _exceptionCount = 0;
                foreach (var e in buffer.entries)
                    Add(new LogEntry { Message = e.m, StackTrace = e.s, Type = (LogType)e.t, Timestamp = new DateTime(e.at, DateTimeKind.Utc) });
                foreach (var e in live) Add(e);
            }
            catch (Exception) { /* 壊れたファイルは無視して新しいバッファで続行 */ }
        }

        void OnLog(string message, string stackTrace, LogType type)
        {
            Add(new LogEntry
            {
                Message = message,
                StackTrace = stackTrace,
                Type = type,
                Timestamp = DateTime.UtcNow
            });
        }

        void Add(LogEntry entry)
        {
            var type = entry.Type;
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
