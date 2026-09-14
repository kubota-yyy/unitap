#if UNITY_EDITOR
using System;
using UnityEngine;
namespace Unitap.Tools {
 public sealed class UiPointerPump : MonoBehaviour {
  public Action Frame;
  void LateUpdate(){Frame?.Invoke();}
 }
}
#endif
