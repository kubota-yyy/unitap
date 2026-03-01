using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditorInternal;
using UnityEngine;

namespace Unitap.Tools
{
    /// <summary>
    /// 任意のEditorWindowをキャプチャするカスタムツール
    /// </summary>
    [McpForUnityTool("capture_editor_window")]
    public static class EditorWindowCaptureTool
    {
        private const string DefaultOutputPath = "/tmp/unity_editor_window.png";

        public static object HandleCommand(JObject @params)
        {
            var outputPath = @params["outputPath"]?.ToString() ?? DefaultOutputPath;
            var windowHint = @params["window"]?.ToString();
            var windowTitle = @params["windowTitle"]?.ToString();
            var windowType = @params["windowType"]?.ToString();
            var menuPath = @params["menuPath"]?.ToString();
            var focus = @params["focus"]?.ToObject<bool>() ?? true;
            var openIfMissing = @params["openIfMissing"]?.ToObject<bool>() ?? true;
            var index = Mathf.Max(0, @params["index"]?.ToObject<int>() ?? 0);

            if (string.IsNullOrEmpty(windowTitle) && string.IsNullOrEmpty(windowType) && !string.IsNullOrEmpty(windowHint))
            {
                windowTitle = windowHint;
                windowType = windowHint;
            }

            var windows = FindTargetWindows(windowTitle, windowType);

            if (windows.Count == 0 && !string.IsNullOrEmpty(menuPath))
            {
                EditorApplication.ExecuteMenuItem(menuPath);
                InternalEditorUtility.RepaintAllViews();
                windows = FindTargetWindows(windowTitle, windowType);
            }

            if (windows.Count == 0 && openIfMissing && !string.IsNullOrEmpty(windowType))
            {
                var type = ResolveEditorWindowType(windowType);
                if (type != null)
                {
                    var opened = EditorWindow.GetWindow(type);
                    opened?.Show();
                    windows = FindTargetWindows(windowTitle, windowType);
                }
            }

            if (windows.Count == 0 && string.IsNullOrEmpty(windowTitle) && string.IsNullOrEmpty(windowType))
            {
                var focusedWindow = EditorWindow.focusedWindow;
                if (focusedWindow != null)
                    windows.Add(focusedWindow);
            }

            if (windows.Count == 0)
            {
                return new ErrorResponse("Target EditorWindow not found. Specify window/windowTitle/windowType (e.g. Inspector or PlayMaker Editor).");
            }

            windows = windows
                .Distinct()
                .OrderBy(w => SafeTitle(w), StringComparer.OrdinalIgnoreCase)
                .ThenBy(w => w.GetType().FullName, StringComparer.OrdinalIgnoreCase)
                .ToList();

            var targetIndex = Mathf.Clamp(index, 0, windows.Count - 1);
            var targetWindow = windows[targetIndex];

            if (focus)
            {
                targetWindow.Show();
                targetWindow.Focus();
                targetWindow.Repaint();
                InternalEditorUtility.RepaintAllViews();
            }

            if (!TryCaptureEditorWindow(targetWindow, outputPath, out var error))
            {
                return new ErrorResponse(error ?? "Failed to capture EditorWindow");
            }

            var matched = windows.Select((window, idx) => BuildWindowInfo(window, idx)).ToList();

            return new SuccessResponse("EditorWindow captured", new
            {
                outputPath,
                capturedWindow = BuildWindowInfo(targetWindow, targetIndex),
                matchedCount = windows.Count,
                matchedWindows = matched
            });
        }

        private static bool TryCaptureEditorWindow(EditorWindow window, string outputPath, out string error)
        {
            error = null;

            try
            {
                var rect = window.position;
                if (rect.width <= 1f || rect.height <= 1f)
                {
                    error = $"Window has invalid size: {rect.width}x{rect.height}";
                    return false;
                }

                var pixelsPerPoint = EditorGUIUtility.pixelsPerPoint > 0f ? EditorGUIUtility.pixelsPerPoint : 1f;
                var x = Mathf.RoundToInt(rect.x * pixelsPerPoint);
                var yTop = Mathf.RoundToInt(rect.y * pixelsPerPoint);
                var width = Mathf.Max(1, Mathf.RoundToInt(rect.width * pixelsPerPoint));
                var height = Mathf.Max(1, Mathf.RoundToInt(rect.height * pixelsPerPoint));

                EnsureOutputDirectory(outputPath);

                // まずは内部APIでEditorWindowを直接キャプチャする（座標依存を避ける）。
                if (TryCaptureByInternalApi(window, width, height, outputPath, out var internalCaptureError))
                    return true;

                // 内部APIが失敗するケース向けに、ReadScreenPixelをフォールバックとして残す。
                if (!TryCaptureByYModes(x, yTop, width, height, outputPath))
                {
                    error = $"Failed to capture EditorWindow. internalCaptureError={internalCaptureError ?? "unknown"}";
                    return false;
                }

                return true;
            }
            catch (Exception ex)
            {
                error = $"Capture failed: {ex.Message}";
                return false;
            }
        }

