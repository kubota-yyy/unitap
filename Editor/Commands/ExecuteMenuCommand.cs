using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using UnityEditor;

namespace Unitap.Commands
{
    public sealed class ExecuteMenuCommand : IUnitapCommand
    {
        // MenuItem("A/B %#t") のショートカット指定部分
        static readonly Regex HotkeySuffix = new(@"\s+[%#&_^]\S*$", RegexOptions.Compiled);

        public object Execute(UnitapRequest request)
        {
            var menuPath = request.Params?["menuPath"]?.ToString();
            if (string.IsNullOrEmpty(menuPath))
                throw new System.ArgumentException("menuPath is required");

            var result = EditorApplication.ExecuteMenuItem(menuPath);
            if (!result)
                throw BuildMenuNotFoundException(menuPath);

            return new { executed = true, menuPath };
        }

        static UnitapCommandException BuildMenuNotFoundException(string menuPath)
        {
            var suggestions = ToolExecCommand.SuggestToolNames(menuPath, CollectScriptMenuPaths(), max: 5);
            var details = new
            {
                menuPath,
                didYouMean = suggestions,
                hint = "Menu paths are case-sensitive and must match the [MenuItem] path exactly (without the hotkey suffix).",
            };

            var msg = $"MenuItem not found: {menuPath}";
            if (suggestions.Count > 0)
                msg += $". Did you mean: {string.Join(", ", suggestions)}?";
            return new UnitapCommandException("menu_not_found", msg, details);
        }

        static List<string> CollectScriptMenuPaths()
        {
            var paths = new HashSet<string>();
            foreach (var method in TypeCache.GetMethodsWithAttribute<MenuItem>())
            {
                foreach (var attribute in method.GetCustomAttributes(typeof(MenuItem), false).Cast<MenuItem>())
                {
                    if (attribute.validate || string.IsNullOrEmpty(attribute.menuItem))
                        continue;
                    if (attribute.menuItem.StartsWith("CONTEXT/"))
                        continue;
                    paths.Add(HotkeySuffix.Replace(attribute.menuItem, string.Empty).Trim());
                }
            }
            return paths.ToList();
        }
    }
}
