namespace Unitap.Commands
{
    /// <summary>
    /// 協調キャンセルのみ。
    /// 現状の HandleCommand(JObject) ベースのツールは cancel 未対応のため cancel_unsupported を返す。
    /// </summary>
    public sealed class CancelCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            return new { status = "cancel_unsupported", message = "Cancel is not supported for the current operation" };
        }
    }
}
