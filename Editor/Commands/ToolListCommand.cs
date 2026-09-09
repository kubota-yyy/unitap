using System.Linq;

namespace Unitap.Commands
{
    public sealed class ToolListCommand : IUnitapCommand
    {
        public object Execute(UnitapRequest request)
        {
            var tools = UnitapToolRegistry.DiscoverTools().Select(entry => entry.Describe()).ToList();
            return new { tools, count = tools.Count };
        }
    }
}
