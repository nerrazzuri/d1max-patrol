// 地图坐标 → 预览图上的像素(站点 map_preview 给的换算:m_per_px、left_x、top_y)。

export interface PreviewMeta {
  width: number;
  height: number;
  m_per_px: number;
  left_x: number;
  top_y: number;
}

export function toPx(meta: PreviewMeta, x: number, y: number): { px: number; py: number } {
  return { px: (x - meta.left_x) / meta.m_per_px, py: (meta.top_y - y) / meta.m_per_px };
}

/** 在图上(留一点边)才画。 */
export function onMap(meta: PreviewMeta, px: number, py: number, margin = 0): boolean {
  return px >= -margin && py >= -margin && px <= meta.width + margin && py <= meta.height + margin;
}

/** 朝向短线的终点(yaw 是地图系,逆时针;图上 y 朝下)。 */
export function heading(px: number, py: number, yaw: number, len: number): { hx: number; hy: number } {
  return { hx: px + Math.cos(yaw) * len, hy: py - Math.sin(yaw) * len };
}
