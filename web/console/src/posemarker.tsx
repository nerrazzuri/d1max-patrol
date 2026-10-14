// 地图上「一个点 + 朝向」的标记(2026-10-15 改版:原来是一个圈 + 一根线连着的把手圈,用户嫌像个头连着个身子、
// 不专业)。现在是导航软件那种定位箭头:圆形底座 + 朝向那一侧伸出去的三角尖;**三角尖就是转朝向的把手**,
// 不再另画一个圈。地图页「去这里 / 在这里设位置」和巡检路线的点都用它。
// 都在标记层里按屏上坐标画(不跟着缩放变大小、不跟着地图转),朝向用屏上方向(已经算上了地图的转角)。
import type { ComponentChildren, JSX } from "preact";

export interface PoseMarkerProps {
  /** 屏上圆心。 */
  x: number;
  y: number;
  /** 朝向那一侧的屏上任意一点(只取方向,比如 routes.handleAt 给的 hx/hy)。 */
  hx: number;
  hy: number;
  /** 底座半径(屏上像素)。 */
  r?: number;
  /** 底座中间的字(路线点的序号);没有就画一个实心小圆点。 */
  label?: ComponentChildren;
  /** 选中的那一个:外面加一圈淡光晕。 */
  selected?: boolean;
  /** 外层 <g> 的 class(路线点:route-pt …;去这里:pose-pin)。 */
  class?: string;
  /** 外层 <g> 的其他属性(data-*、事件)。 */
  attrs?: JSX.HTMLAttributes<SVGGElement>;
  /** 给了就把三角尖做成能拖的把手(class route-handle、data-handle 照旧,测试和拖动代码都认它)。 */
  handle?: { data: string | number; onPointerDown: (ev: PointerEvent) => void };
  children?: ComponentChildren;
}

/** 三角尖的三个点:底边压在底座边上(±42°),尖端伸出去 1.15 r(小了看不出朝向)。 */
export function nosePoints(x: number, y: number, a: number, r: number): string {
  const tip = r * 2.15;
  const half = (42 * Math.PI) / 180;
  const p = (ang: number, d: number) => `${(x + Math.cos(ang) * d).toFixed(1)},${(y + Math.sin(ang) * d).toFixed(1)}`;
  return `${p(a - half, r * 0.92)} ${p(a, tip)} ${p(a + half, r * 0.92)}`;
}

export function PoseMarker(p: PoseMarkerProps) {
  const r = p.r ?? 11;
  const a = Math.atan2(p.hy - p.y, p.hx - p.x);
  const nose = <polygon class="pm-nose" points={nosePoints(p.x, p.y, a, r)} />;
  return (
    <g class={`pose-marker${p.selected ? " sel" : ""}${p.class ? ` ${p.class}` : ""}`} {...p.attrs}>
      {p.children}
      {p.selected && <circle class="pm-halo" cx={p.x} cy={p.y} r={r + 7} />}
      {p.handle ? (
        <g class="route-handle" data-handle={p.handle.data} onPointerDown={p.handle.onPointerDown}
          onClick={(ev) => ev.stopPropagation()}>
          {nose}
          {/* 把手的点击区比三角尖大一点,好抓 */}
          <circle class="pm-grab" cx={p.x + Math.cos(a) * r * 1.6} cy={p.y + Math.sin(a) * r * 1.6} r={r * 0.8} />
        </g>
      ) : nose}
      <circle class="pm-body" cx={p.x} cy={p.y} r={r} />
      {p.label != null ? <text class="pm-label" x={p.x} y={p.y}>{p.label}</text>
        : <circle class="pm-dot" cx={p.x} cy={p.y} r={r * 0.38} />}
    </g>
  );
}
