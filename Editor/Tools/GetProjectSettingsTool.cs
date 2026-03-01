using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Unitap.Tools
{
    [McpForUnityTool("get_project_settings")]
    public static class GetProjectSettingsTool
    {
        public static object HandleCommand(JObject @params)
        {
            var category = @params["category"]?.ToString()?.ToLowerInvariant();
            if (string.IsNullOrEmpty(category))
                return new ErrorResponse("category is required (player, quality, build)");

            return category switch
            {
                "player" => GetPlayerSettings(),
                "quality" => GetQualitySettings(),
                "build" => GetBuildSettings(),
                _ => new ErrorResponse($"Unknown category: {category}. Valid: player, quality, build")
            };
        }

        static object GetPlayerSettings()
        {
            return new SuccessResponse("Player settings", new
            {
                companyName = PlayerSettings.companyName,
                productName = PlayerSettings.productName,
                bundleVersion = PlayerSettings.bundleVersion,
                applicationIdentifier = PlayerSettings.applicationIdentifier,
                defaultInterfaceOrientation = PlayerSettings.defaultInterfaceOrientation.ToString(),
                allowedAutorotateToPortrait = PlayerSettings.allowedAutorotateToPortrait,
                allowedAutorotateToLandscapeLeft = PlayerSettings.allowedAutorotateToLandscapeLeft,
                allowedAutorotateToLandscapeRight = PlayerSettings.allowedAutorotateToLandscapeRight,
                statusBarHidden = PlayerSettings.statusBarHidden,
                apiCompatibilityLevel = PlayerSettings.GetApiCompatibilityLevel(EditorUserBuildSettings.selectedBuildTargetGroup).ToString(),
                scriptingBackend = PlayerSettings.GetScriptingBackend(EditorUserBuildSettings.selectedBuildTargetGroup).ToString(),
                targetArchitecture = PlayerSettings.Android.targetArchitectures.ToString(),
                colorSpace = PlayerSettings.colorSpace.ToString()
            });
        }

        static object GetQualitySettings()
        {
            return new SuccessResponse("Quality settings", new
            {
                names = QualitySettings.names,
                activeQualityLevel = QualitySettings.GetQualityLevel(),
                activeQualityName = QualitySettings.names[QualitySettings.GetQualityLevel()],
                vSyncCount = QualitySettings.vSyncCount,
                antiAliasing = QualitySettings.antiAliasing,
                shadowResolution = QualitySettings.shadowResolution.ToString(),
                textureQuality = QualitySettings.globalTextureMipmapLimit,
                anisotropicFiltering = QualitySettings.anisotropicFiltering.ToString()
            });
        }

        static object GetBuildSettings()
        {
            return new SuccessResponse("Build settings", new
            {
                activeBuildTarget = EditorUserBuildSettings.activeBuildTarget.ToString(),
                selectedBuildTargetGroup = EditorUserBuildSettings.selectedBuildTargetGroup.ToString(),
                development = EditorUserBuildSettings.development,
                il2cpp = PlayerSettings.GetScriptingBackend(EditorUserBuildSettings.selectedBuildTargetGroup) == ScriptingImplementation.IL2CPP,
                buildAppBundle = EditorUserBuildSettings.buildAppBundle,
                androidBuildSystem = EditorUserBuildSettings.androidBuildSystem.ToString()
            });
        }
    }
}
