using UnityEditor;

namespace Unitap.Commands
{
    public sealed class StopCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            if (!EditorApplication.isPlaying)
                return new { already = true, message = "Not in play mode" };

            EditorApplication.isPlaying = false;
            return new { stopped = true };
        }
    }
}
