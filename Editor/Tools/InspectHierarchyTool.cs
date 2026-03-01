using System;
using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Unitap.Tools
{
    [McpForUnityTool("inspect_hierarchy")]
    public static class InspectHierarchyTool
    {
        public static object HandleCommand(JObject @params)
        {
            var rootPath = @params["root"]?.ToString();
            var depth = @params["depth"]?.ToObject<int>() ?? 3;
            var includeComponents = @params["includeComponents"]?.ToObject<bool>() ?? false;
            var limit = @params["limit"]?.ToObject<int>() ?? 50;

            var nodeCount = 0;

            if (!string.IsNullOrEmpty(rootPath))
            {
                // 指定されたルートから走査
                var rootGo = GameObject.Find(rootPath) ?? FindInactiveGameObject(rootPath);
                if (rootGo == null)
                    return new ErrorResponse($"GameObject not found: {rootPath}");

                var tree = BuildNode(rootGo.transform, rootPath, depth, includeComponents, ref nodeCount, limit);
                return new SuccessResponse("Hierarchy inspected", new { root = tree, nodeCount });
            }

            // シーン全体
            var roots = new List<object>();
            var rootObjects = SceneManager.GetActiveScene().GetRootGameObjects();
            foreach (var go in rootObjects)
            {
                if (nodeCount >= limit) break;
                roots.Add(BuildNode(go.transform, "/" + go.name, depth, includeComponents, ref nodeCount, limit));
            }

            // DontDestroyOnLoad (Playモード時)
            if (Application.isPlaying)
            {
                var ddolObjects = GetDontDestroyOnLoadObjects();
                if (ddolObjects.Length > 0)
                {
                    foreach (var go in ddolObjects)
                    {
                        if (nodeCount >= limit) break;
                        roots.Add(BuildNode(go.transform, "[DontDestroyOnLoad]/" + go.name, depth, includeComponents, ref nodeCount, limit));
                    }
                }
            }

            return new SuccessResponse("Hierarchy inspected", new
            {
                sceneName = SceneManager.GetActiveScene().name,
                scenePath = SceneManager.GetActiveScene().path,
                roots,
                nodeCount
            });
        }

        static object BuildNode(Transform t, string path, int depth, bool includeComponents, ref int nodeCount, int limit)
        {
            if (nodeCount >= limit)
                return new { name = t.name, path, truncated = true };

            nodeCount++;

            var node = new Dictionary<string, object>
            {
                ["name"] = t.name,
                ["path"] = path,
                ["active"] = t.gameObject.activeSelf,
                ["childCount"] = t.childCount
            };

            if (includeComponents)
            {
                var components = t.GetComponents<Component>();
                node["components"] = components
                    .Where(c => c != null)
                    .Select(c => c.GetType().Name)
                    .ToList();
            }

            if (depth > 0 && t.childCount > 0)
            {
                var children = new List<object>();
                for (int i = 0; i < t.childCount; i++)
                {
                    if (nodeCount >= limit) break;
                    var child = t.GetChild(i);
                    children.Add(BuildNode(child, path + "/" + child.name, depth - 1, includeComponents, ref nodeCount, limit));
                }
                node["children"] = children;
            }

            return node;
        }

        static GameObject FindInactiveGameObject(string path)
        {
            var parts = path.TrimStart('/').Split('/');
            var rootObjects = SceneManager.GetActiveScene().GetRootGameObjects();
            foreach (var root in rootObjects)
            {
                if (root.name == parts[0])
                {
                    if (parts.Length == 1) return root;
                    var current = root.transform;
                    for (int i = 1; i < parts.Length; i++)
                    {
                        current = current.Find(parts[i]);
                        if (current == null) break;
                    }
                    if (current != null) return current.gameObject;
                }
            }
            return null;
        }

        static GameObject[] GetDontDestroyOnLoadObjects()
        {
            if (!Application.isPlaying) return Array.Empty<GameObject>();
            try
            {
                var temp = new GameObject("_temp_hierarchy_finder");
                UnityEngine.Object.DontDestroyOnLoad(temp);
                var scene = temp.scene;
                UnityEngine.Object.DestroyImmediate(temp);
                return scene.GetRootGameObjects();
            }
            catch
            {
                return Array.Empty<GameObject>();
            }
        }
    }
}
