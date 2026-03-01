using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Tools;

namespace Unitap.Commands
{
    /// <summary>
    /// [McpForUnityTool] 属性付きクラスを列挙する。
    /// </summary>
    public sealed class ToolListCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var tools = new List<object>();
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
                tools.Add(new
                {
                    name = toolName,
                    description = attr.Description ?? "",
                    className = type.FullName
                });
            }

            return new { tools = tools.OrderBy(t => ((dynamic)t).name).ToList(), count = tools.Count };
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
