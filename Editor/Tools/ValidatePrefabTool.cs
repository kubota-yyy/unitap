using System.Collections.Generic;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Unitap.Tools
{
    [McpForUnityTool("validate_prefab")]
    public static class ValidatePrefabTool
    {
        public static object HandleCommand(JObject @params)
        {
            var assetPath = @params["assetPath"]?.ToString();
            if (string.IsNullOrEmpty(assetPath))
                return new ErrorResponse("assetPath is required");

            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (prefab == null)
                return new ErrorResponse($"Prefab not found: {assetPath}");

            var missingScripts = new List<object>();
            var missingReferences = new List<object>();
            int componentCount = 0;

            var allTransforms = prefab.GetComponentsInChildren<Transform>(true);
            foreach (var t in allTransforms)
            {
                var go = t.gameObject;
                var goPath = GetRelativePath(prefab.transform, t);
                var components = go.GetComponents<Component>();

                for (int i = 0; i < components.Length; i++)
                {
                    if (components[i] == null)
                    {
                        missingScripts.Add(new { gameObject = goPath, index = i });
                        continue;
                    }

                    componentCount++;

                    var so = new SerializedObject(components[i]);
                    var sp = so.GetIterator();
                    bool enterChildren = true;
                    while (sp.NextVisible(enterChildren))
                    {
                        enterChildren = false;
                        if (sp.propertyType == SerializedPropertyType.ObjectReference)
                        {
                            if (sp.objectReferenceValue == null && sp.objectReferenceInstanceIDValue != 0)
                            {
                                missingReferences.Add(new
                                {
                                    gameObject = goPath,
                                    component = components[i].GetType().Name,
                                    property = sp.propertyPath
                                });
                            }
                        }
                    }
                }
            }

            return new SuccessResponse("Validation complete", new
            {
                valid = missingScripts.Count == 0 && missingReferences.Count == 0,
                assetPath,
                rootName = prefab.name,
                componentCount,
                missingScripts,
                missingScriptCount = missingScripts.Count,
                missingReferences,
                missingReferenceCount = missingReferences.Count
            });
        }

        static string GetRelativePath(Transform root, Transform target)
        {
            if (target == root) return root.name;
            var path = target.name;
            var current = target.parent;
            while (current != null && current != root)
            {
                path = current.name + "/" + path;
                current = current.parent;
            }
            return root.name + "/" + path;
        }
    }
}
