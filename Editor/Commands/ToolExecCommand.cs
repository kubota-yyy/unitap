using System;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Tools;
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
                throw new ArgumentException("tool parameter is required");

            var toolParams = request.Params["params"] as JObject ?? new JObject();

            // ツールクラスを検索
            var toolType = AppDomain.CurrentDomain.GetAssemblies()
                .Where(a => !a.IsDynamic)
                .SelectMany(a =>
                {
                    try { return a.GetTypes(); }
                    catch { return Array.Empty<Type>(); }
                })
                .FirstOrDefault(t =>
                {
                    var attr = t.GetCustomAttribute<McpForUnityToolAttribute>();
                    if (attr == null) return false;
                    var name = attr.Name ?? ToSnakeCase(t.Name);
                    return name == toolName;
                });

            if (toolType == null)
                throw new InvalidOperationException($"Tool not found: {toolName}");

            // HandleCommand(JObject) メソッドを取得して実行
            var method = toolType.GetMethod("HandleCommand",
                BindingFlags.Public | BindingFlags.Static,
                null, new[] { typeof(JObject) }, null);

            if (method == null)
                throw new InvalidOperationException($"Tool {toolName} has no HandleCommand(JObject) method");

            return method.Invoke(null, new object[] { toolParams });
        }

        static string ToSnakeCase(string name)
        {
            if (string.IsNullOrEmpty(name)) return name;
            var s1 = Regex.Replace(name, "(.)([A-Z][a-z]+)", "$1_$2");
            var s2 = Regex.Replace(s1, "([a-z0-9])([A-Z])", "$1_$2");
            return s2.ToLower();
        }
    }
}