        private static bool TryCaptureByInternalApi(
            EditorWindow window,
            int width,
            int height,
            string outputPath,
            out string error)
        {
            error = null;
            RenderTexture rt = null;
            var previous = RenderTexture.active;
            Texture2D texture = null;

            try
            {
                rt = RenderTexture.GetTemporary(width, height, 24, RenderTextureFormat.ARGB32);
                var captured = InternalEditorUtility.CaptureEditorWindow(window, rt);
                if (!captured)
                {
                    error = "InternalEditorUtility.CaptureEditorWindow returned false";
                    return false;
                }

                RenderTexture.active = rt;
                texture = new Texture2D(width, height, TextureFormat.RGB24, false);
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                texture.Apply();

                File.WriteAllBytes(outputPath, texture.EncodeToPNG());
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
            finally
            {
                if (texture != null)
                    UnityEngine.Object.DestroyImmediate(texture);

                RenderTexture.active = previous;
                if (rt != null)
                    RenderTexture.ReleaseTemporary(rt);
            }
        }

        private static bool TryCaptureByYModes(int x, int yTop, int width, int height, string outputPath)
        {
            var candidateY = new List<int>();

            var displayHeight = Display.main != null ? Display.main.systemHeight : 0;
            if (displayHeight <= 0)
                displayHeight = Screen.currentResolution.height;

            if (displayHeight > 0)
                candidateY.Add(displayHeight - yTop - height);

            candidateY.Add(yTop);

            foreach (var y in candidateY.Distinct())
            {
                if (TryReadAndSavePng(x, y, width, height, outputPath))
                    return true;
            }

            return false;
        }

        private static bool TryReadAndSavePng(int x, int y, int width, int height, string outputPath)
        {
            // マルチモニター環境では負座標が有効な場合があるため、まず生座標を試す。
            if (TryReadAndWritePng(new Vector2(x, y), width, height, outputPath))
                return true;

            // 生座標で失敗した場合のみ、画面内にクランプした座標を試す。
            var clampedX = Mathf.Max(0, x);
            var clampedY = Mathf.Max(0, y);
            if (clampedX != x || clampedY != y)
                return TryReadAndWritePng(new Vector2(clampedX, clampedY), width, height, outputPath);

            return false;
        }

        private static bool TryReadAndWritePng(Vector2 pixelPos, int width, int height, string outputPath)
        {
            var pixels = InternalEditorUtility.ReadScreenPixel(pixelPos, width, height);
            if (pixels == null || pixels.Length != width * height)
                return false;

            var texture = new Texture2D(width, height, TextureFormat.RGB24, false);
            try
            {
                texture.SetPixels(pixels);
                texture.Apply();
                File.WriteAllBytes(outputPath, texture.EncodeToPNG());
                return true;
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(texture);
            }
        }

        private static void EnsureOutputDirectory(string outputPath)
        {
            var dir = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                Directory.CreateDirectory(dir);
        }

        private static List<EditorWindow> FindTargetWindows(string windowTitle, string windowType)
        {
            var titleNeedle = Normalize(windowTitle);
            var typeNeedle = Normalize(windowType);

            return Resources.FindObjectsOfTypeAll<EditorWindow>()
                .Where(window => window != null)
                .Where(window => string.IsNullOrEmpty(titleNeedle)
                    || SafeTitle(window).IndexOf(titleNeedle, StringComparison.OrdinalIgnoreCase) >= 0)
                .Where(window => string.IsNullOrEmpty(typeNeedle) || MatchType(window.GetType(), typeNeedle))
                .ToList();
        }

        private static bool MatchType(Type type, string typeNeedle)
        {
            if (type == null)
                return false;

            var name = type.Name ?? string.Empty;
            var fullName = type.FullName ?? string.Empty;
            var assemblyQualifiedName = type.AssemblyQualifiedName ?? string.Empty;

            return name.IndexOf(typeNeedle, StringComparison.OrdinalIgnoreCase) >= 0
                || fullName.IndexOf(typeNeedle, StringComparison.OrdinalIgnoreCase) >= 0
                || assemblyQualifiedName.IndexOf(typeNeedle, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static Type ResolveEditorWindowType(string windowType)
        {
            if (string.IsNullOrWhiteSpace(windowType))
                return null;

            var exactType = Type.GetType(windowType, false);
            if (exactType != null && typeof(EditorWindow).IsAssignableFrom(exactType))
                return exactType;

            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try
                {
                    types = assembly.GetTypes();
                }
                catch (ReflectionTypeLoadException ex)
                {
                    types = ex.Types ?? Array.Empty<Type>();
                }
                catch
                {
                    continue;
                }

                foreach (var type in types)
                {
                    if (type == null || !typeof(EditorWindow).IsAssignableFrom(type))
                        continue;

                    if (MatchType(type, windowType))
                        return type;
                }
            }

            return null;
        }

        private static object BuildWindowInfo(EditorWindow window, int index)
        {
            var rect = window.position;
            return new
            {
                index,
                title = SafeTitle(window),
                type = window.GetType().FullName,
                rect = new
                {
                    x = rect.x,
                    y = rect.y,
                    width = rect.width,
                    height = rect.height
                }
            };
        }

        private static string SafeTitle(EditorWindow window)
        {
            return window?.titleContent?.text ?? string.Empty;
        }

        private static string Normalize(string value)
        {
            return string.IsNullOrWhiteSpace(value) ? string.Empty : value.Trim();
        }
    }
}
