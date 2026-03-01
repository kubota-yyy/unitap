using System.Collections.Generic;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Unitap.Tools
{
    [McpForUnityTool("set_component")]
    public static class SetComponentTool
    {
        public static object HandleCommand(JObject @params)
        {
            var gameObjectPath = @params["gameObject"]?.ToString();
            if (string.IsNullOrEmpty(gameObjectPath))
                return new ErrorResponse("gameObject is required");

            var componentName = @params["component"]?.ToString();
            if (string.IsNullOrEmpty(componentName))
                return new ErrorResponse("component is required");

            var values = @params["values"] as JObject;
            if (values == null || !values.HasValues)
                return new ErrorResponse("values is required");

            var save = @params["save"]?.ToObject<bool>() ?? true;

            var go = GameObject.Find(gameObjectPath) ?? FindInactiveGameObject(gameObjectPath);
            if (go == null)
                return new ErrorResponse($"GameObject not found: {gameObjectPath}");

            var component = go.GetComponent(componentName);
            if (component == null)
                return new ErrorResponse($"Component not found: {componentName} on {gameObjectPath}");

            Undo.RecordObject(component, $"Set {componentName} values");

            var so = new SerializedObject(component);
            var setFields = new List<string>();

            foreach (var kv in values)
            {
                var prop = so.FindProperty(kv.Key);
                if (prop == null)
                    continue;

                if (SetProperty(prop, kv.Value))
                    setFields.Add(kv.Key);
            }

            so.ApplyModifiedProperties();

            if (save)
            {
                EditorUtility.SetDirty(component);
                if (!EditorApplication.isPlaying)
                    AssetDatabase.SaveAssets();
            }

            return new SuccessResponse($"Set {setFields.Count} fields on {componentName}", new
            {
                gameObject = gameObjectPath,
                component = componentName,
                setFields,
                saved = save
            });
        }

        static bool SetProperty(SerializedProperty prop, JToken value)
        {
            switch (prop.propertyType)
            {
                case SerializedPropertyType.Integer:
                    prop.intValue = value.ToObject<int>();
                    return true;
                case SerializedPropertyType.Boolean:
                    prop.boolValue = value.ToObject<bool>();
                    return true;
                case SerializedPropertyType.Float:
                    prop.floatValue = value.ToObject<float>();
                    return true;
                case SerializedPropertyType.String:
                    prop.stringValue = value.ToString();
                    return true;
                case SerializedPropertyType.Color:
                    if (value is JObject colorObj)
                    {
                        prop.colorValue = new Color(
                            colorObj["r"]?.ToObject<float>() ?? 0,
                            colorObj["g"]?.ToObject<float>() ?? 0,
                            colorObj["b"]?.ToObject<float>() ?? 0,
                            colorObj["a"]?.ToObject<float>() ?? 1
                        );
                        return true;
                    }
                    return false;
                case SerializedPropertyType.Vector2:
                    if (value is JObject v2Obj)
                    {
                        prop.vector2Value = new Vector2(
                            v2Obj["x"]?.ToObject<float>() ?? 0,
                            v2Obj["y"]?.ToObject<float>() ?? 0
                        );
                        return true;
                    }
                    return false;
                case SerializedPropertyType.Vector3:
                    if (value is JObject v3Obj)
                    {
                        prop.vector3Value = new Vector3(
                            v3Obj["x"]?.ToObject<float>() ?? 0,
                            v3Obj["y"]?.ToObject<float>() ?? 0,
                            v3Obj["z"]?.ToObject<float>() ?? 0
                        );
                        return true;
                    }
                    return false;
                case SerializedPropertyType.Vector4:
                    if (value is JObject v4Obj)
                    {
                        prop.vector4Value = new Vector4(
                            v4Obj["x"]?.ToObject<float>() ?? 0,
                            v4Obj["y"]?.ToObject<float>() ?? 0,
                            v4Obj["z"]?.ToObject<float>() ?? 0,
                            v4Obj["w"]?.ToObject<float>() ?? 0
                        );
                        return true;
                    }
                    return false;
                case SerializedPropertyType.Enum:
                    if (value.Type == JTokenType.Integer)
                        prop.enumValueIndex = value.ToObject<int>();
                    else if (value.Type == JTokenType.String)
                    {
                        var idx = System.Array.IndexOf(prop.enumDisplayNames, value.ToString());
                        if (idx >= 0) prop.enumValueIndex = idx;
                    }
                    return true;
                default:
                    return false;
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
