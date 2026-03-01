using System;
using System.Reflection;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.UI;

namespace Unitap.Tools
{
    /// <summary>
    /// Inspector操作を自動化するMCPカスタムツール
    ///
    /// type="method": [Button]属性メソッドやpublicメソッドをリフレクションで呼び出し
    /// type="menu": MenuItem実行
    /// type="click": uGUI ButtonのonClickを発火
    /// </summary>
    [McpForUnityTool("invoke_inspector_action")]
    public static class InvokeInspectorAction
    {
        public static object HandleCommand(JObject @params)
        {
            var type = @params["type"]?.ToString();

            if (string.IsNullOrEmpty(type))
            {
                return new ErrorResponse("type パラメータが必要です (method, menu, click)");
            }

            return type switch
            {
                "method" => InvokeMethod(@params),
                "menu" => InvokeMenu(@params),
                "click" => InvokeClick(@params),
                _ => new ErrorResponse($"不明なtype: {type}. 有効な値: method, menu, click")
            };
        }

        /// <summary>
        /// コンポーネントのメソッドをリフレクションで呼び出す
        /// </summary>
        private static object InvokeMethod(JObject @params)
        {
            var gameObjectName = @params["gameObject"]?.ToString();
            var componentName = @params["component"]?.ToString();
            var methodName = @params["methodName"]?.ToString();
            var argsToken = @params["args"];

            if (string.IsNullOrEmpty(gameObjectName))
                return new ErrorResponse("gameObject パラメータが必要です");
            if (string.IsNullOrEmpty(componentName))
                return new ErrorResponse("component パラメータが必要です");
            if (string.IsNullOrEmpty(methodName))
                return new ErrorResponse("methodName パラメータが必要です");

            // GameObjectを検索
            var go = GameObject.Find(gameObjectName);
            if (go == null)
            {
                // 非アクティブオブジェクトも検索
                go = FindInactiveGameObject(gameObjectName);
                if (go == null)
                    return new ErrorResponse($"GameObjectが見つかりません: {gameObjectName}");
            }

            // コンポーネントを取得
            var component = go.GetComponent(componentName);
            if (component == null)
                return new ErrorResponse($"コンポーネントが見つかりません: {componentName} on {gameObjectName}");

            // 引数を解析
            object[] methodArgs = null;
            Type[] argTypes = null;

            if (argsToken != null && argsToken.Type == JTokenType.Array)
            {
                var argsArray = (JArray)argsToken;
                methodArgs = new object[argsArray.Count];
                argTypes = new Type[argsArray.Count];

                for (int i = 0; i < argsArray.Count; i++)
                {
                    var arg = argsArray[i];
                    switch (arg.Type)
                    {
                        case JTokenType.Integer:
                            methodArgs[i] = arg.ToObject<int>();
                            argTypes[i] = typeof(int);
                            break;
                        case JTokenType.Float:
                            methodArgs[i] = arg.ToObject<float>();
                            argTypes[i] = typeof(float);
                            break;
                        case JTokenType.Boolean:
                            methodArgs[i] = arg.ToObject<bool>();
                            argTypes[i] = typeof(bool);
                            break;
                        case JTokenType.String:
                            methodArgs[i] = arg.ToString();
                            argTypes[i] = typeof(string);
                            break;
                        default:
                            methodArgs[i] = arg.ToString();
                            argTypes[i] = typeof(string);
                            break;
                    }
                }
            }

            // メソッドを検索
            MethodInfo method;
            if (argTypes != null && argTypes.Length > 0)
            {
                method = component.GetType().GetMethod(methodName,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance,
                    null, argTypes, null);
            }
            else
            {
                method = component.GetType().GetMethod(methodName,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            }

            if (method == null)
                return new ErrorResponse($"メソッドが見つかりません: {methodName} on {componentName}");

            try
            {
                method.Invoke(component, methodArgs);
                string argsStr = methodArgs != null ? string.Join(", ", methodArgs) : "";
                return new SuccessResponse($"メソッドを実行しました: {gameObjectName}/{componentName}.{methodName}({argsStr})");
            }
            catch (Exception ex)
            {
                return new ErrorResponse($"メソッド実行エラー: {ex.InnerException?.Message ?? ex.Message}");
            }
        }

        /// <summary>
        /// MenuItemを実行
        /// </summary>
        private static object InvokeMenu(JObject @params)
        {
            var menuPath = @params["menuPath"]?.ToString();

            if (string.IsNullOrEmpty(menuPath))
                return new ErrorResponse("menuPath パラメータが必要です");

            if (!EditorApplication.ExecuteMenuItem(menuPath))
                return new ErrorResponse($"MenuItemが見つかりません: {menuPath}");

            return new SuccessResponse($"MenuItemを実行しました: {menuPath}");
        }

        /// <summary>
        /// uGUI ButtonのonClickを発火
        /// </summary>
        private static object InvokeClick(JObject @params)
        {
            var uiPath = @params["uiPath"]?.ToString();

            if (string.IsNullOrEmpty(uiPath))
                return new ErrorResponse("uiPath パラメータが必要です");

            // GameObjectを検索
            var go = GameObject.Find(uiPath);
            if (go == null)
            {
                go = FindInactiveGameObject(uiPath);
                if (go == null)
                    return new ErrorResponse($"GameObjectが見つかりません: {uiPath}");
            }

            // Buttonコンポーネントを取得
            var button = go.GetComponent<Button>();
            if (button == null)
                return new ErrorResponse($"Buttonコンポーネントが見つかりません: {uiPath}");

            // onClickを発火
            button.onClick.Invoke();
            return new SuccessResponse($"Buttonをクリックしました: {uiPath}");
        }

        /// <summary>
        /// 非アクティブなGameObjectも含めて検索
        /// </summary>
        private static GameObject FindInactiveGameObject(string path)
        {
            // パスを分解
            var parts = path.Split('/');

            // ルートオブジェクトを検索
            var rootObjects = UnityEngine.SceneManagement.SceneManager.GetActiveScene().GetRootGameObjects();
            foreach (var root in rootObjects)
            {
                if (root.name == parts[0])
                {
                    if (parts.Length == 1) return root;

                    // 子を辿る
                    var current = root.transform;
                    for (int i = 1; i < parts.Length; i++)
                    {
                        current = current.Find(parts[i]);
                        if (current == null) break;
                    }
                    if (current != null) return current.gameObject;
                }
            }

            // DontDestroyOnLoadも検索
            var dontDestroyOnLoadObjects = GetDontDestroyOnLoadObjects();
            foreach (var root in dontDestroyOnLoadObjects)
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

        /// <summary>
        /// DontDestroyOnLoadシーンのオブジェクトを取得
        /// </summary>
        private static GameObject[] GetDontDestroyOnLoadObjects()
        {
            // DontDestroyOnLoadシーンは直接アクセスできないため、既知のオブジェクトから取得を試みる
            // Playモード時のみ有効
            if (!Application.isPlaying) return Array.Empty<GameObject>();

            try
            {
                // 一時的なオブジェクトを作成してDontDestroyOnLoadに移動し、そのシーンを取得
                var temp = new GameObject("_temp_finder");
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
