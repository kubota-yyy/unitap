using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using UnityEngine;

namespace Unitap.Commands
{
    public sealed class ReadConsoleCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var console = UnitapEntry.Console;

            var typeFilter = request.Params?["type"]?.ToString();
            var limit = request.Params?["limit"]?.ToObject<int>() ?? 200;
            var sinceLastClear = request.Params?["sinceLastClear"]?.ToObject<bool>() ?? false;
            var sinceRaw = request.Params?["since"]?.ToObject<string>();
            var normalizedTypeFilter = typeFilter?.ToLowerInvariant();

            LogType? logType = normalizedTypeFilter switch
            {
                "error" => null, // error は Error / Exception / Assert をまとめて扱う
                "warning" => LogType.Warning,
                "log" => LogType.Log,
                "exception" => LogType.Exception,
                "assert" => LogType.Assert,
                _ => null
            };

            DateTime? sinceUtc = null;
            if (!string.IsNullOrEmpty(sinceRaw)
                && DateTime.TryParse(sinceRaw, null, DateTimeStyles.RoundtripKind, out var parsedSince))
            {
                sinceUtc = parsedSince.ToUniversalTime();
            }

            if (sinceLastClear && console != null)
            {
                var clearedAt = console.LastClearedAtUtc;
                if (!sinceUtc.HasValue || clearedAt > sinceUtc.Value)
                    sinceUtc = clearedAt;
            }

            var capturedEntries = console?.GetEntries(
                    logType,
                    normalizedTypeFilter == "error" ? 5000 : limit,
                    sinceUtc)
                ?? new List<UnitapConsoleCapture.LogEntry>();

            if (normalizedTypeFilter == "error")
            {
                capturedEntries = capturedEntries
                    .Where(e => e.Type == LogType.Error || e.Type == LogType.Exception || e.Type == LogType.Assert)
                    .ToList();
                if (capturedEntries.Count > limit)
                {
                    capturedEntries = capturedEntries
                        .Skip(capturedEntries.Count - limit)
                        .ToList();
                }
            }

            var result = capturedEntries
                .Select(e => new
                {
                    message = e.Message,
                    stackTrace = e.StackTrace,
                    type = e.Type.ToString(),
                    timestamp = e.Timestamp.ToString("O")
                })
                .ToList<object>();

            // CompilationPipeline 経由のコンパイルエラー/警告を補完
            // Application.logMessageReceivedThreaded ではコンパイルエラーが通知されないため
            var compileEntries = UnitapCompileErrorCapture.GetEntries();
            foreach (var ce in compileEntries)
            {
                bool isError = ce.Level == "error";
                bool isWarning = ce.Level == "warning";

                if (normalizedTypeFilter == "error" && !isError) continue;
                if (normalizedTypeFilter == "warning" && !isWarning) continue;
                if (normalizedTypeFilter == "log"
                    || normalizedTypeFilter == "exception"
                    || normalizedTypeFilter == "assert")
                    continue; // これらのフィルタ時はコンパイルエラーを含めない

                DateTime? entryTimestamp = null;
                if (!string.IsNullOrEmpty(ce.Timestamp)
                    && DateTime.TryParse(ce.Timestamp, null, DateTimeStyles.RoundtripKind, out var parsedTs))
                {
                    entryTimestamp = parsedTs.ToUniversalTime();
                }
                if (sinceUtc.HasValue)
                {
                    if (!entryTimestamp.HasValue) continue;
                    if (entryTimestamp.Value < sinceUtc.Value) continue;
                }

                result.Add(new
                {
                    message = ce.Message,
                    stackTrace = "",
                    type = isError ? "Error" : "Warning",
                    timestamp = entryTimestamp?.ToString("O") ?? ""
                });
            }

            return new
            {
                entries = result,
                count = result.Count,
                since = sinceUtc?.ToString("O"),
                sinceLastClearApplied = sinceLastClear
            };
        }
    }
}
