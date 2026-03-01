using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Resources;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;

namespace Unitap.Tools
{
    /// <summary>
    /// MCP経由でカスタムツール/リソース一覧を取得するツール。
    /// stdio連携ではunity://custom-toolsリソースが動作しないため、
    /// batch_execute経由でこのツールを呼び出して一覧を取得する。
    /// </summary>
    [McpForUnityTool("list_custom_tools", Description = "Lists all registered custom MCP tools and resources")]
    public static class ListCustomTools
    {
        public static object HandleCommand(JObject @params)
        {
            var type = @params["type"]?.ToString() ?? "all";

            var tools = new List<object>();
            var resources = new List<object>();

            if (type == "tools" || type == "all")
            {
                tools = DiscoverTools();
            }

            if (type == "resources" || type == "all")
            {
                resources = DiscoverResources();
            }

            return new
            {
                success = true,
                tools,
                resources,
                toolCount = tools.Count,
                resourceCount = resources.Count
            };
        }

        private static List<object> DiscoverTools()
        {
            var result = new List<object>();

            var allTypes = AppDomain.CurrentDomain.GetAssemblies()
                .Where(a => !a.IsDynamic)
                .SelectMany(a =>
                {
                    try { return a.GetTypes(); }
                    catch { return Array.Empty<Type>(); }
                })
                .Where(t => t.GetCustomAttribute<McpForUnityToolAttribute>() != null);

            foreach (var type in allTypes)
            {
                var attr = type.GetCustomAttribute<McpForUnityToolAttribute>();
                var toolName = attr.Name ?? ToSnakeCase(type.Name);

                result.Add(new
                {
                    name = toolName,
                    description = attr.Description ?? "",
                    className = type.FullName,
                    requiresPolling = attr.RequiresPolling,
                    pollAction = attr.RequiresPolling ? attr.PollAction : null,
                    autoRegister = attr.AutoRegister
                });
            }

            return result.OrderBy(t => ((dynamic)t).name).ToList();
        }

        private static List<object> DiscoverResources()
        {
            var result = new List<object>();

            var allTypes = AppDomain.CurrentDomain.GetAssemblies()
                .Where(a => !a.IsDynamic)
                .SelectMany(a =>
                {
                    try { return a.GetTypes(); }
                    catch { return Array.Empty<Type>(); }
                })
                .Where(t => t.GetCustomAttribute<McpForUnityResourceAttribute>() != null);

            foreach (var type in allTypes)
            {
                var attr = type.GetCustomAttribute<McpForUnityResourceAttribute>();
                var resourceName = attr.ResourceName ?? ToSnakeCase(type.Name);

                result.Add(new
                {
                    name = resourceName,
                    className = type.FullName
                });
            }

            return result.OrderBy(r => ((dynamic)r).name).ToList();
        }

        private static string ToSnakeCase(string name)
        {
            if (string.IsNullOrEmpty(name)) return name;

            var s1 = Regex.Replace(name, "(.)([A-Z][a-z]+)", "$1_$2");
            var s2 = Regex.Replace(s1, "([a-z0-9])([A-Z])", "$1_$2");
            return s2.ToLower();
        }
    }
}
