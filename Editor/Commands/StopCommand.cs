using UnityEditor;
using System;
using System.Reflection;

namespace Unitap.Commands
{
    public sealed class StopCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            if (!EditorApplication.isPlaying)
                return new { already = true, message = "Not in play mode" };

            PrepareForPlayModeExit();
            EditorApplication.isPlaying = false;
            return new { stopped = true };
        }

        private static void PrepareForPlayModeExit()
        {
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                var cleanupType = assembly.GetType("FirestorePlayModeCleanup", throwOnError: false);
                var method = cleanupType?.GetMethod(
                    "PrepareForPlayModeExit",
                    BindingFlags.Public | BindingFlags.Static
                );
                if (method == null) continue;

                method.Invoke(null, null);
                return;
            }
        }
    }
}
