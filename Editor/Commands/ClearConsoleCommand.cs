using System;

namespace Unitap.Commands
{
    public sealed class ClearConsoleCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var nativeCleared = UnitapNativeConsole.Clear();
            var clearedAt = UnitapEntry.Console?.Clear() ?? DateTime.UtcNow;
            UnitapCompileErrorCapture.Clear();
            return new { cleared = true, nativeCleared, clearedAt = clearedAt.ToString("O") };
        }
    }
}
