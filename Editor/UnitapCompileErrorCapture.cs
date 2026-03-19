using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// CompilationPipeline.assemblyCompilationFinished 経由でコンパイルエラーを捕捉する。
    /// Application.logMessageReceivedThreaded ではコンパイルエラーが通知されないため、
    /// このクラスでファイルに永続化し、compile_check / read_console から参照する。
    /// </summary>
    [InitializeOnLoad]
    public static class UnitapCompileErrorCapture
    {
        static readonly string FilePath = Path.Combine(
            Application.dataPath, "..", "Library", "Unitap", "compile-errors.json");

        [Serializable]
        class CapturedEntry
        {
            public string file;
            public int line;
            public int column;
            public string level; // "error" or "warning"
            public string message;
            public string timestamp;
        }

        [Serializable]
        class CapturedData
        {
            public List<CapturedEntry> entries = new();
        }

        static UnitapCompileErrorCapture()
        {
            CompilationPipeline.compilationStarted += OnCompilationStarted;
            CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompilationFinished;
        }

        static void OnCompilationStarted(object _)
        {
            Clear();
        }

        static void OnAssemblyCompilationFinished(string assemblyPath, CompilerMessage[] messages)
        {
            var relevant = messages.Where(m =>
                m.type == CompilerMessageType.Error || m.type == CompilerMessageType.Warning).ToArray();
            if (relevant.Length == 0) return;

            var data = Load() ?? new CapturedData();
            foreach (var msg in relevant)
            {
                data.entries.Add(new CapturedEntry
                {
                    file = msg.file ?? "",
                    line = msg.line,
                    column = msg.column,
                    level = msg.type == CompilerMessageType.Error ? "error" : "warning",
                    message = msg.message ?? "",
                    timestamp = DateTime.UtcNow.ToString("O")
                });
            }
            Save(data);
        }

        /// <summary>
        /// キャプチャ済みエントリをクリアする。compile_check 開始時に呼ぶ。
        /// </summary>
        public static void Clear()
        {
            try { if (File.Exists(FilePath)) File.Delete(FilePath); }
            catch { /* ignore */ }
        }

        /// <summary>
        /// キャプチャ済みのコンパイルエラー/警告を返す。
        /// </summary>
        public static List<CompileEntry> GetEntries()
        {
            var data = Load();
            if (data == null) return new List<CompileEntry>();
            return data.entries
                .Where(e => !IsStale(e))
                .Select(e => new CompileEntry
            {
                File = e.file,
                Line = e.line,
                Column = e.column,
                Level = e.level,
                Message = e.message,
                Timestamp = e.timestamp
            }).ToList();
        }

        /// <summary>
        /// コンパイルエラー数を返す（全件ロード不要）。
        /// </summary>
        public static int ErrorCount
        {
            get
            {
                var data = Load();
                if (data == null) return 0;
                return data.entries.Count(e => e.level == "error" && !IsStale(e));
            }
        }

        public struct CompileEntry
        {
            public string File;
            public int Line;
            public int Column;
            public string Level;
            public string Message;
            public string Timestamp;
        }

        static CapturedData Load()
        {
            try
            {
                if (!File.Exists(FilePath)) return null;
                return JsonConvert.DeserializeObject<CapturedData>(File.ReadAllText(FilePath));
            }
            catch { return null; }
        }

        static void Save(CapturedData data)
        {
            try
            {
                var dir = Path.GetDirectoryName(FilePath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllText(FilePath, JsonConvert.SerializeObject(data));
            }
            catch { /* ignore */ }
        }

        static bool IsStale(CapturedEntry entry)
        {
            if (entry == null || string.IsNullOrEmpty(entry.file) || string.IsNullOrEmpty(entry.timestamp))
            {
                return false;
            }

            if (!DateTime.TryParse(entry.timestamp, null, System.Globalization.DateTimeStyles.RoundtripKind, out var capturedAt))
            {
                return false;
            }

            try
            {
                var path = entry.file;
                if (!Path.IsPathRooted(path))
                {
                    path = Path.GetFullPath(Path.Combine(Application.dataPath, "..", path));
                }

                if (!File.Exists(path))
                {
                    return false;
                }

                return File.GetLastWriteTimeUtc(path) > capturedAt.ToUniversalTime();
            }
            catch
            {
                return false;
            }
        }
    }
}
