using UnityEditor;
using UnityEngine;
using System;
using System.Collections.Generic;

namespace Unitap
{
    /// <summary>
    /// Unitap のエントリポイント。InitializeOnLoadで自動起動。
    /// Host / Heartbeat / ConsoleCapture / Dispatcher を統合管理する。
    /// </summary>
    [InitializeOnLoad]
    public static class UnitapEntry
    {
        const int HostStartRetryMax = 5;
        const double HostStartRetryIntervalSeconds = 1.0;

        static IUnitapHost _host;
        static UnitapHeartbeat _heartbeat;
        static UnitapConsoleCapture _console;
        static UnitapDispatcher _dispatcher;
        static int _hostStartRetryCount;
        static double _nextHostStartRetryAt;

        public static UnitapConsoleCapture Console => _console;
        public static UnitapTransportInfo TransportInfo => _host?.TransportInfo;
        public static int QueueDepth => _host?.QueueDepth ?? 0;

        static UnitapEntry()
        {
            // ドメインリロード後に再起動
            // delayCall はドメインリロード後に発火しないケースがあるため update を使う
            EditorApplication.update += InitializeOnce;
        }

        static void InitializeOnce()
        {
            EditorApplication.update -= InitializeOnce;
            _hostStartRetryCount = 0;
            Initialize();
        }

        static void RetryInitialize()
        {
            if (EditorApplication.timeSinceStartup < _nextHostStartRetryAt)
                return;

            EditorApplication.update -= RetryInitialize;
            Initialize();
        }

        static void Initialize()
        {
            Shutdown(deleteHeartbeat: false); // 既存インスタンスを破棄（heartbeatファイルは残す）

            _console = new UnitapConsoleCapture();
            _console.Start();

            if (!TryStartHost(out _host, out var hostStartError))
            {
                _hostStartRetryCount++;
                var reason = string.IsNullOrEmpty(hostStartError) ? "unknown" : hostStartError;
                Debug.LogWarning($"[Unitap] Failed to start Unitap host ({_hostStartRetryCount}/{HostStartRetryMax}): {reason}");

                _host = null;
                _console?.Dispose();
                _console = null;

                if (_hostStartRetryCount < HostStartRetryMax)
                {
                    _nextHostStartRetryAt = EditorApplication.timeSinceStartup + HostStartRetryIntervalSeconds;
                    EditorApplication.update -= RetryInitialize;
                    EditorApplication.update += RetryInitialize;
                }
                else
                {
                    Debug.LogError("[Unitap] Failed to start Unitap host after retries");
                }
                return;
            }
            _hostStartRetryCount = 0;

            _heartbeat = new UnitapHeartbeat();
            _heartbeat.Start(_host.TransportInfo);

            _dispatcher = new UnitapDispatcher();
            _dispatcher.Init();

            // コマンド登録
            RegisterCommands();

            // ドメインリロード後の非同期ジョブ復帰
            UnitapAsyncJob.RestoreIfNeeded();

            // イベント登録（重複防止のため先に -= してから +=）
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
            EditorApplication.quitting -= OnQuitting;
            EditorApplication.quitting += OnQuitting;
            AssemblyReloadEvents.beforeAssemblyReload -= OnBeforeAssemblyReload;
            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;

            var transport = _host.TransportInfo;
            var endpoint = transport?.Kind == "tcp"
                ? $"{transport.Host}:{transport.Port}"
                : transport?.Kind == "pipe"
                    ? transport.PipeName
                    : transport?.FileTransportDirectory;
            Debug.Log($"[Unitap] Started via {transport?.Kind ?? "unknown"} ({endpoint})");
        }

        static bool TryStartHost(out IUnitapHost host, out string errorSummary)
        {
            host = null;
            var errors = new List<string>();

            foreach (var candidate in CreateHostCandidates())
            {
                if (candidate.Start())
                {
                    host = candidate;
                    errorSummary = null;
                    return true;
                }

                var candidateError = string.IsNullOrEmpty(candidate.LastStartError) ? "unknown" : candidate.LastStartError;
                errors.Add($"{candidate.GetType().Name}: {candidateError}");
                candidate.Dispose();
            }

            errorSummary = string.Join(" | ", errors);
            return false;
        }

