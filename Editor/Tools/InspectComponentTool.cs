using System;
using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Unitap.Tools
{
    [McpForUnityTool("inspect_component")]
    public static class InspectComponentTool
    {
        public static object HandleCommand(JObject @params)
        {
            var gameObjectPath = @params["gameObject"]?.ToString();
            if (string.IsNullOrEmpty(gameObjectPath))
                return new ErrorResponse("gameObject is required");

            var componentName = @params["component"]?.ToString();
            var fieldsToken = @params["fields"] as JArray;
            var arrayLimit = @params["limit"]?.ToObject<int>() ?? 50;

            // GameObjectを検索
            var go = GameObject.Find(gameObjectPath) ?? FindInactiveGameObject(gameObjectPath);
            if (go == null)
                return new ErrorResponse($"GameObject not found: {gameObjectPath}");

            // コンポーネント名が省略 → 全コンポーネントサマリー
            if (string.IsNullOrEmpty(componentName))
            {
                var components = go.GetComponents<Component>();
                var summary = components
                    .Where(c => c != null)
                    .Select(c => new { type = c.GetType().Name, fullType = c.GetType().FullName })
                    .ToList();
                return new SuccessResponse("Components listed", new
                {
                    gameObject = gameObjectPath,
                    components = summary,
                    count = summary.Count
                });
            }

            // コンポーネント取得
            var target = go.GetComponent(componentName);
            if (target == null)
                return new ErrorResponse($"Component not found: {componentName} on {gameObjectPath}");

            // フィールドリスト
            HashSet<string> fieldFilter = null;
            if (fieldsToken != null && fieldsToken.Count > 0)
                fieldFilter = new HashSet<string>(fieldsToken.Select(t => t.ToString()));

            // SerializedObjectで値を読み取り
            var so = new SerializedObject(target);
            var fields = new Dictionary<string, object>();
            var sp = so.GetIterator();
            bool enterChildren = true;

            while (sp.NextVisible(enterChildren))
            {
                enterChildren = false;

                // m_Script などの内部プロパティはスキップ
                if (sp.propertyPath == "m_Script") continue;

                if (fieldFilter != null && !fieldFilter.Contains(sp.name) && !fieldFilter.Contains(sp.propertyPath))
                    continue;

                fields[sp.propertyPath] = ReadSerializedProperty(sp, arrayLimit);
            }

            return new SuccessResponse("Component inspected", new
            {
                gameObject = gameObjectPath,
                component = componentName,
                componentType = target.GetType().FullName,
                fields
            });
        }

        static object ReadSerializedProperty(SerializedProperty prop, int arrayLimit)
        {
            switch (prop.propertyType)
            {
                case SerializedPropertyType.Integer:
                    return prop.intValue;
                case SerializedPropertyType.Boolean:
                    return prop.boolValue;
                case SerializedPropertyType.Float:
                    return prop.floatValue;
                case SerializedPropertyType.String:
                    return prop.stringValue;
                case SerializedPropertyType.Color:
                    var c = prop.colorValue;
                    return new { r = c.r, g = c.g, b = c.b, a = c.a };
                case SerializedPropertyType.Vector2:
                    var v2 = prop.vector2Value;
                    return new { x = v2.x, y = v2.y };
                case SerializedPropertyType.Vector3:
                    var v3 = prop.vector3Value;
                    return new { x = v3.x, y = v3.y, z = v3.z };
                case SerializedPropertyType.Vector4:
                    var v4 = prop.vector4Value;
                    return new { x = v4.x, y = v4.y, z = v4.z, w = v4.w };
                case SerializedPropertyType.Rect:
                    var r = prop.rectValue;
                    return new { x = r.x, y = r.y, width = r.width, height = r.height };
                case SerializedPropertyType.Enum:
                    return prop.enumDisplayNames.Length > prop.enumValueIndex && prop.enumValueIndex >= 0
                        ? prop.enumDisplayNames[prop.enumValueIndex]
                        : prop.enumValueIndex.ToString();
                case SerializedPropertyType.ObjectReference:
                    if (prop.objectReferenceValue != null)
                        return new
                        {
                            instanceId = prop.objectReferenceValue.GetInstanceID(),
                            name = prop.objectReferenceValue.name,
                            type = prop.objectReferenceValue.GetType().Name
                        };
                    return prop.objectReferenceInstanceIDValue != 0
                        ? (object)new { missing = true, instanceId = prop.objectReferenceInstanceIDValue }
                        : null;
                case SerializedPropertyType.LayerMask:
                    return prop.intValue;
                case SerializedPropertyType.Quaternion:
                    var q = prop.quaternionValue;
                    return new { x = q.x, y = q.y, z = q.z, w = q.w };
                case SerializedPropertyType.ArraySize:
                    return prop.intValue;
                default:
                    return $"<{prop.propertyType}>";
            }
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
    }
}
