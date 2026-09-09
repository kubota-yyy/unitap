using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;

namespace Unitap.Tools
{
    /// <summary>Expose the same registry used by tool_list and tool_exec.</summary>
    [McpForUnityTool("list_custom_tools", Description = "List registered project tools and resources with parameter schemas")]
    [UnitapToolParameter("type", "string", "all, tools or resources", DefaultValue = "all")]
    public static class ListCustomTools
    {
        public static object HandleCommand(JObject @params)
        {
            var type = @params["type"]?.ToString() ?? "all";
            var tools = type == "tools" || type == "all"
                ? UnitapToolRegistry.DiscoverTools().Select(entry => entry.Describe()).ToList()
                : new List<object>();
            var resources = type == "resources" || type == "all"
                ? UnitapToolRegistry.DescribeResources() : new List<object>();
            return new { success = true, tools, resources, toolCount = tools.Count, resourceCount = resources.Count };
        }
    }
}
