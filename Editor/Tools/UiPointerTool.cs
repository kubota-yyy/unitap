using System;
using System.Collections.Generic;
using UnityEditor;
using System.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.EventSystems;

namespace Unitap.Tools
{
    [McpForUnityTool("ui_pointer", RequiresPolling=true, Description="Raycast the real EventSystem at screen coordinates; inspect hits or dispatch pointer down/up/click to the top hit")]
    [UnitapToolParameter("x", "float", "Screen x in pixels, origin bottom left", Required=false)]
    [UnitapToolParameter("y", "float", "Screen y in pixels, origin bottom left", Required=false)]
    [UnitapToolParameter("action", "string", "inspect, down, up, click or status (click performs down then up)", DefaultValue="inspect")]
    [UnitapToolParameter("limit", "int", "Maximum raycast hits returned", DefaultValue="8")]
    public static class UiPointerTool
    {
        static PointerEventData pressed;
        static JObject queued;
        static object result;
        static UiPointerPump pump;
        public static void Tick(){if(queued==null)return;var request=queued;queued=null;try{result=Evaluate(request);}catch(Exception e){result=new ErrorResponse(e.Message);}}
        [InitializeOnLoadMethod] static void Initialize(){EditorApplication.playModeStateChanged+=_=>{pressed=null;queued=null;result=null;pump=null;};}
        public static object HandleCommand(JObject p)
        {
            if(p.Value<string>("action")=="status")return result??(queued!=null?(object)new PendingResponse("Waiting for a game frame",.05):new ErrorResponse("No pointer request is pending"));
            if(!Application.isPlaying||EditorApplication.isPaused)return new ErrorResponse("Unpaused Play Mode is required");
            if(queued!=null)return new ErrorResponse("Another pointer request is pending");
            if(pump==null){var go=new GameObject("Unitap UI pointer pump"){hideFlags=HideFlags.HideAndDontSave};UnityEngine.Object.DontDestroyOnLoad(go);pump=go.AddComponent<UiPointerPump>();pump.Frame=Tick;}
            result=null;queued=(JObject)p.DeepClone();return new PendingResponse("Queued for the runtime EventSystem",.05);
        }
        static object Evaluate(JObject p)
        {
            if(!Application.isPlaying||EventSystem.current==null)return new ErrorResponse("Play Mode and an active EventSystem are required");
            if(p["x"]==null||p["y"]==null)return new ErrorResponse("x and y are required");
            var action=p.Value<string>("action")??"inspect";
            if(!new[]{"inspect","down","up","click"}.Contains(action))return new ErrorResponse("action must be inspect, down, up or click");
            var position=new Vector2(p.Value<float>("x"),p.Value<float>("y"));
            var data=new PointerEventData(EventSystem.current){position=position,button=PointerEventData.InputButton.Left,pointerId=-101};
            var hits=new List<RaycastResult>();Canvas.ForceUpdateCanvases();EventSystem.current.RaycastAll(data,hits);
            var first=hits.FirstOrDefault();data.pointerCurrentRaycast=first;
            var descriptions=hits.Take(Mathf.Clamp(p.Value<int?>("limit")??8,1,50)).Select(h=>new{path=Path(h.gameObject.transform),module=h.module.GetType().Name,depth=h.depth,sortingOrder=h.sortingOrder}).ToArray();
            bool clicked=false;
            if(action=="down"||action=="click"){
                if(pressed!=null && pressed.pointerPress!=null)return new ErrorResponse("A pointer is already down; send up first");
                data.pressPosition=position;data.pointerPressRaycast=first;data.eligibleForClick=true;
                data.pointerPress=first.gameObject!=null?ExecuteEvents.ExecuteHierarchy(first.gameObject,data,ExecuteEvents.pointerDownHandler):null;
                if(data.pointerPress==null&&first.gameObject!=null)data.pointerPress=ExecuteEvents.GetEventHandler<IPointerClickHandler>(first.gameObject);
                pressed=data;
            }
            if(action=="up"||action=="click"){
                if(pressed==null)return new ErrorResponse("No pointer is down");
                var down=pressed;pressed=null;down.position=position;down.pointerCurrentRaycast=first;
                if(down.pointerPress!=null){
                    ExecuteEvents.Execute(down.pointerPress,down,ExecuteEvents.pointerUpHandler);
                    if(first.gameObject!=null&&down.pointerPress==ExecuteEvents.GetEventHandler<IPointerClickHandler>(first.gameObject))clicked=ExecuteEvents.Execute(down.pointerPress,down,ExecuteEvents.pointerClickHandler);
                }
            }
            return new SuccessResponse("EventSystem pointer evaluated",new{action,clicked,screen=new{Screen.width,Screen.height},hitCount=hits.Count,hits=descriptions});
        }
        static string Path(Transform t)=>t.parent==null?t.name:Path(t.parent)+"/"+t.name;
    }
}
