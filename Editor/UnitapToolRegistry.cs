using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using MCPForUnity.Editor.Resources;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;

namespace Unitap
{
    /// <summary>Document JObject parameters next to the handler without dummy fields.</summary>
    [AttributeUsage(AttributeTargets.Class, AllowMultiple = true)]
    public sealed class UnitapToolParameterAttribute : Attribute
    {
        public string Name { get; }
        public string Type { get; }
        public string Description { get; }
        public bool Required { get; set; }
        public string DefaultValue { get; set; }

        public UnitapToolParameterAttribute(string name, string type, string description)
        {
            Name = name;
            Type = type;
            Description = description;
        }
    }

    /// <summary>One discovery path for listing and execution, refreshed by Unity TypeCache.</summary>
    internal static class UnitapToolRegistry
    {
        internal sealed class Entry
        {
            public string Name;
            public Type Type;
            public McpForUnityToolAttribute Attribute;
            public MethodInfo Method;

            public object Describe() => new
            {
                name = Name,
                description = Attribute.Description ?? "",
                className = Type.FullName,
                requiresPolling = Attribute.RequiresPolling,
                pollAction = Attribute.RequiresPolling ? Attribute.PollAction : null,
                autoRegister = Attribute.AutoRegister,
                callable = Method != null,
                parameters = Parameters(Type)
            };
        }

        internal static List<Entry> DiscoverTools() => TypeCache.GetTypesWithAttribute<McpForUnityToolAttribute>()
            .Select(type =>
            {
                var attr = type.GetCustomAttribute<McpForUnityToolAttribute>();
                return new Entry
                {
                    Name = attr.Name ?? ToSnakeCase(type.Name),
                    Type = type,
                    Attribute = attr,
                    Method = type.GetMethod("HandleCommand", BindingFlags.Public | BindingFlags.Static,
                        null, new[] { typeof(JObject) }, null)
                };
            })
            .OrderBy(entry => entry.Name, StringComparer.Ordinal)
            .ThenBy(entry => entry.Type.FullName, StringComparer.Ordinal)
            .ToList();

        internal static List<object> DescribeResources() => TypeCache.GetTypesWithAttribute<McpForUnityResourceAttribute>()
            .OrderBy(type => type.FullName, StringComparer.Ordinal)
            .Select(type => (object)new
            {
                name = type.GetCustomAttribute<McpForUnityResourceAttribute>().ResourceName ?? ToSnakeCase(type.Name),
                className = type.FullName
            }).ToList();

        static List<object> Parameters(Type type)
        {
            var result = new List<object>();
            foreach (var attr in type.GetCustomAttributes<UnitapToolParameterAttribute>())
                result.Add(new { name = attr.Name, type = attr.Type, description = attr.Description,
                    required = attr.Required, defaultValue = attr.DefaultValue });
            // Preserve existing extension tools using the MCP compatibility attribute.
            foreach (var member in type.GetMembers(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance))
            {
                var attr = member.GetCustomAttribute<ToolParameterAttribute>();
                if (attr == null) continue;
                var memberType = member is FieldInfo field ? field.FieldType
                    : member is PropertyInfo property ? property.PropertyType : null;
                result.Add(new { name = attr.Name ?? ToSnakeCase(member.Name), type = memberType?.Name ?? "unknown",
                    description = attr.Description ?? "", required = attr.Required, defaultValue = attr.DefaultValue });
            }
            return result;
        }

        static string ToSnakeCase(string name)
        {
            if (string.IsNullOrEmpty(name)) return name;
            var s1 = Regex.Replace(name, "(.)([A-Z][a-z]+)", "$1_$2");
            return Regex.Replace(s1, "([a-z0-9])([A-Z])", "$1_$2").ToLowerInvariant();
        }
    }
}
