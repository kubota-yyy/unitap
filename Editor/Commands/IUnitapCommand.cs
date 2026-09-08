using System;

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

    /// <summary>
    /// 構造化エラーコード付きのコマンド例外。
    /// Dispatcher がこの例外をキャッチし、Code/Details をそのままレスポンスに使う。
    /// </summary>
    public sealed class UnitapCommandException : Exception
    {
        public string Code { get; }
        public object Details { get; }

        public UnitapCommandException(string code, string message) : base(message)
        {
            Code = code;
            Details = null;
        }

        public UnitapCommandException(string code, string message, object details) : base(message)
        {
            Code = code;
            Details = details;
        }
    }
}
