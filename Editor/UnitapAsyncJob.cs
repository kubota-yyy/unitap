using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// compile_check / wait_idle の非同期ジョブ管理。
    /// メインスレッドをブロックせず EditorApplication.update でポーリングし、
    /// ジョブ状態をファイルに永続化してドメインリロードを跨ぐ。
    /// </summary>
    public static class UnitapAsyncJob
    {
        static readonly string JobFilePath = Path.Combine(
            Application.dataPath, "..", "Library", "Unitap", "async-job.json");

        // Unity compiler error pattern: Assets/path/file.cs(line,col): error CS0001: message
        static readonly Regex CompilerMessagePattern = new(
            @"^(?<file>Assets/.+?)\((?<line>\d+),(?<col>\d+)\):\s+(?<level>error|warning)\s+(?<code>\w+):\s+(?<msg>.+)$",
            RegexOptions.Compiled);

        static JobState _current;
        static double _lastPollTime;
        const double PollIntervalSeconds = 1.0;
        const int RequiredConsecutive = 3;
        const double CompileCheckGraceSeconds = 5.0;

        [Serializable]
        class JobState
        {
            public string jobId;
            public string command;   // "compile_check" or "wait_idle"
            public string status;    // "running" or "completed"
            public string startedAtUtc;
            public int timeoutMs;
            public int consecutive;
            public bool compileStarted;
            public long? compileStartObservedAtMs;
            public JObject result;   // 完了時の結果
        }

        /// <summary>
        /// 新規ジョブを開始する。EditorApplication.update に登録。
        /// </summary>
        public static string StartNew(string command, int timeoutMs)
        {
            _current = new JobState
            {
                jobId = Guid.NewGuid().ToString("N").Substring(0, 12),
                command = command,
                status = "running",
                startedAtUtc = DateTime.UtcNow.ToString("O"),
                timeoutMs = timeoutMs,
                consecutive = 0,
                compileStarted = false,
                compileStartObservedAtMs = null,
                result = null
            };
            _lastPollTime = EditorApplication.timeSinceStartup;
            Save();
            EditorApplication.update -= Poll;
            EditorApplication.update += Poll;
            return _current.jobId;
        }

        /// <summary>
        /// ジョブの現在の状態を返す。
        /// </summary>
        public static object GetStatus(string jobId)
        {
            // メモリ上にあればそれを使う
            if (_current != null && _current.jobId == jobId)
            {
                if (_current.status == "running")
                    return new { jobId, status = "running" };

                return _current.result?.ToObject<object>()
                    ?? new { jobId, status = _current.status };
            }

            // ファイルから読み込み
            var loaded = Load();
            if (loaded != null && loaded.jobId == jobId)
            {
                if (loaded.status == "running")
                    return new { jobId, status = "running" };

                return loaded.result?.ToObject<object>()
                    ?? new { jobId, status = loaded.status };
            }

            return new { jobId, status = "not_found" };
        }

        /// <summary>
        /// 実行中のジョブがあるか返す。
        /// </summary>
        public static bool HasRunningJob(out string jobId)
        {
            if (_current != null && _current.status == "running")
            {
                jobId = _current.jobId;
                return true;
            }

            var loaded = Load();
            if (loaded != null && loaded.status == "running")
            {
                jobId = loaded.jobId;
                return true;
            }

            jobId = null;
            return false;
        }

        /// <summary>
        /// ドメインリロード後にジョブを復帰する。UnitapEntry.Initialize()から呼ぶ。
        /// </summary>
        public static void RestoreIfNeeded()
        {
            var loaded = Load();
            if (loaded == null || loaded.status != "running")
                return;

            _current = loaded;

            // コンパイルが既に完了していれば即座に完了判定
            if (!EditorApplication.isCompiling && !EditorApplication.isUpdating)
            {
                if (_current.command == "compile_check" && !_current.compileStarted)
                {
                    // ドメインリロードまで到達していれば compile_check は開始済み。
                    // Poll() が isCompiling を観測する前に reload されたケースを救済する。
                    _current.compileStarted = true;
                    _current.compileStartObservedAtMs ??= 0;
                }
                // ドメインリロード直後はidle確定なので即完了扱い
                Complete();
                return;
            }

            // まだコンパイル中ならポーリング再開
            _lastPollTime = EditorApplication.timeSinceStartup;
            EditorApplication.update -= Poll;
            EditorApplication.update += Poll;
        }

        static void Poll()
        {
            if (_current == null || _current.status != "running")
            {
                EditorApplication.update -= Poll;
                return;
            }

            // 1秒間隔でチェック（ブロックなし）
            if (EditorApplication.timeSinceStartup - _lastPollTime < PollIntervalSeconds)
                return;
            _lastPollTime = EditorApplication.timeSinceStartup;

            // タイムアウト判定
            var started = DateTime.Parse(_current.startedAtUtc, null,
                System.Globalization.DateTimeStyles.RoundtripKind);
            if ((DateTime.UtcNow - started).TotalMilliseconds > _current.timeoutMs)
            {
                Complete(timedOut: true);
                return;
            }

            var elapsedMs = (long)(DateTime.UtcNow - started).TotalMilliseconds;
            bool isCompilingNow = EditorApplication.isCompiling || EditorApplication.isUpdating;
            if (_current.command == "compile_check" && !_current.compileStarted && isCompilingNow)
            {
                _current.compileStarted = true;
                _current.compileStartObservedAtMs = elapsedMs;
            }

            // idle判定
            bool idle = !EditorApplication.isCompiling && !EditorApplication.isUpdating;
            if (idle)
            {
                // compile_check はグレース期間中は idle カウントしない
                // AssetDatabase.Refresh() → isCompiling=true になるまで遅延があるため
                if (_current.command == "compile_check")
                {
                    var elapsed = (DateTime.UtcNow - started).TotalSeconds;
                    if (elapsed < CompileCheckGraceSeconds)
                    {
                        Save();
                        return;
                    }
                }

                _current.consecutive++;
                if (_current.consecutive >= RequiredConsecutive)
                {
                    Complete();
                    return;
                }
            }
            else
            {
                _current.consecutive = 0;
            }

            // 進捗をファイルに保存（ドメインリロードに備える）
            Save();
        }

        static void Complete(bool timedOut = false)
        {
            EditorApplication.update -= Poll;

            var started = DateTime.Parse(_current.startedAtUtc, null,
                System.Globalization.DateTimeStyles.RoundtripKind);
            var elapsedMs = (long)(DateTime.UtcNow - started).TotalMilliseconds;

            object result;
            if (_current.command == "compile_check")
                result = BuildCompileCheckResult(
                    elapsedMs,
                    timedOut,
                    _current.compileStarted,
                    _current.compileStartObservedAtMs
                );
            else
            {
                var isCompiling = EditorApplication.isCompiling;
                var isUpdating = EditorApplication.isUpdating;
                result = new
                {
                    idle = !isCompiling && !isUpdating,
                    isCompiling,
                    isUpdating,
                    timedOut,
                    elapsedMs,
                    status = "completed"
                };
            }

            _current.status = "completed";
            _current.result = JObject.FromObject(result);
            Save();
        }

        struct ErrorItem
        {
            public string file, code, message;
            public int line, column;
            public string DedupKey => $"{file}:{line}:{column}:{code}";
        }

        static object BuildCompileCheckResult(
            long elapsedMs,
            bool timedOut,
            bool compileStarted,
            long? compileStartObservedAtMs
        )
        {
            var errors = new List<object>();
            var warnings = new List<object>();
            var seen = new HashSet<string>();

            void Add(ErrorItem item, bool isError)
            {
                if (!string.IsNullOrEmpty(item.file) && !seen.Add(item.DedupKey)) return;
                var obj = new { item.file, item.line, item.column, item.code, item.message };
                if (isError) errors.Add(obj); else warnings.Add(obj);
            }

            // 1. CompilationPipeline 経由（最も正確なソース）
            var compileEntries = UnitapCompileErrorCapture.GetEntries();
            foreach (var ce in compileEntries)
            {
                var codeMatch = Regex.Match(ce.Message, @"(error|warning)\s+(\w+):\s+(.+)$");
                Add(new ErrorItem
                {
                    file = ce.File, line = ce.Line, column = ce.Column,
                    code = codeMatch.Success ? codeMatch.Groups[2].Value : "",
                    message = codeMatch.Success ? codeMatch.Groups[3].Value : ce.Message
                }, ce.Level == "error");
            }

            // 2. コンソールキャプチャ（重複は除外）
            var console = UnitapEntry.Console;
            if (console != null)
            {
                foreach (var entry in console.GetEntries(null, 500))
                {
                    var match = CompilerMessagePattern.Match(entry.Message);
                    if (match.Success)
                    {
                        Add(new ErrorItem
                        {
                            file = match.Groups["file"].Value,
                            line = int.Parse(match.Groups["line"].Value),
                            column = int.Parse(match.Groups["col"].Value),
                            code = match.Groups["code"].Value,
                            message = match.Groups["msg"].Value
                        }, match.Groups["level"].Value == "error");
                    }
                    else if (entry.Type == LogType.Error || entry.Type == LogType.Exception || entry.Type == LogType.Assert)
                    {
                        errors.Add(new { file = "", line = 0, column = 0, code = "", message = entry.Message });
                    }
                }
            }

            return new
            {
                compiled = !timedOut,
                compileStarted,
                compileStartObservedAtMs,
                hasErrors = errors.Count > 0,
                errors, warnings,
                errorCount = errors.Count,
                warningCount = warnings.Count,
                elapsedMs, timedOut,
                status = "completed"
            };
        }

        static void Save()
        {
            if (_current == null) return;
            try
            {
                var dir = Path.GetDirectoryName(JobFilePath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllText(JobFilePath,
                    JsonConvert.SerializeObject(_current, Formatting.Indented));
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Unitap] AsyncJob save error: {ex.Message}");
            }
        }

        static JobState Load()
        {
            try
            {
                if (!File.Exists(JobFilePath)) return null;
                return JsonConvert.DeserializeObject<JobState>(File.ReadAllText(JobFilePath));
            }
            catch
            {
                return null;
            }
        }
    }
}
