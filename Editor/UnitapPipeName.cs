using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

namespace Unitap
{
    public static class UnitapPipeName
    {
        public static string GetPipeName()
        {
            var projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."))
                .Replace('\\', '/')
                .TrimEnd('/');
            using var sha = SHA256.Create();
            var bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(projectRoot));
            var builder = new StringBuilder(16);
            for (var i = 0; i < 8; i++)
            {
                builder.Append(bytes[i].ToString("x2"));
            }

            return $"unitap_{builder}";
        }

        public static string GetUnixSocketPath(string pipeName)
        {
            if (string.IsNullOrEmpty(pipeName))
            {
                return null;
            }

            return Path.Combine(Path.GetTempPath(), $"CoreFxPipe_{pipeName}");
        }

        public static string GetPidFilePath()
            => Path.Combine(Application.dataPath, "..", "Library", "Unitap", "server.pid");

        public static string GetFileTransportDirectory()
            => Path.Combine(Application.dataPath, "..", "Library", "Unitap", "file-transport");

        public static string GetFileTransportRequestsDirectory()
            => Path.Combine(GetFileTransportDirectory(), "requests");

        public static string GetFileTransportProcessingDirectory()
            => Path.Combine(GetFileTransportDirectory(), "processing");

        public static string GetFileTransportResponsesDirectory()
            => Path.Combine(GetFileTransportDirectory(), "responses");
    }
}
