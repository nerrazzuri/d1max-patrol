// 地图坐标 → 预览图上的像素(站点 map_preview 给的换算:m_per_px、left_x、top_y),再到屏上。
//
// 给客户看的显示(只改显示,站点、狗用的坐标一个不动):
// - 转正:站点给 rotation_deg(−45 ≤ θ < 45),整张图(底图 + 上面所有的东西)在屏上绕图中心顺时针转 θ,
//   跟 CSS rotate(θdeg) 同向。屏上 y 朝下,所以 SVG matrix(a b c d) = k·[cos θ, sin θ, −sin θ, cos θ]。
// - 视图(View):放大倍数 k(屏上一像素 / 预览一像素)+ 图中心在屏上的位置 (cx, cy)。
//   屏上点 = (cx, cy) + k·R(θ)·(预览点 − 图中心)。全图(fitView)= 转过的整张图正好放进框里、居中。
// - 屏上一点 → 地图坐标:交给浏览器(世界层的 getScreenCTM().inverse()),跟画的是同一个 transform。
// - 全图按有内容的范围(地图台账 M7,2026-10-14):站点给 content_bbox(预览像素 [x0, y0, x1, y1]:地面 + 墙 +
//   「看到过」,去掉抹掉的),fitView 把它转过以后的外框放进框里、居中;没给(老站点)按整张图。缩放的上下限、
//   平移的边照旧按整张图算(缩小还能看到整张)。

export type Pt = [number, number];

/** 站点的导航区域(nogo 当障碍、slow 限速、erase 抹掉区:狗不去、站点画图时抹成没建图),地图坐标(米)。 */
export interface NavZone {
  id: string;
  kind: string;
  label: string;
  polygon: Pt[];
  /** 硬禁区(决策 65,水体、落差):遥控也开不进去。 */
  hard?: boolean;
  /** 临时区到期(决策 66,毫秒)。 */
  until_ms?: number;
}

/** 站点给的有名字的区域(地图坐标,米)。 */
export interface NamedArea {
  name: string;
  points: { x: number; y: number }[];
}

