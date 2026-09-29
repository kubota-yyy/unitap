using System;
using System.IO;

namespace Unitap
{
    /// <summary>
    /// Library/Unitap の jsonl 追記。上限を超えたら 1 世代 (.1.jsonl) だけ残して切り替え、
    /// 長期運用で履歴ファイルが際限なく肥大しないようにする。
    /// </summary>
    internal static class UnitapJsonl
    {
        internal const long DefaultMaxBytes = 8L * 1024 * 1024;

        internal static void Append(string path, string line, long maxBytes = DefaultMaxBytes)
        {
            if (string.IsNullOrEmpty(path)) return;
            RotateIfNeeded(path, maxBytes);
            File.AppendAllText(path, line + "\n");
        }

        static void RotateIfNeeded(string path, long maxBytes)
        {
            if (maxBytes <= 0) return;
            try
            {
                var info = new FileInfo(path);
                if (!info.Exists || info.Length <= maxBytes) return;

                var rotated = Path.Combine(
                    Path.GetDirectoryName(path) ?? string.Empty,
                    Path.GetFileNameWithoutExtension(path) + ".1" + Path.GetExtension(path));
                if (File.Exists(rotated)) File.Delete(rotated);
                File.Move(path, rotated);
            }
            catch (Exception)
            {
                // rotate 失敗時は追記だけ続ける
            }
        }
    }
}
