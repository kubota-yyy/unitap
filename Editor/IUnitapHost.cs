using System;

namespace Unitap
{
    public struct UnitapPendingRequest
    {
        public UnitapRequest Request;
        public Action<UnitapResponse> Respond;
    }

    public interface IUnitapHost : IDisposable
    {
        bool IsRunning { get; }
        bool TryDequeue(out UnitapPendingRequest request);
    }
}
