using System;

namespace Unitap
{
    public sealed class UnitapPendingRequest
    {
        public UnitapRequest Request;
        public Action<UnitapResponse> Respond;
        public volatile bool Abandoned;
    }

    public interface IUnitapHost : IDisposable
    {
        bool Start();
        bool IsRunning { get; }
        string LastStartError { get; }
        UnitapTransportInfo TransportInfo { get; }
        bool TryDequeue(out UnitapPendingRequest request);
        int QueueDepth { get; }
    }
}
