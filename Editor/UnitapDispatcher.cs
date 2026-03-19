using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using Unitap.Commands;

namespace Unitap
{
    /// <summary>
    /// メインスレッドでコマンドを実行するディスパッチャー。
    /// idempotencyKey による重複実行抑止を行う。
    /// </summary>
    public sealed class UnitapDispatcher
    {
        readonly Dictionary<string, IUnitapCommand> _commands = new();
        readonly ConcurrentDictionary<string, DateTime> _processedKeys = new();
        const int MaxProcessedKeys = 500;
        static EditorState _lastKnownEditorState = new();
        string _journalPath;
        double _lastPurge;

        public void RegisterCommand(string name, IUnitapCommand command)
        {
            _commands[name] = command;
        }

        public void Init()
        {
            var dir = Path.Combine(Application.dataPath, "..", "Library", "Unitap");
            Directory.CreateDirectory(dir);
            _journalPath = Path.Combine(dir, "processed-journal.jsonl");
        }

        /// <summary>
        /// メインスレッドから呼ばれる。キューから取り出して実行。
        /// </summary>
        public void ProcessQueue(params IUnitapHost[] hosts)
        {
            // 5分ごとに古い idempotency key をパージ
            var now = EditorApplication.timeSinceStartup;
            if (now - _lastPurge > 300)
            {
                _lastPurge = now;
                PurgeOldKeys();
            }

            // 1フレームで最大8件処理 (UIフリーズ防止)
            int processed = 0;
            if (hosts == null)
            {
                return;
            }

            foreach (var host in hosts)
            {
                if (host == null)
                {
                    continue;
                }

                while (processed < 8 && host.TryDequeue(out var pending))
                {
                    processed++;
                    var req = pending.Request;
                    UnitapResponse resp;

                    try
                    {
                        // idempotencyKey 重複チェック
                        if (!string.IsNullOrEmpty(req.IdempotencyKey) && _processedKeys.TryGetValue(req.IdempotencyKey, out _))
                        {
                            resp = MakeOk(req.RequestId, new { duplicate = true, message = "Already processed" });
                            pending.Respond(resp);
                            continue;
                        }

                        if (!_commands.TryGetValue(req.Command, out var cmd))
                        {
                            resp = MakeError(req.RequestId, "unknown_command", $"Unknown command: {req.Command}");
                            pending.Respond(resp);
                            continue;
                        }

                        WriteJournal(req.IdempotencyKey, "received", req.Command);
                        var result = cmd.Execute(req);

                        if (!string.IsNullOrEmpty(req.IdempotencyKey))
                        {
                            _processedKeys.TryAdd(req.IdempotencyKey, DateTime.UtcNow);
                        }

                        WriteJournal(req.IdempotencyKey, "completed", req.Command);
                        resp = MakeOk(req.RequestId, result);
                    }
                    catch (Exception ex)
                    {
                        Debug.LogError($"[Unitap] Command '{req.Command}' error: {ex}");
                        resp = MakeError(req.RequestId, "execution_error", ex.Message);
                    }

                    pending.Respond(resp);
                }

                if (processed >= 8)
                {
                    break;
                }
            }
        }

        public static EditorState GetEditorState()
        {
            _lastKnownEditorState = new EditorState
            {
                IsPlaying = EditorApplication.isPlaying,
                IsCompiling = EditorApplication.isCompiling,
                IsUpdating = EditorApplication.isUpdating,
                ActiveScene = EditorSceneManager.GetActiveScene().path
            };
            return CloneEditorState(_lastKnownEditorState);
        }

        public static EditorState GetLastKnownEditorState()
        {
            return CloneEditorState(_lastKnownEditorState);
        }

        static EditorState CloneEditorState(EditorState editorState)
        {
            return new EditorState
            {
                IsPlaying = editorState.IsPlaying,
                IsCompiling = editorState.IsCompiling,
                IsUpdating = editorState.IsUpdating,
                ActiveScene = editorState.ActiveScene
            };
        }

        UnitapResponse MakeOk(string requestId, object result)
        {
            return new UnitapResponse
            {
                RequestId = requestId,
                Ok = true,
                Result = result,
                Editor = GetEditorState(),
                CompletedAtUtc = DateTime.UtcNow.ToString("O")
            };
        }

        UnitapResponse MakeError(string requestId, string code, string message)
        {
            return new UnitapResponse
            {
                RequestId = requestId,
                Ok = false,
                Error = new UnitapError { Code = code, Message = message },
                Editor = GetEditorState(),
                CompletedAtUtc = DateTime.UtcNow.ToString("O")
            };
        }

        void PurgeOldKeys()
        {
            var cutoff = DateTime.UtcNow.AddMinutes(-5);
            var toRemove = new List<string>();
            foreach (var kv in _processedKeys)
            {
                if (kv.Value < cutoff)
                    toRemove.Add(kv.Key);
            }
            foreach (var key in toRemove)
                _processedKeys.TryRemove(key, out _);

            // 上限超過時は古い順に削除
            if (_processedKeys.Count > MaxProcessedKeys)
            {
                var sorted = new List<KeyValuePair<string, DateTime>>(_processedKeys);
                sorted.Sort((a, b) => a.Value.CompareTo(b.Value));
                int removeCount = sorted.Count - MaxProcessedKeys;
                for (int i = 0; i < removeCount; i++)
                    _processedKeys.TryRemove(sorted[i].Key, out _);
            }
        }

        void WriteJournal(string idempotencyKey, string phase, string command)
        {
            if (string.IsNullOrEmpty(idempotencyKey) || _journalPath == null) return;
            try
            {
                var entry = JsonConvert.SerializeObject(new
                {
                    idempotencyKey,
                    phase,
                    command,
                    timestamp = DateTime.UtcNow.ToString("O")
                });
                File.AppendAllText(_journalPath, entry + "\n");
            }
            catch { /* ジャーナル書き込み失敗は無視 */ }
        }
    }
}
