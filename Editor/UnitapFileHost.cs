using System;
using System.IO;
using Newtonsoft.Json;
using UnityEngine;

namespace Unitap
{
    /// <summary>
    /// ファイルベースの request/response transport。
    /// socket を使えない sandbox からも利用できるようにする。
    /// </summary>
    public sealed class UnitapFileHost : IUnitapHost
    {
        public bool IsRunning { get; private set; }
        public string RootDirectory { get; private set; }
        public string LastStartError { get; private set; }

        string _requestsDirectory;
        string _processingDirectory;
        string _responsesDirectory;

        public bool Start()
        {
            try
            {
                RootDirectory = UnitapPipeName.GetFileTransportDirectory();
                _requestsDirectory = UnitapPipeName.GetFileTransportRequestsDirectory();
                _processingDirectory = UnitapPipeName.GetFileTransportProcessingDirectory();
                _responsesDirectory = UnitapPipeName.GetFileTransportResponsesDirectory();

                Directory.CreateDirectory(RootDirectory);
                Directory.CreateDirectory(_requestsDirectory);
                Directory.CreateDirectory(_processingDirectory);
                Directory.CreateDirectory(_responsesDirectory);
                RestoreProcessingFiles();

                LastStartError = null;
                IsRunning = true;
                Debug.Log($"[Unitap] File transport ready at {RootDirectory}");
                return true;
            }
            catch (Exception ex)
            {
                LastStartError = ex.Message;
                IsRunning = false;
                Debug.LogError($"[Unitap] Failed to start file host ({ex.Message})");
                return false;
            }
        }

        public bool TryDequeue(out UnitapPendingRequest pendingRequest)
        {
            pendingRequest = default;
            if (!IsRunning || string.IsNullOrEmpty(_requestsDirectory))
            {
                return false;
            }

            string[] files;
            try
            {
                files = Directory.GetFiles(_requestsDirectory, "*.json");
            }
            catch
            {
                return false;
            }

            Array.Sort(files, StringComparer.Ordinal);
            foreach (var requestPath in files)
            {
                var fileName = Path.GetFileName(requestPath);
                var processingPath = Path.Combine(_processingDirectory, fileName);

                try
                {
                    File.Move(requestPath, processingPath);
                }
                catch
                {
                    continue;
                }

                var request = LoadRequest(processingPath, fileName, out var immediateResponse);
                if (immediateResponse != null)
                {
                    WriteResponse(processingPath, fileName, immediateResponse);
                    continue;
                }

                if (request == null)
                {
                    continue;
                }

                pendingRequest = new UnitapPendingRequest
                {
                    Request = request,
                    Respond = response => WriteResponse(processingPath, fileName, response)
                };
                return true;
            }

            return false;
        }

        public void Dispose()
        {
            IsRunning = false;
        }

        void RestoreProcessingFiles()
        {
            if (!Directory.Exists(_processingDirectory))
            {
                return;
            }

            foreach (var processingPath in Directory.GetFiles(_processingDirectory, "*.json"))
            {
                var targetPath = Path.Combine(_requestsDirectory, Path.GetFileName(processingPath));
                try
                {
                    if (File.Exists(targetPath))
                    {
                        File.Delete(processingPath);
                        continue;
                    }

                    File.Move(processingPath, targetPath);
                }
                catch
                {
                    // ignore
                }
            }
        }

        UnitapRequest LoadRequest(string processingPath, string fileName, out UnitapResponse immediateResponse)
        {
            immediateResponse = null;
            try
            {
                var json = File.ReadAllText(processingPath);
                var request = JsonConvert.DeserializeObject<UnitapRequest>(json);
                if (request == null)
                {
                    immediateResponse = MakeImmediateError(null, "parse_error", "Invalid JSON payload");
                    return null;
                }

                if (request.Version != 1)
                {
                    immediateResponse = MakeImmediateError(
                        request.RequestId,
                        "unsupported_version",
                        $"Version {request.Version} not supported"
                    );
                    return null;
                }

                return request;
            }
            catch (Exception ex)
            {
                immediateResponse = MakeImmediateError(
                    Path.GetFileNameWithoutExtension(fileName),
                    "parse_error",
                    $"Invalid JSON: {ex.Message}"
                );
                return null;
            }
        }

        void WriteResponse(string processingPath, string requestFileName, UnitapResponse response)
        {
            var requestId = response?.RequestId;
            if (string.IsNullOrEmpty(requestId))
            {
                requestId = Path.GetFileNameWithoutExtension(requestFileName);
            }

            var responsePath = Path.Combine(_responsesDirectory, $"{requestId}.json");
            var tempPath = responsePath + ".tmp";

            try
            {
                var json = JsonConvert.SerializeObject(response, Formatting.None);
                File.WriteAllText(tempPath, json);
                if (File.Exists(responsePath))
                {
                    File.Delete(responsePath);
                }
                File.Move(tempPath, responsePath);
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Unitap] File response write error: {ex.Message}");
                try
                {
                    if (File.Exists(tempPath))
                    {
                        File.Delete(tempPath);
                    }
                }
                catch
                {
                    // ignore
                }
            }
            finally
            {
                try
                {
                    if (File.Exists(processingPath))
                    {
                        File.Delete(processingPath);
                    }
                }
                catch
                {
                    // ignore
                }
            }
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
