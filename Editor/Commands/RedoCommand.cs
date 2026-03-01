using UnityEditor;

namespace Unitap.Commands
{
    public sealed class RedoCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            Undo.PerformRedo();
            return new { redone = true };
        }
    }
}
