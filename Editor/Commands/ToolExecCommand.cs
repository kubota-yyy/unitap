using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;

namespace Unitap.Commands
{
    /// <summary>
    /// 既存 [McpForUnityTool] カスタムツールの HandleCommand(JObject) を呼び出す。
    /// </summary>
    public sealed class ToolExecCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var toolName = request.Params?["tool"]?.ToString();
            if (string.IsNullOrEmpty(toolName))
                throw new UnitapCommandException("invalid_params", "tool parameter is required");

            var toolParams = request.Params["params"] as JObject ?? new JObject();

            // ツールクラスを検索
            var allToolEntries = UnitapToolRegistry.DiscoverTools();
            var matches = allToolEntries.Where(t => t.Name == toolName).ToList();
            if (matches.Count > 1)
                throw new UnitapCommandException("ambiguous_tool", $"Multiple tools registered as {toolName}",
                    new { classes = matches.Select(entry => entry.Type.FullName).ToArray() });
            var match = matches.FirstOrDefault();

            if (match == null)
                throw BuildToolNotFoundException(toolName, allToolEntries);

            // HandleCommand(JObject) メソッドを取得して実行
            var method = match.Method;

            if (method == null)
                throw new UnitapCommandException("tool_not_found", $"Tool {toolName} has no HandleCommand(JObject) method");

            try
            {
                return method.Invoke(null, new object[] { toolParams });
            }
            catch (TargetInvocationException tie) when (tie.InnerException != null)
            {
                throw new UnitapCommandException("tool_error", $"[{toolName}] {tie.InnerException.Message}");
            }
        }

        static UnitapCommandException BuildToolNotFoundException(string toolName, List<UnitapToolRegistry.Entry> all)
        {
            var available = all.Select(e => e.Name).OrderBy(n => n).ToList();
            var suggestions = SuggestToolNames(toolName, available, max: 5);

            var details = new
            {
                tool = toolName,
                didYouMean = suggestions,
                availableCount = available.Count,
                hint = "Run `unitap.py tool_list` to see all custom tools.",
            };

            var msg = $"Tool not found: {toolName}";
            if (suggestions.Count > 0)
                msg += $". Did you mean: {string.Join(", ", suggestions)}?";

            return new UnitapCommandException("tool_not_found", msg, details);
        }

        static List<string> SuggestToolNames(string query, List<string> candidates, int max)
        {
            if (string.IsNullOrEmpty(query) || candidates == null || candidates.Count == 0)
                return new List<string>();

            var lq = query.ToLowerInvariant();

            var scored = new List<(string Name, int Score)>(candidates.Count);
            foreach (var c in candidates)
            {
                var lc = c.ToLowerInvariant();
                int distance = Levenshtein(lq, lc);
                // boost prefix / contains matches by lowering distance
                if (lc.StartsWith(lq) || lq.StartsWith(lc)) distance -= 2;
                else if (lc.Contains(lq) || lq.Contains(lc)) distance -= 1;
                scored.Add((c, distance));
            }

            // Cap distance: filter out wildly different names
            int threshold = Math.Max(3, query.Length / 2 + 1);
            return scored
                .Where(s => s.Score <= threshold)
                .OrderBy(s => s.Score)
                .ThenBy(s => s.Name, StringComparer.OrdinalIgnoreCase)
                .Take(max)
                .Select(s => s.Name)
                .ToList();
        }

        static int Levenshtein(string a, string b)
        {
            if (string.IsNullOrEmpty(a)) return b?.Length ?? 0;
            if (string.IsNullOrEmpty(b)) return a.Length;

            var prev = new int[b.Length + 1];
            var curr = new int[b.Length + 1];
            for (int j = 0; j <= b.Length; j++) prev[j] = j;

            for (int i = 1; i <= a.Length; i++)
            {
                curr[0] = i;
                for (int j = 1; j <= b.Length; j++)
                {
                    int cost = a[i - 1] == b[j - 1] ? 0 : 1;
                    curr[j] = Math.Min(
                        Math.Min(curr[j - 1] + 1, prev[j] + 1),
                        prev[j - 1] + cost);
                }
                Array.Copy(curr, prev, b.Length + 1);
            }
            return prev[b.Length];
        }

    }
}