export interface PreviewMeta {
  width: number;
  height: number;
  m_per_px: number;
  left_x: number;
  top_y: number;
  /** 屏上顺时针转这么多度(绕图中心);没给 = 0。 */
  rotation_deg?: number;
  /** 有名字的区域;没给 = 老站点。 */
  areas?: NamedArea[];
  /** 平面图(灰度 + 透明,盖住预览的整个范围,scale 倍分辨率);拉到 width×height 画。 */
  plan?: { url: string; scale: number };
  zones?: NavZone[];
  /** 有内容的范围(预览像素 [x0, y0, x1, y1]);没给 = 老站点,全图按整张图。 */
  content_bbox?: number[];
  /** 这一版带「看到过、狗没走过」的淡色层(图例里加一行)。 */
  seen?: boolean;
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

// ------------------------------------------------------------------ 转正、视图

/** 站点给的转角(度);没给、不是有限数 → 0。 */
export function rotationOf(meta: PreviewMeta): number {
  const r = meta.rotation_deg;
  return typeof r === "number" && Number.isFinite(r) ? r : 0;
}

/** 2D 仿射变换,跟 SVG 的 matrix(a b c d e f) 一样:x' = a·x + c·y + e,y' = b·x + d·y + f。 */
export interface Mat { a: number; b: number; c: number; d: number; e: number; f: number }

/** 放大倍数 k(屏上像素 / 预览像素)+ 图中心在屏上的位置。 */
export interface View { k: number; cx: number; cy: number }

export interface Box { w: number; h: number }

/** 预览像素 → 屏上(svg 的用户单位 = CSS 像素)。 */
export function worldMatrix(meta: PreviewMeta, v: View): Mat {
  const t = (rotationOf(meta) * Math.PI) / 180;
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  const a = v.k * cos;
  const b = v.k * sin;
  const c = -v.k * sin;
  const d = v.k * cos;
  const mx = meta.width / 2;
  const my = meta.height / 2;
  // 转角 0 时 b、c 是 −0:写成 0,属性里不出 "-0"
  return { a, b: b || 0, c: c || 0, d, e: v.cx - (a * mx + c * my), f: v.cy - (b * mx + d * my) };
}

export function applyMat(m: Mat, x: number, y: number): Pt {
  return [m.a * x + m.c * y + m.e, m.b * x + m.d * y + m.f];
}

export function invertMat(m: Mat): Mat {
  const det = m.a * m.d - m.b * m.c;
  const a = m.d / det;
  const b = -m.b / det;
  const c = -m.c / det;
  const d = m.a / det;
  return { a, b, c, d, e: -(a * m.e + c * m.f), f: -(b * m.e + d * m.f) };
}

export function matrixAttr(m: Mat): string {
  return `matrix(${m.a} ${m.b} ${m.c} ${m.d} ${m.e} ${m.f})`;
}

/** 转过以后整张图的外框(预览像素)。 */
export function rotatedSize(meta: PreviewMeta): Box {
  const t = (rotationOf(meta) * Math.PI) / 180;
  const cos = Math.abs(Math.cos(t));
  const sin = Math.abs(Math.sin(t));
  return { w: meta.width * cos + meta.height * sin, h: meta.width * sin + meta.height * cos };
}

/** 站点给的有内容的范围:四个有限数、在图里(夹到图上)、有大小;不合用回 null(按整张图)。 */
export function contentBox(meta: PreviewMeta): [number, number, number, number] | null {
  const b = meta.content_bbox;
  if (!Array.isArray(b) || b.length !== 4 || !b.every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const x0 = Math.max(0, Math.min(b[0], b[2]));
  const y0 = Math.max(0, Math.min(b[1], b[3]));
  const x1 = Math.min(meta.width, Math.max(b[0], b[2]));
  const y1 = Math.min(meta.height, Math.max(b[1], b[3]));
  return x1 - x0 > 0 && y1 - y0 > 0 ? [x0, y0, x1, y1] : null;
}

/** 整张图:转过的整张图放进框里(四边留 pad)、居中。缩放的上下限按它算。 */
export function fitWhole(meta: PreviewMeta, box: Box, pad = 16): View {
  const r = rotatedSize(meta);
  const k = Math.max(1e-6, Math.min((box.w - 2 * pad) / r.w, (box.h - 2 * pad) / r.h));
  return { k, cx: box.w / 2, cy: box.h / 2 };
}

/** 全图(刚打开、点「全图」):有内容的范围(四个角绕图中心转过以后的外框)放进框里(四边留 pad)、居中;
 *  站点没给范围就是整张图(fitWhole)。 */
export function fitView(meta: PreviewMeta, box: Box, pad = 16): View {
  const b = contentBox(meta);
  if (!b) return fitWhole(meta, box, pad);
  const t = (rotationOf(meta) * Math.PI) / 180;
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  const mx = meta.width / 2;
  const my = meta.height / 2;
  // 四个角相对图中心转过以后(屏上方向,k = 1)
  const pts = [[b[0], b[1]], [b[2], b[1]], [b[0], b[3]], [b[2], b[3]]].map(([x, y]) => [
    cos * (x - mx) - sin * (y - my), sin * (x - mx) + cos * (y - my)]);
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const w = Math.max(...xs) - Math.min(...xs);
  const h = Math.max(...ys) - Math.min(...ys);
  const k = Math.max(1e-6, Math.min((box.w - 2 * pad) / w, (box.h - 2 * pad) / h));
  const ux = (Math.max(...xs) + Math.min(...xs)) / 2;
  const uy = (Math.max(...ys) + Math.min(...ys)) / 2;
  return { k, cx: box.w / 2 - k * ux, cy: box.h / 2 - k * uy };
}

/** 以屏上 (qx, qy) 为定点放大 f 倍(那一点下面的地图不动)。 */
export function zoomView(v: View, f: number, qx: number, qy: number): View {
  return { k: v.k * f, cx: qx + (v.cx - qx) * f, cy: qy + (v.cy - qy) * f };
}

/** 平移、缩放的边:最小是全图的一半,最大全图的 16 倍;图中心不出框外(留得住一块图在屏上)。 */
export function clampView(meta: PreviewMeta, box: Box, v: View): View {
  const fit = fitWhole(meta, box);
  const k = Math.min(fit.k * 16, Math.max(fit.k * 0.5, v.k));
  const r = rotatedSize(meta);
  const hw = (r.w * k) / 2;
  const hh = (r.h * k) / 2;
  const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));
  return { k, cx: clamp(v.cx, -hw + 48, box.w + hw - 48), cy: clamp(v.cy, -hh + 48, box.h + hh - 48) };
}

// ------------------------------------------------------------------ 比例尺

const NICE = [1, 2, 5];

/** 比例尺:不超过 maxPx 的最长的 1-2-5 米数;回米数和屏上的长度。转角不影响(等比缩放)。 */
export function scaleBar(mPerPx: number, k: number, maxPx = 120): { m: number; px: number } {
  const pxPerM = k / mPerPx;
  let best = { m: 0.1, px: 0.1 * pxPerM };
  for (let e = -1; e <= 3; e++) {
    for (const n of NICE) {
      const m = Number((n * 10 ** e).toPrecision(3));
      if (m * pxPerM <= maxPx) best = { m, px: m * pxPerM };
    }
  }
  return best;
}

// ------------------------------------------------------------------ 区域名

