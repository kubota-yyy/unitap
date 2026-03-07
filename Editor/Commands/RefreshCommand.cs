using UnityEditor;

namespace Unitap.Commands
{
    public sealed class RefreshCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            AutoReloadExternalSceneChanges.TryReloadActiveSceneIfModifiedExternally("refresh");
            AssetDatabase.Refresh();
            return new { refreshed = true };
        }
    }
}
