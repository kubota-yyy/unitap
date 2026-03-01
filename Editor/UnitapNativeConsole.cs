using System;
using System.Reflection;
using UnityEngine;

namespace Unitap
{
    internal static class UnitapNativeConsole
    {
        static readonly string[] CandidateTypeNames =
        {
            "UnityEditor.LogEntries, UnityEditor.dll",
            "UnityEditorInternal.LogEntries, UnityEditor.dll"
        };

        static MethodInfo _clearMethod;
        static bool _resolved;

        public static bool Clear()
        {
            var clearMethod = ResolveClearMethod();
            if (clearMethod == null)
                return false;

            try
            {
                clearMethod.Invoke(null, null);
                return true;
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Unitap] Failed to clear native Unity console: {ex.Message}");
                return false;
            }
        }

        static MethodInfo ResolveClearMethod()
        {
            if (_resolved)
                return _clearMethod;

            _resolved = true;
            foreach (var typeName in CandidateTypeNames)
            {
                var type = Type.GetType(typeName);
                if (type == null)
                    continue;

                _clearMethod = type.GetMethod("Clear", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                if (_clearMethod != null)
                    return _clearMethod;
            }

            return null;
        }
    }
}
