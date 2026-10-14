// 地图上拖点、拖朝向把手(2026-10-14):按下时接管(不让地图跟着平移),之后在 window 上听移动和抬起,
// 手指 / 鼠标跑出了点也跟得上。抬起后浏览器还会发一个 click(落在按下和抬起的共同祖先上,可能是地图本身,
// 会被当成「点了地图」加一个点),所以拖过的要让外面那层吞掉下一个 click(见 clickGuard)。

export interface DragGuard {
  /** 拖过(动了 ≥ 4 像素)之后到下一个 click 之前为真。 */
  swallow: boolean;
}

export function clickGuard(): DragGuard {
  return { swallow: false };
}

/** 外层 div 的 onClickCapture 用:吞掉拖完之后的那一个 click。 */
export function swallowClick(g: DragGuard, e: Event): void {
  if (!g.swallow) return;
  g.swallow = false;
  e.stopPropagation();
  e.preventDefault();
}

export function startDrag(e: PointerEvent, g: DragGuard, onMove: (cx: number, cy: number) => void, onEnd?: (moved: boolean) => void): void {
  if (e.button !== undefined && e.button !== 0) return;
  e.stopPropagation();
  const x0 = e.clientX;
  const y0 = e.clientY;
  let moved = false;
  const move = (ev: PointerEvent) => {
    if (!moved && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 4) return;
    moved = true;
    onMove(ev.clientX, ev.clientY);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    if (moved) {
      g.swallow = true;
      setTimeout(() => { g.swallow = false; }, 0);   // 没有 click 跟着来(跑到窗口外松手)就别留着
    }
    onEnd?.(moved);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}
