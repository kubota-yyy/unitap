using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using Newtonsoft.Json;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace Unitap
{
    /// <summary>
    /// Library/Unitap/.heartbeat.json を定期更新する。
    /// CLIはこのファイルの鮮度 (lastHeartbeat) でUnity生存を判定する。
    /// </summary>
    public sealed class UnitapHeartbeat : IDisposable
    {
        string _heartbeatPath;
        int _port;
        string _pipeName;
        string _fileTransportDir;
        double _lastWrite;
        const double IntervalSeconds = 0.8;

        public void Start(int port, string pipeName, string fileTransportDir)
        {
            _port = port;
            _pipeName = pipeName;
            _fileTransportDir = fileTransportDir;
            var dir = Path.Combine(Application.dataPath, "..", "Library", "Unitap");
            Directory.CreateDirectory(dir);
            // ディレクトリのパーミッション設定 (Unix系のみ)
            try
            {
                var psi = new ProcessStartInfo("chmod", $"700 \"{dir}\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process.Start(psi)?.WaitForExit(1000);
            }
            catch { /* Windows等では無視 */ }

            _heartbeatPath = Path.Combine(dir, ".heartbeat.json");
            Write();
        }

        public void Tick()
        {
            if (UnityEditor.EditorApplication.timeSinceStartup - _lastWrite < IntervalSeconds) return;
            Write();
        }

        /// <summary>
        /// ドメインリロード直前に呼び出し、isCompiling=true で最終書き込みする。
        /// CLIはこのフラグを見てstale閾値を延長する。
        /// </summary>
        public void WriteReloading()
        {
            if (_heartbeatPath == null) return;
            try
            {
                var data = new HeartbeatData
                {
                    Pid = Process.GetCurrentProcess().Id,
                    Port = _port,
                    PipeName = _pipeName,
                    PipeSocketPath = UnitapPipeName.GetUnixSocketPath(_pipeName),
                    FileTransportDir = _fileTransportDir,
                    AvailableTransports = BuildAvailableTransports(),
                    PidFile = UnitapPipeName.GetPidFilePath(),
                    ProjectPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..")),
                    ProjectName = Application.productName,
                    UnityVersion = Application.unityVersion,
                    LastHeartbeat = DateTime.UtcNow.ToString("O"),
                    IsCompiling = true,
                    IsPlaying = false,
                    HasErrors = false,
                    ErrorCount = 0
                };
                var json = JsonConvert.SerializeObject(data, Formatting.Indented);
                WriteHeartbeatAtomically(json);
            }
            catch { /* ignore */ }
        }

        public void Dispose()
        {
            try
            {
                if (_heartbeatPath != null && File.Exists(_heartbeatPath))
                    File.Delete(_heartbeatPath);
            }
            catch { /* ignore */ }
        }

        void Write()
        {
            _lastWrite = UnityEditor.EditorApplication.timeSinceStartup;
            var errorStats = GetErrorStats();
            var data = new HeartbeatData
            {
                Pid = Process.GetCurrentProcess().Id,
                Port = _port,
                PipeName = _pipeName,
                PipeSocketPath = UnitapPipeName.GetUnixSocketPath(_pipeName),
                FileTransportDir = _fileTransportDir,
                AvailableTransports = BuildAvailableTransports(),
                PidFile = UnitapPipeName.GetPidFilePath(),
                ProjectPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..")),
                ProjectName = Application.productName,
                UnityVersion = Application.unityVersion,
                LastHeartbeat = DateTime.UtcNow.ToString("O"),
                IsCompiling = UnityEditor.EditorApplication.isCompiling,
                IsPlaying = UnityEditor.EditorApplication.isPlaying,
                HasErrors = errorStats.hasErrors,
                ErrorCount = errorStats.count
            };
            try
            {
                var json = JsonConvert.SerializeObject(data, Formatting.Indented);
                WriteHeartbeatAtomically(json);
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Unitap] Heartbeat write error: {ex.Message}");
            }
        }

        void WriteHeartbeatAtomically(string json)
        {
            if (_heartbeatPath == null)
                return;

            var tempPath = _heartbeatPath + ".tmp";
            File.WriteAllText(tempPath, json);

            if (File.Exists(_heartbeatPath))
            {
                try
                {
                    File.Replace(tempPath, _heartbeatPath, null, true);
                    return;
                }
                catch
                {
                    // File.Replace が使えない環境では Move にフォールバック
                }
            }

            if (File.Exists(_heartbeatPath))
                File.Delete(_heartbeatPath);
            File.Move(tempPath, _heartbeatPath);
        }

        static (bool hasErrors, int count) GetErrorStats()
        {
            int count = 0;
            var console = UnitapEntry.Console;
            if (console != null)
                count += console.ErrorCount;
            count += UnitapCompileErrorCapture.ErrorCount;
            return (count > 0, count);
        }

        string[] BuildAvailableTransports()
        {
            var transports = new List<string>();
            if (_port > 0)
            {
                transports.Add("tcp");
            }

            if (!string.IsNullOrEmpty(_pipeName))
            {
                transports.Add("pipe");
            }

            if (!string.IsNullOrEmpty(_fileTransportDir))
            {
                transports.Add("file");
            }

            return transports.ToArray();
        }
    }
}
