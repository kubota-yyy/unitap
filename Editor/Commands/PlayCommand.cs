using UnityEditor;

namespace Unitap.Commands
{
    public sealed class PlayCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            if (EditorApplication.isPlaying)
                return new { already = true, message = "Already in play mode" };

            if (EditorApplication.isCompiling)
                return new { error = true, message = "Cannot enter play mode while compiling" };

            EditorApplication.isPlaying = true;
            return new { started = true };
        }
    }
}
