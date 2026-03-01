namespace Unitap.Commands
{
    /// <summary>
    /// Unitap コマンドのインターフェース。
    /// メインスレッドで Execute が呼ばれる。
    /// </summary>
    public interface IUnitapCommand
    {
        object Execute(UnitapRequest request);
    }
}
