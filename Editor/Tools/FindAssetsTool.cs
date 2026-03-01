using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;

namespace Unitap.Tools
{
    [McpForUnityTool("find_assets")]
    public static class FindAssetsTool
    {
        public static object HandleCommand(JObject @params)
        {
            var query = @params["query"]?.ToString() ?? "";
            var type = @params["type"]?.ToString();
            var path = @params["path"]?.ToString();
            var limit = @params["limit"]?.ToObject<int>() ?? 20;

            var filter = query;
            if (!string.IsNullOrEmpty(type))
                filter = $"{query} t:{type}".Trim();

            string[] searchFolders = null;
            if (!string.IsNullOrEmpty(path))
                searchFolders = new[] { path };

            var guids = searchFolders != null
                ? AssetDatabase.FindAssets(filter, searchFolders)
                : AssetDatabase.FindAssets(filter);

            var results = guids
                .Take(limit)
                .Select(guid =>
                {
                    var assetPath = AssetDatabase.GUIDToAssetPath(guid);
                    var assetType = AssetDatabase.GetMainAssetTypeAtPath(assetPath);
                    return new
                    {
                        path = assetPath,
                        guid,
                        type = assetType?.Name ?? "Unknown",
                        name = System.IO.Path.GetFileNameWithoutExtension(assetPath)
                    };
                })
                .ToList();

            return new SuccessResponse($"Found {results.Count} assets (total: {guids.Length})", new
            {
                assets = results,
                count = results.Count,
                totalFound = guids.Length
            });
        }
    }
}
