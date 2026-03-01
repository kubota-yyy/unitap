using UnityEditor;

namespace Unitap.Commands
{
    public sealed class UndoCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            Undo.PerformUndo();
            return new { undone = true };
        }
    }
}
