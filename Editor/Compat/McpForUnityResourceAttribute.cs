using System;

namespace MCPForUnity.Editor.Resources
{
    /// <summary>
    /// Compatibility layer: replaces McpForUnityResourceAttribute from external MCP packages.
    /// </summary>
    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
    public class McpForUnityResourceAttribute : Attribute
    {
        public string ResourceName { get; }
        public string Description { get; set; }

        public McpForUnityResourceAttribute() { ResourceName = null; }
        public McpForUnityResourceAttribute(string resourceName) { ResourceName = resourceName; }
    }
}
