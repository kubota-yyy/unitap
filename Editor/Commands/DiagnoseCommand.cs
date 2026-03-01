using System.Collections.Generic;
using UnityEditor;
using UnityEngine;

namespace Unitap.Commands
{
    public sealed class DiagnoseCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var issues = new List<object>();

            if (EditorApplication.isCompiling)
            {
                issues.Add(new
                {
                    issue = "compiling",
                    detail = "Unity is currently compiling scripts",
                    next_actions = new[] { "wait_idle", "read_console --type error" }
                });
            }

            if (EditorApplication.isUpdating)
            {
                issues.Add(new
                {
                    issue = "updating",
                    detail = "AssetDatabase is updating",
                    next_actions = new[] { "wait_idle" }
                });
            }

            var console = UnitapEntry.Console;
            int errorCount = 0;
            if (console != null)
            {
                errorCount += console.GetEntries(LogType.Error, 5000).Count;
                errorCount += console.GetEntries(LogType.Exception, 5000).Count;
                errorCount += console.GetEntries(LogType.Assert, 5000).Count;
            }
            // CompilationPipeline 経由のコンパイルエラーも加算
            errorCount += UnitapCompileErrorCapture.GetEntries().FindAll(e => e.Level == "error").Count;
            if (errorCount > 0)
            {
                issues.Add(new
                {
                    issue = "compile_errors",
                    detail = $"{errorCount} errors in console",
                    next_actions = new[] { "read_console --type error", "compile_check" }
                });
            }

            if (EditorApplication.isPlaying)
            {
                if (EditorApplication.isPaused)
                {
                    issues.Add(new
                    {
                        issue = "paused",
                        detail = "Editor is in play mode but paused",
                        next_actions = new[] { "stop", "play" }
                    });
                }
                else
                {
                    issues.Add(new
                    {
                        issue = "playing",
                        detail = "Editor is in play mode",
                        next_actions = new[] { "stop" }
                    });
                }
            }

            if (EditorApplication.isPlayingOrWillChangePlaymode && !EditorApplication.isPlaying)
            {
                issues.Add(new
                {
                    issue = "entering_playmode",
                    detail = "Editor is transitioning to play mode",
                    next_actions = new[] { "wait_idle", "stop" }
                });
            }

            if (issues.Count == 0)
            {
                return new
                {
                    status = "ok",
                    detail = "No issues detected. Editor is idle and ready.",
                    issues,
                    next_actions = new string[] { }
                };
            }

            return new
            {
                status = "issues_found",
                detail = $"{issues.Count} issue(s) detected",
                issues,
                next_actions = new[] { "status" }
            };
        }
    }
}