        static IEnumerable<IUnitapHost> CreateHostCandidates()
        {
            foreach (var transportKind in EnumerateTransportKinds())
            {
                switch (transportKind)
                {
                    case "file":
                        yield return new UnitapFileHost();
                        break;
                    case "pipe":
                        yield return new UnitapPipeHost();
                        break;
                    default:
                        yield return new UnitapTcpHost();
                        break;
                }
            }
        }

        static IEnumerable<string> EnumerateTransportKinds()
        {
            var emitted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var rawPreference = Environment.GetEnvironmentVariable("UNITAP_TRANSPORT_PREFERENCE");

            foreach (var transportKind in ParseTransportPreference(rawPreference))
            {
                if (emitted.Add(transportKind))
                    yield return transportKind;
            }

            foreach (var fallbackKind in new[] { "file", "pipe", "tcp" })
            {
                if (emitted.Add(fallbackKind))
                    yield return fallbackKind;
            }
        }

        static IEnumerable<string> ParseTransportPreference(string rawPreference)
        {
            if (string.IsNullOrWhiteSpace(rawPreference))
                yield break;

            foreach (var token in rawPreference.Split(','))
            {
                var transportKind = token?.Trim().ToLowerInvariant();
                switch (transportKind)
                {
                    case "file":
                    case "pipe":
                    case "tcp":
                        yield return transportKind;
                        break;
                }
            }
        }

        static void RegisterCommands()
        {
            _dispatcher.RegisterCommand("status", new Commands.StatusCommand());
            _dispatcher.RegisterCommand("play", new Commands.PlayCommand());
            _dispatcher.RegisterCommand("stop", new Commands.StopCommand());
            _dispatcher.RegisterCommand("execute_menu", new Commands.ExecuteMenuCommand());
            _dispatcher.RegisterCommand("refresh", new Commands.RefreshCommand());
            _dispatcher.RegisterCommand("reimport", new Commands.ReimportCommand());
            _dispatcher.RegisterCommand("wait_idle", new Commands.WaitIdleCommand());
            _dispatcher.RegisterCommand("read_console", new Commands.ReadConsoleCommand());
            _dispatcher.RegisterCommand("clear_console", new Commands.ClearConsoleCommand());
            _dispatcher.RegisterCommand("cancel", new Commands.CancelCommand());
            _dispatcher.RegisterCommand("tool_list", new Commands.ToolListCommand());
            _dispatcher.RegisterCommand("tool_exec", new Commands.ToolExecCommand());
            _dispatcher.RegisterCommand("compile_check", new Commands.CompileCheckCommand());
            _dispatcher.RegisterCommand("save_scene", new Commands.SaveSceneCommand());
            _dispatcher.RegisterCommand("undo", new Commands.UndoCommand());
            _dispatcher.RegisterCommand("redo", new Commands.RedoCommand());
            _dispatcher.RegisterCommand("diagnose", new Commands.DiagnoseCommand());
            _dispatcher.RegisterCommand("capture", new Commands.CaptureCommand());
        }

        static void Tick()
        {
            if (_host == null || !_host.IsRunning) return;
            _heartbeat?.Tick();
            _dispatcher?.ProcessQueue(_host);
        }

        static void OnQuitting() => Shutdown(deleteHeartbeat: true);
        static void OnBeforeAssemblyReload()
        {
            _heartbeat?.WriteReloading();
            Shutdown(deleteHeartbeat: false);
        }

        static void Shutdown(bool deleteHeartbeat)
        {
            EditorApplication.update -= Tick;
            EditorApplication.update -= RetryInitialize;
            _host?.Dispose();
            _console?.Dispose();
            if (deleteHeartbeat)
                _heartbeat?.Dispose(); // ファイル削除はエディタ終了時のみ
            _host = null;
            _heartbeat = null;
            _console = null;
            _dispatcher = null;
        }

        /// <summary>
        /// 現在のエディタ状態を取得 (他クラスからも利用)
        /// </summary>
        public static EditorState GetEditorState() => UnitapDispatcher.GetEditorState();

        /// <summary>
        /// 直近のエディタ状態を取得 (メインスレッド外での参照用)
        /// </summary>
        public static EditorState GetLastKnownEditorState() => UnitapDispatcher.GetLastKnownEditorState();
    }
}
