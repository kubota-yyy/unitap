using System;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json.Linq;
using UnityEditor;

namespace Unitap.Commands
{
    /// <summary>
    /// 指定 path のみを reimport する。Assets/Reimport All のような全体走査ではないので軽い。
    ///
    /// params:
    ///   paths: string | string[]   - Assets/ からの相対 path or 絶対 path。フォルダ可。
    ///   recursive: bool (default true) - フォルダの場合に下位アセットも含める
    ///
    /// 各 path に対して AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceUpdate[ | ImportRecursive])
    /// を呼ぶ。Assets/ 配下の path に正規化する。
    /// </summary>
    public sealed class ReimportCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var paths = ParsePaths(request.Params);
            if (paths.Count == 0)
                throw new ArgumentException("paths is required (string or string[])");

            bool recursive = request.Params?["recursive"]?.ToObject<bool?>() ?? true;
            var options = ImportAssetOptions.ForceUpdate;
            if (recursive) options |= ImportAssetOptions.ImportRecursive;

            var imported = new List<string>();
            var skipped = new List<object>();

            try
            {
                AssetDatabase.StartAssetEditing();
                foreach (var raw in paths)
                {
                    if (string.IsNullOrEmpty(raw))
                    {
                        skipped.Add(new { path = raw, reason = "empty" });
                        continue;
                    }

                    string assetPath = NormalizeToAssetPath(raw);
                    if (assetPath == null)
                    {
                        skipped.Add(new { path = raw, reason = "outside_project" });
                        continue;
                    }

                    string absPath = Path.GetFullPath(assetPath);
                    if (!File.Exists(absPath) && !Directory.Exists(absPath))
                    {
                        skipped.Add(new { path = assetPath, reason = "not_found" });
                        continue;
                    }

                    AssetDatabase.ImportAsset(assetPath, options);
                    imported.Add(assetPath);
                }
            }
            finally
            {
                AssetDatabase.StopAssetEditing();
            }

            AssetDatabase.Refresh();
            return new
            {
                reimported = true,
                count = imported.Count,
                paths = imported,
                skipped = skipped.ToArray(),
                recursive,
            };
        }

        private static List<string> ParsePaths(JObject parameters)
        {
            var list = new List<string>();
            if (parameters == null) return list;

            var token = parameters["paths"] ?? parameters["path"];
            if (token == null) return list;

            if (token.Type == JTokenType.Array)
            {
                foreach (var t in token)
                {
                    var s = t?.ToString();
                    if (!string.IsNullOrEmpty(s)) list.Add(s);
                }
            }
            else
            {
                var s = token.ToString();
                if (!string.IsNullOrEmpty(s)) list.Add(s);
            }

            return list;
        }

        private static string NormalizeToAssetPath(string raw)
        {
            string p = raw.Replace('\\', '/').TrimEnd('/');
            string projectRoot = Path.GetFullPath(Directory.GetCurrentDirectory()).Replace('\\', '/');

            // Already a project-relative path (Assets/... or Packages/...)
            if (p.StartsWith("Assets/", StringComparison.Ordinal)
                || p.Equals("Assets", StringComparison.Ordinal)
                || p.StartsWith("Packages/", StringComparison.Ordinal))
            {
                return p;
            }

            // Absolute path under the project root
            if (Path.IsPathRooted(p))
            {
                string full = Path.GetFullPath(p).Replace('\\', '/');
                if (full.StartsWith(projectRoot + "/", StringComparison.Ordinal))
                {
                    return full.Substring(projectRoot.Length + 1);
                }
                return null;
            }

            // Relative to project root (e.g. "00_app/...")
            return "Assets/" + p;
        }
    }
}
