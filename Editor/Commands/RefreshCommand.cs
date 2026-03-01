using UnityEditor;

namespace Unitap.Commands
{
    public sealed class RefreshCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            AssetDatabase.Refresh();
            return new { refreshed = true };
        }
    }
}
