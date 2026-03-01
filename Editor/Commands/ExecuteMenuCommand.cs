using UnityEditor;

namespace Unitap.Commands
{
    public sealed class ExecuteMenuCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var menuPath = request.Params?["menuPath"]?.ToString();
            if (string.IsNullOrEmpty(menuPath))
                throw new System.ArgumentException("menuPath is required");

            var result = EditorApplication.ExecuteMenuItem(menuPath);
            if (!result)
                throw new System.InvalidOperationException($"MenuItem not found: {menuPath}");

            return new { executed = true, menuPath };
        }
    }
}
