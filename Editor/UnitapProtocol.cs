using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Unitap
{
    /// <summary>
    /// Unitap プロトコル v1 のリクエスト/レスポンス型
    /// </summary>
    public sealed class UnitapRequest
    {
        [JsonProperty("version")] public int Version { get; set; } = 1;
        [JsonProperty("requestId")] public string RequestId { get; set; }
        [JsonProperty("idempotencyKey")] public string IdempotencyKey { get; set; }
        [JsonProperty("command")] public string Command { get; set; }
        [JsonProperty("params")] public JObject Params { get; set; }
        [JsonProperty("createdAtUtc")] public string CreatedAtUtc { get; set; }
        [JsonProperty("timeoutMs")] public int TimeoutMs { get; set; } = 30000;
        [JsonProperty("retryable")] public bool Retryable { get; set; } = true;
    }

    public sealed class UnitapResponse
    {
        [JsonProperty("version")] public int Version { get; set; } = 1;
        [JsonProperty("requestId")] public string RequestId { get; set; }
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("result")] public object Result { get; set; }
        [JsonProperty("error", NullValueHandling = NullValueHandling.Ignore)]
        public UnitapError Error { get; set; }
        [JsonProperty("editor")] public EditorState Editor { get; set; }
        [JsonProperty("completedAtUtc")] public string CompletedAtUtc { get; set; }
        [JsonProperty("processingTimeMs")] public long ProcessingTimeMs { get; set; }
        [JsonProperty("transportKind", NullValueHandling = NullValueHandling.Ignore)]
        public string TransportKind { get; set; }
        [JsonProperty("warnings", NullValueHandling = NullValueHandling.Ignore)]
        public string[] Warnings { get; set; }
    }

    public sealed class UnitapError
    {
        [JsonProperty("code")] public string Code { get; set; }
        [JsonProperty("message")] public string Message { get; set; }
        [JsonProperty("details", NullValueHandling = NullValueHandling.Ignore)]
        public object Details { get; set; }
    }

    public sealed class EditorState
    {
        [JsonProperty("isPlaying")] public bool IsPlaying { get; set; }
        [JsonProperty("isCompiling")] public bool IsCompiling { get; set; }
        [JsonProperty("isUpdating")] public bool IsUpdating { get; set; }
        [JsonProperty("activeScene")] public string ActiveScene { get; set; }
    }

    public sealed class UnitapTransportInfo
    {
        [JsonProperty("kind")] public string Kind { get; set; }
        [JsonProperty("host", NullValueHandling = NullValueHandling.Ignore)]
        public string Host { get; set; }
        [JsonProperty("port", NullValueHandling = NullValueHandling.Ignore)]
        public int? Port { get; set; }
        [JsonProperty("pipeName", NullValueHandling = NullValueHandling.Ignore)]
        public string PipeName { get; set; }
        [JsonProperty("pipeSocketPath", NullValueHandling = NullValueHandling.Ignore)]
        public string PipeSocketPath { get; set; }
        [JsonProperty("fileTransportDirectory", NullValueHandling = NullValueHandling.Ignore)]
        public string FileTransportDirectory { get; set; }
    }

    public sealed class HeartbeatData
    {
        [JsonProperty("pid")] public int Pid { get; set; }
        [JsonProperty("port", NullValueHandling = NullValueHandling.Ignore)]
        public int? Port { get; set; }
        [JsonProperty("projectPath")] public string ProjectPath { get; set; }
        [JsonProperty("projectName")] public string ProjectName { get; set; }
        [JsonProperty("unityVersion")] public string UnityVersion { get; set; }
        [JsonProperty("lastHeartbeat")] public string LastHeartbeat { get; set; }
        [JsonProperty("isCompiling")] public bool IsCompiling { get; set; }
        [JsonProperty("isPlaying")] public bool IsPlaying { get; set; }
        [JsonProperty("hasErrors")] public bool HasErrors { get; set; }
        [JsonProperty("errorCount")] public int ErrorCount { get; set; }
        [JsonProperty("timeSinceStartup")] public double TimeSinceStartup { get; set; }
        [JsonProperty("transportKind")] public string TransportKind { get; set; }
        [JsonProperty("host", NullValueHandling = NullValueHandling.Ignore)]
        public string Host { get; set; }
        [JsonProperty("pipeName", NullValueHandling = NullValueHandling.Ignore)]
        public string PipeName { get; set; }
        [JsonProperty("pipeSocketPath", NullValueHandling = NullValueHandling.Ignore)]
        public string PipeSocketPath { get; set; }
        [JsonProperty("fileTransportDirectory", NullValueHandling = NullValueHandling.Ignore)]
        public string FileTransportDirectory { get; set; }
    }
}
