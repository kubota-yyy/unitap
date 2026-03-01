using System;

namespace MCPForUnity.Editor.Tools
{
    /// <summary>
    /// Compatibility layer: replaces McpForUnityToolAttribute from external MCP packages.
    /// 既存カスタムツールのコード変更なしで動作する。
    /// </summary>
    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
    public class McpForUnityToolAttribute : Attribute
    {
        public string Name { get; set; }
        public string Description { get; set; }
        public bool StructuredOutput { get; set; } = true;
        public bool AutoRegister { get; set; } = true;
        public bool RequiresPolling { get; set; } = false;
        public string PollAction { get; set; } = "status";

        public string CommandName
        {
            get => Name;
            set => Name = value;
        }

        public McpForUnityToolAttribute() { Name = null; }
        public McpForUnityToolAttribute(string name = null) { Name = name; }
    }

    [AttributeUsage(AttributeTargets.Property | AttributeTargets.Field, AllowMultiple = false)]
    public class ToolParameterAttribute : Attribute
    {
        public string Name { get; }
        public string Description { get; set; }
        public bool Required { get; set; } = true;
        public string DefaultValue { get; set; }

        public ToolParameterAttribute(string description) { Description = description; }
    }
}
