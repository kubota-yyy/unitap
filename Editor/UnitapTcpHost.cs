using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// 127.0.0.1 上でTCPリスナーを起動し、8byte長さプレフィックス+UTF-8 JSONフレームで通信する。
    /// 受信したリクエストを ConcurrentQueue に積み、メインスレッドで処理する。
    /// </summary>
    public sealed class UnitapTcpHost : IUnitapHost
    {
        public const int DefaultPort = 6400;
        public const int MaxPortScan = 10;
        const int MaxFrameSize = 64 * 1024 * 1024; // 64MB
        const int HeaderSize = 8;

        public int BoundPort { get; private set; }
        public bool IsRunning => _running;
        public string LastStartError { get; private set; }

        readonly ConcurrentQueue<UnitapPendingRequest> _inbox = new();
        TcpListener _listener;
        Thread _acceptThread;
        volatile bool _running;

        public bool TryDequeue(out UnitapPendingRequest req) => _inbox.TryDequeue(out req);

        public bool Start()
        {
            LastStartError = null;
            string lastError = null;

            for (int offset = 0; offset < MaxPortScan; offset++)
            {
                int port = DefaultPort + offset;
                try
                {
                    _listener = new TcpListener(IPAddress.Loopback, port);
                    _listener.Start();
                    BoundPort = port;
                    _running = true;
                    _acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "Unitap-Accept" };
                    _acceptThread.Start();
                    Debug.Log($"[Unitap] TCP listening on 127.0.0.1:{port}");
                    return true;
                }
                catch (SocketException ex)
                {
                    // ポート使用中 → 次へ
                    lastError = $"port {port}: {ex.SocketErrorCode}";
                }
            }
            LastStartError = lastError ?? $"failed to bind TCP port {DefaultPort}-{DefaultPort + MaxPortScan - 1}";
            Debug.LogError($"[Unitap] Failed to bind TCP port {DefaultPort}-{DefaultPort + MaxPortScan - 1} ({LastStartError})");
            return false;
        }

        public void Dispose()
        {
            _running = false;
            try { _listener?.Stop(); } catch { /* ignore */ }
            _acceptThread?.Join(2000);
        }

        void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client = null;
                try
                {
                    client = _listener.AcceptTcpClient();
                    client.NoDelay = true;
                    var thread = new Thread(() => HandleClient(client))
                    {
                        IsBackground = true,
                        Name = "Unitap-Client"
                    };
                    thread.Start();
                }
                catch (SocketException) when (!_running)
                {
                    break;
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[Unitap] Accept error: {ex.Message}");
                }
            }
        }

        void HandleClient(TcpClient client)
        {
            try
            {
                using var stream = client.GetStream();
                stream.ReadTimeout = 120_000;
                stream.WriteTimeout = 30_000;
                var buf = new byte[HeaderSize];

                while (_running && client.Connected)
                {
                    // ヘッダ読み取り (8 bytes big-endian length)
                    if (!ReadExact(stream, buf, 0, HeaderSize)) break;
                    long len = ReadInt64BE(buf, 0);
                    if (len <= 0 || len > MaxFrameSize)
                    {
                        Debug.LogWarning($"[Unitap] Invalid frame size: {len}");
                        break;
                    }

                    // ペイロード読み取り
                    var payload = new byte[len];
                    if (!ReadExact(stream, payload, 0, (int)len)) break;
                    var json = Encoding.UTF8.GetString(payload);

                    UnitapRequest req;
                    try
                    {
                        req = JsonConvert.DeserializeObject<UnitapRequest>(json);
                    }
                    catch (Exception ex)
                    {
                        SendError(stream, null, "parse_error", $"Invalid JSON: {ex.Message}");
                        continue;
                    }

                    if (req.Version != 1)
                    {
                        SendError(stream, req.RequestId, "unsupported_version", $"Version {req.Version} not supported");
                        continue;
                    }

                    // メインスレッドで処理するためキューに積む
                    var responded = false;
                    var pending = new UnitapPendingRequest
                    {
                        Request = req,
                        Respond = resp =>
                        {
                            if (responded) return;
                            responded = true;
                            try { WriteFrame(stream, resp); }
                            catch (Exception ex) { Debug.LogWarning($"[Unitap] Write error: {ex.Message}"); }
                        }
                    };
                    _inbox.Enqueue(pending);

                    // レスポンスが返るまで待機 (タイムアウト付き)
                    var deadline = DateTime.UtcNow.AddMilliseconds(req.TimeoutMs > 0 ? req.TimeoutMs : 30000);
                    while (!responded && DateTime.UtcNow < deadline && _running)
                    {
                        Thread.Sleep(10);
                    }

                    if (!responded)
                    {
                        SendError(stream, req.RequestId, "timeout", "Command timed out");
                        responded = true;
                    }
                }
            }
            catch (Exception ex)
            {
                if (_running)
                    Debug.LogWarning($"[Unitap] Client error: {ex.Message}");
            }
            finally
            {
                try { client?.Close(); } catch { /* ignore */ }
            }
        }

        void SendError(NetworkStream stream, string requestId, string code, string message)
        {
            var resp = new UnitapResponse
            {
                RequestId = requestId,
                Ok = false,
                Error = new UnitapError { Code = code, Message = message },
                Editor = UnitapEntry.GetLastKnownEditorState(),
                CompletedAtUtc = DateTime.UtcNow.ToString("O")
            };
            try { WriteFrame(stream, resp); }
            catch { /* ignore */ }
        }

        static void WriteFrame(NetworkStream stream, UnitapResponse resp)
        {
            var json = JsonConvert.SerializeObject(resp, Formatting.None);
            var payload = Encoding.UTF8.GetBytes(json);
            var header = new byte[HeaderSize];
            WriteInt64BE(header, 0, payload.Length);
            lock (stream)
            {
                stream.Write(header, 0, HeaderSize);
                stream.Write(payload, 0, payload.Length);
                stream.Flush();
            }
        }

        static bool ReadExact(NetworkStream stream, byte[] buf, int offset, int count)
        {
            int read = 0;
            while (read < count)
            {
                int n;
                try { n = stream.Read(buf, offset + read, count - read); }
                catch { return false; }
                if (n <= 0) return false;
                read += n;
            }
            return true;
        }

        static long ReadInt64BE(byte[] buf, int offset)
        {
            long v = 0;
            for (int i = 0; i < 8; i++)
                v = (v << 8) | buf[offset + i];
            return v;
        }

        static void WriteInt64BE(byte[] buf, int offset, long value)
        {
            for (int i = 7; i >= 0; i--)
            {
                buf[offset + i] = (byte)(value & 0xFF);
                value >>= 8;
            }
        }
    }
}
