using System;
using System.Collections.Concurrent;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// project 固有の named pipe で待ち受ける host。
    /// Unix 系では .NET runtime により Unix domain socket にマップされる。
    /// </summary>
    public sealed class UnitapPipeHost : IUnitapHost
    {
        const int MaxFrameSize = 64 * 1024 * 1024;
        const byte AckByte = 0x01;
        const int HeaderSize = 4;

        readonly ConcurrentQueue<UnitapPendingRequest> _inbox = new();
        readonly object _acceptingPipeLock = new();

        NamedPipeServerStream _acceptingPipe;
        Thread _acceptThread;
        volatile bool _running;

        public bool IsRunning => _running;
        public string PipeName { get; private set; }
        public string LastStartError { get; private set; }

        public bool TryDequeue(out UnitapPendingRequest request) => _inbox.TryDequeue(out request);

        public bool Start()
        {
            try
            {
                PipeName = UnitapPipeName.GetPipeName();
                LastStartError = null;
                _running = true;
                _acceptThread = new Thread(AcceptLoop)
                {
                    IsBackground = true,
                    Name = "Unitap-Pipe-Accept"
                };
                _acceptThread.Start();
                Debug.Log($"[Unitap] Pipe listening on {PipeName}");
                return true;
            }
            catch (Exception ex)
            {
                LastStartError = ex.Message;
                _running = false;
                Debug.LogError($"[Unitap] Failed to start pipe host ({ex.Message})");
                return false;
            }
        }

        public void Dispose()
        {
            _running = false;
            DisposeAcceptingPipe();
            _acceptThread?.Join(2000);
        }

        void AcceptLoop()
        {
            while (_running)
            {
                NamedPipeServerStream pipe = null;
                try
                {
                    pipe = new NamedPipeServerStream(
                        PipeName,
                        PipeDirection.InOut,
                        NamedPipeServerStream.MaxAllowedServerInstances,
                        PipeTransmissionMode.Byte,
                        PipeOptions.Asynchronous
                    );

                    lock (_acceptingPipeLock)
                    {
                        _acceptingPipe = pipe;
                    }

                    pipe.WaitForConnection();

                    lock (_acceptingPipeLock)
                    {
                        if (_acceptingPipe == pipe)
                        {
                            _acceptingPipe = null;
                        }
                    }

                    var thread = new Thread(() => HandleClient(pipe))
                    {
                        IsBackground = true,
                        Name = "Unitap-Pipe-Client"
                    };
                    thread.Start();
                }
                catch (IOException) when (!_running)
                {
                    pipe?.Dispose();
                    break;
                }
                catch (ObjectDisposedException)
                {
                    pipe?.Dispose();
                    break;
                }
                catch (Exception ex)
                {
                    pipe?.Dispose();
                    if (_running)
                    {
                        Debug.LogWarning($"[Unitap] Pipe accept error: {ex.Message}");
                        Thread.Sleep(200);
                    }
                }
                finally
                {
                    lock (_acceptingPipeLock)
                    {
                        if (_acceptingPipe == pipe)
                        {
                            _acceptingPipe = null;
                        }
                    }
                }
            }
        }

        void HandleClient(NamedPipeServerStream pipe)
        {
            try
            {
                using (pipe)
                {
                    pipe.ReadTimeout = 120_000;
                    pipe.WriteTimeout = 30_000;
                    var header = new byte[HeaderSize];

                    while (_running && pipe.IsConnected)
                    {
                        if (!ReadExact(pipe, header, 0, HeaderSize))
                        {
                            break;
                        }

                        var length = BitConverter.ToInt32(header, 0);
                        if (length <= 0 || length > MaxFrameSize)
                        {
                            Debug.LogWarning($"[Unitap] Invalid pipe frame size: {length}");
                            break;
                        }

                        var payload = new byte[length];
                        if (!ReadExact(pipe, payload, 0, length))
                        {
                            break;
                        }

                        UnitapResponse immediateResponse = null;
                        UnitapRequest request = null;
                        try
                        {
                            var json = Encoding.UTF8.GetString(payload);
                            request = JsonConvert.DeserializeObject<UnitapRequest>(json);
                            if (request == null)
                            {
                                immediateResponse = MakeImmediateError(null, "parse_error", "Invalid JSON payload");
                            }
                            else if (request.Version != 1)
                            {
                                immediateResponse = MakeImmediateError(
                                    request.RequestId,
                                    "unsupported_version",
                                    $"Version {request.Version} not supported"
                                );
                            }
                        }
                        catch (Exception ex)
                        {
                            immediateResponse = MakeImmediateError(null, "parse_error", $"Invalid JSON: {ex.Message}");
                        }

                        if (!WriteAck(pipe))
                        {
                            break;
                        }

                        if (immediateResponse != null)
                        {
                            if (!WriteFrame(pipe, immediateResponse))
                            {
                                break;
                            }
                            continue;
                        }

                        var responded = false;
                        var pending = new UnitapPendingRequest
                        {
                            Request = request,
                            Respond = response =>
                            {
                                if (responded)
                                {
                                    return;
                                }

                                responded = true;
                                try
                                {
                                    WriteFrame(pipe, response);
                                }
                                catch (Exception ex)
                                {
                                    Debug.LogWarning($"[Unitap] Pipe write error: {ex.Message}");
                                }
                            }
                        };
                        _inbox.Enqueue(pending);

                        var deadline = DateTime.UtcNow.AddMilliseconds(request.TimeoutMs > 0 ? request.TimeoutMs : 30000);
                        while (!responded && DateTime.UtcNow < deadline && _running)
                        {
                            Thread.Sleep(10);
                        }

                        if (!responded)
                        {
                            var timeoutResponse = MakeImmediateError(request.RequestId, "timeout", "Command timed out");
                            WriteFrame(pipe, timeoutResponse);
                            responded = true;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                if (_running)
                {
                    Debug.LogWarning($"[Unitap] Pipe client error: {ex.Message}");
                }
            }
        }

        void DisposeAcceptingPipe()
        {
            lock (_acceptingPipeLock)
            {
                try
                {
                    _acceptingPipe?.Dispose();
                }
                catch
                {
                    // ignore
                }

                _acceptingPipe = null;
            }
        }

        static bool WriteAck(NamedPipeServerStream pipe)
        {
            try
            {
                pipe.WriteByte(AckByte);
                pipe.Flush();
                return true;
            }
            catch
            {
                return false;
            }
        }

        static bool WriteFrame(NamedPipeServerStream pipe, UnitapResponse response)
        {
            var json = JsonConvert.SerializeObject(response, Formatting.None);
            var payload = Encoding.UTF8.GetBytes(json);
            var header = BitConverter.GetBytes(payload.Length);

            lock (pipe)
            {
                pipe.Write(header, 0, header.Length);
                pipe.Write(payload, 0, payload.Length);
                pipe.Flush();
            }

            return true;
        }

        static bool ReadExact(Stream stream, byte[] buffer, int offset, int count)
        {
            var read = 0;
            while (read < count)
            {
                int bytesRead;
                try
                {
                    bytesRead = stream.Read(buffer, offset + read, count - read);
                }
                catch
                {
                    return false;
                }

                if (bytesRead <= 0)
                {
                    return false;
                }

                read += bytesRead;
            }

            return true;
        }

        static UnitapResponse MakeImmediateError(string requestId, string code, string message)
        {
            return new UnitapResponse
            {
                RequestId = requestId,
                Ok = false,
                Error = new UnitapError { Code = code, Message = message },
                Editor = UnitapEntry.GetLastKnownEditorState(),
                CompletedAtUtc = DateTime.UtcNow.ToString("O")
            };
        }
    }
}
