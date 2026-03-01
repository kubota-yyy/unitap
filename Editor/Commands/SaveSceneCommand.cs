using UnityEditor.SceneManagement;

namespace Unitap.Commands
{
    public sealed class SaveSceneCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var all = request.Params?["all"]?.ToObject<bool>() ?? false;

            if (all)
            {
                var saved = EditorSceneManager.SaveOpenScenes();
                return new { saved, all = true };
            }

            var scene = EditorSceneManager.GetActiveScene();
            var result = EditorSceneManager.SaveScene(scene);
            return new { saved = result, scenePath = scene.path };
        }
    }
}