export function pointInPolygon([x, y]: Pt, pts: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i];
    const [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function edgeDistance([x, y]: Pt, pts: Pt[]): number {
  let best = Infinity;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [ax, ay] = pts[j];
    const [bx, by] = pts[i];
    const dx = bx - ax;
    const dy = by - ay;
    const l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / l2)) : 0;
    best = Math.min(best, Math.hypot(x - (ax + t * dx), y - (ay + t * dy)));
  }
  return best;
}

/** 里面离边最远的那一点(视觉中心,凹的多边形也落在里面):网格找、在最好的那一格附近再细找几轮。 */
export function visualCentre(pts: Pt[]): Pt {
  if (pts.length === 0) return [0, 0];
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  let x0 = Math.min(...xs);
  let x1 = Math.max(...xs);
  let y0 = Math.min(...ys);
  let y1 = Math.max(...ys);
  let best: Pt = [xs.reduce((s, v) => s + v, 0) / xs.length, ys.reduce((s, v) => s + v, 0) / ys.length];
  let bestD = pointInPolygon(best, pts) ? edgeDistance(best, pts) : -1;
  const N = 16;
  for (let round = 0; round < 4; round++) {
    const sx = (x1 - x0) / N;
    const sy = (y1 - y0) / N;
    for (let i = 0; i <= N; i++) {
      for (let j = 0; j <= N; j++) {
        const p: Pt = [x0 + i * sx, y0 + j * sy];
        if (!pointInPolygon(p, pts)) continue;
        const d = edgeDistance(p, pts);
        if (d > bestD) {
          bestD = d;
          best = p;
        }
      }
    }
    // 下一轮在最好的那一点周围两格里细找
    x0 = best[0] - 2 * sx;
    x1 = best[0] + 2 * sx;
    y0 = best[1] - 2 * sy;
    y1 = best[1] + 2 * sy;
  }
  return best;
}

/** 估一行字的宽(像素):中日韩全角约 1 个字号,其它约 0.6。 */
export function textWidth(text: string, fontPx: number): number {
  let w = 0;
  for (const ch of text) w += (ch.codePointAt(0) ?? 0) >= 0x2e80 ? 1 : 0.6;
  return w * fontPx;
}

/** 屏上的多边形放得下这个名字吗(视觉中心那一点的内切圆够一行字高、外框够字宽)。 */
export function labelFits(screenPts: Pt[], text: string, fontPx: number): boolean {
  if (screenPts.length < 3) return false;
  const c = visualCentre(screenPts);
  if (!pointInPolygon(c, screenPts) || edgeDistance(c, screenPts) < fontPx * 0.7) return false;
  const xs = screenPts.map((p) => p[0]);
  return Math.max(...xs) - Math.min(...xs) >= textWidth(text, fontPx) + 4;
}

/** 屏上要让开的东西(狗、报警点):圆心 + 半径(像素)。 */
export interface Obstacle { x: number; y: number; r: number }

/**
 * 区域名放哪儿(屏上):视觉中心;压在狗、报警点上就挪到那个东西的正上方或正下方(还要在多边形里、离边够远);
 * 都不行就还放中心(名字在狗、报警点底下,不藏)。放不下字回 null。
 */
export function placeLabel(screenPts: Pt[], text: string, fontPx: number, avoid: Obstacle[]): Pt | null {
  if (!labelFits(screenPts, text, fontPx)) return null;
  const c = visualCentre(screenPts);
  const hw = textWidth(text, fontPx) / 2;
  const hh = fontPx / 2;
  // 字的外框(以 p 为中心)跟圆 a 有没有交
  const overlaps = (a: Obstacle, [x, y]: Pt) => {
    const dx = Math.max(Math.abs(a.x - x) - hw, 0);
    const dy = Math.max(Math.abs(a.y - y) - hh, 0);
    return dx * dx + dy * dy < a.r * a.r;
  };
  const hits = (p: Pt) => avoid.some((a) => overlaps(a, p));
  const hit = avoid.find((a) => overlaps(a, c));
  if (!hit) return c;
  for (const p of [[c[0], hit.y + hit.r + hh + 2], [c[0], hit.y - hit.r - hh - 2]] as Pt[]) {
    if (pointInPolygon(p, screenPts) && edgeDistance(p, screenPts) >= hh && !hits(p)) return p;
  }
  return c;
}

/** 站点给的区域:去掉没名字的、点不够的、坐标不是数的。 */
export function namedAreas(meta: PreviewMeta): { name: string; pts: Pt[] }[] {
  if (!Array.isArray(meta.areas)) return [];
  return meta.areas
    .filter((a) => a && typeof a.name === "string" && a.name.trim() && Array.isArray(a.points))
    .map((a) => ({ name: a.name.trim(), pts: a.points.map((p) => [Number(p?.x), Number(p?.y)] as Pt) }))
    .filter((a) => a.pts.length >= 3 && a.pts.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y)));
}
