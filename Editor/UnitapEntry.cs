using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// Unitap のエントリポイント。InitializeOnLoadで自動起動。
    /// TCP Host / Heartbeat / ConsoleCapture / Dispatcher を統合管理する。
    /// </summary>
    [InitializeOnLoad]
    public static class UnitapEntry
    {
        const int HostStartRetryMax = 5;
        const double HostStartRetryIntervalSeconds = 1.0;

        static UnitapTcpHost _tcpHost;
        static UnitapPipeHost _pipeHost;
        static UnitapFileHost _fileHost;
        static UnitapHeartbeat _heartbeat;
        static UnitapConsoleCapture _console;
        static UnitapDispatcher _dispatcher;
        static int _hostStartRetryCount;
        static double _nextHostStartRetryAt;

        public static UnitapConsoleCapture Console => _console;

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
            EnsurePidFile();

            _console = new UnitapConsoleCapture();
            _console.Start();

            _tcpHost = new UnitapTcpHost();
            var tcpStarted = _tcpHost.Start();
            if (!tcpStarted)
            {
                var reason = string.IsNullOrEmpty(_tcpHost.LastStartError) ? "unknown" : _tcpHost.LastStartError;
                Debug.LogWarning($"[Unitap] Failed to start TCP host ({_hostStartRetryCount + 1}/{HostStartRetryMax}): {reason}");
                _tcpHost.Dispose();
                _tcpHost = null;
            }

            _pipeHost = new UnitapPipeHost();
            var pipeStarted = _pipeHost.Start();
            if (!pipeStarted)
            {
                var reason = string.IsNullOrEmpty(_pipeHost.LastStartError) ? "unknown" : _pipeHost.LastStartError;
                Debug.LogWarning($"[Unitap] Failed to start pipe host ({reason})");
                _pipeHost.Dispose();
                _pipeHost = null;
            }

            _fileHost = new UnitapFileHost();
            var fileStarted = _fileHost.Start();
            if (!fileStarted)
            {
                var reason = string.IsNullOrEmpty(_fileHost.LastStartError) ? "unknown" : _fileHost.LastStartError;
                Debug.LogWarning($"[Unitap] Failed to start file host ({reason})");
                _fileHost.Dispose();
                _fileHost = null;
            }

            if (!tcpStarted && !pipeStarted && !fileStarted)
            {
                _hostStartRetryCount++;
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
                    Debug.LogError("[Unitap] Failed to start Unitap hosts after retries");
                }
                return;
            }

            _hostStartRetryCount = 0;

            _heartbeat = new UnitapHeartbeat();
            _heartbeat.Start(_tcpHost?.BoundPort ?? 0, _pipeHost?.PipeName, _fileHost?.RootDirectory);

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

            var tcpInfo = _tcpHost != null ? $"tcp:{_tcpHost.BoundPort}" : "tcp:off";
            var pipeInfo = _pipeHost != null ? $"pipe:{_pipeHost.PipeName}" : "pipe:off";
            var fileInfo = _fileHost != null ? $"file:{_fileHost.RootDirectory}" : "file:off";
            Debug.Log($"[Unitap] Started ({tcpInfo}, {pipeInfo}, {fileInfo})");
        }

        static void RegisterCommands()
        {
            _dispatcher.RegisterCommand("status", new Commands.StatusCommand());
            _dispatcher.RegisterCommand("play", new Commands.PlayCommand());
            _dispatcher.RegisterCommand("stop", new Commands.StopCommand());
            _dispatcher.RegisterCommand("execute_menu", new Commands.ExecuteMenuCommand());
            _dispatcher.RegisterCommand("refresh", new Commands.RefreshCommand());
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
            if ((_tcpHost == null || !_tcpHost.IsRunning) &&
                (_pipeHost == null || !_pipeHost.IsRunning) &&
                (_fileHost == null || !_fileHost.IsRunning)) return;
            _heartbeat?.Tick();
            _dispatcher?.ProcessQueue(_tcpHost, _pipeHost, _fileHost);
        }

        static void OnQuitting()
        {
            Shutdown(deleteHeartbeat: true);
            DeletePidFile();
        }
        static void OnBeforeAssemblyReload()
        {
            _heartbeat?.WriteReloading();
            Shutdown(deleteHeartbeat: false);
        }

        static void Shutdown(bool deleteHeartbeat)
        {
            EditorApplication.update -= Tick;
            EditorApplication.update -= RetryInitialize;
            _tcpHost?.Dispose();
            _pipeHost?.Dispose();
            _fileHost?.Dispose();
            _console?.Dispose();
            if (deleteHeartbeat)
                _heartbeat?.Dispose(); // ファイル削除はエディタ終了時のみ
            _tcpHost = null;
            _pipeHost = null;
            _fileHost = null;
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

        static void EnsurePidFile()
        {
            try
            {
                var path = UnitapPipeName.GetPidFilePath();
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir))
                {
                    Directory.CreateDirectory(dir);
                }

                File.WriteAllText(path, System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Unitap] Failed to write PID file: {ex.Message}");
            }
        }

        static void DeletePidFile()
        {
            try
            {
                var path = UnitapPipeName.GetPidFilePath();
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
                // ignore
            }
        }
    }
}
