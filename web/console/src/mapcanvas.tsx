// 地图(地图页、防区页共用):给客户看的显示 —— 转正、全图、滚轮缩放 / 拖动平移、区域名、图例 + 比例尺、平面图。
// 只改显示,站点、狗用的坐标一个不动。
//
// 两层:
// - 世界层(g.map-world):底图(平面图或栅格)和多边形(禁行 / 限速区、区域、防区、正在画的线)用预览像素坐标画,
//   整层一个 transform = matrix(…)(放大、转正、平移合在一起,见 mapview.worldMatrix),跟着图一起转;
// - 标记层(g.map-marks):字(区域名、狗编号)、狗、报警点画在屏上坐标里(位置用同一个矩阵算),
//   不转、不跟着缩放变大小 —— 字永远是正的。
// 屏上一点 → 预览像素:世界层的 getScreenCTM().inverse()(浏览器按画的那个 transform 倒回去)。
import type { ComponentChildren } from "preact";
import { useLayoutEffect, useRef, useState } from "preact/hooks";
import { t, type Key } from "./i18n";
import {
  applyMat,
  clampView,
  fitView,
  matrixAttr,
  placeLabel,
  rotationOf,
  scaleBar,
  toPx,
  worldMatrix,
  zoomView,
  type Box,
  type NavZone,
  type Obstacle,
  type PreviewMeta,
  type Pt,
  type View,
} from "./mapview";
import { rasterShade, scheme } from "./theme";

/** 图上画的有名字的多边形(地图坐标,米):area = 区域(淡),zone = 防区(防区页上能改的那些,显眼)。 */
export interface CanvasArea { name: string; pts: Pt[]; kind: "area" | "zone" }

export type LegendKey = "floor" | "wall" | "seen" | "unknown" | "nogo" | "hard" | "vwall" | "erase" | "slow" | "area" | "zone" | "robot" | "alarm";
const LEGEND_ORDER: LegendKey[] = ["floor", "wall", "seen", "unknown", "nogo", "hard", "vwall", "erase", "slow", "area", "zone", "robot", "alarm"];
const LEGEND_TEXT: Record<LegendKey, Key> = {
  floor: "legendFloor", wall: "legendWall", seen: "legendSeen", unknown: "legendUnknown", nogo: "legendNoGo",
  hard: "hardNoGoLegend", vwall: "legendVWall", erase: "legendErase", slow: "legendSlow",
  area: "legendArea", zone: "legendZone", robot: "legendRobot", alarm: "legendAlarm",
};

/** 底图的灰度(站点 map_preview:空地 254、墙 0、看到过 232、未知 205;平面图:地面白、墙深灰、看到过是
 *  透明度 0.35 的白、未知透明)。 */
const SHADE = { raster: { floor: 254, wall: 0, seen: 232, unknown: 205 }, plan: { floor: 255, wall: 34, seenAlpha: 0.35 } };

/** 标记层用:地图坐标 / 预览像素 → 屏上。 */
export interface Screen {
  map(x: number, y: number): Pt;
  px(px: number, py: number): Pt;
  k: number;
}

/** 量不到大小(测试环境)时先用这个。 */
const FALLBACK: Box = { w: 960, h: 600 };

function useBox(ref: { current: HTMLElement | null }): Box {
  const [box, setBox] = useState<Box>(FALLBACK);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) setBox((b) => (Math.abs(b.w - r.width) < 0.5 && Math.abs(b.h - r.height) < 0.5 ? b : { w: r.width, h: r.height }));
    };
    read();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", read);
      return () => window.removeEventListener("resize", read);
    }
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return box;
}

function polyAttr(meta: PreviewMeta, pts: Pt[]): string {
  return pts.map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px},${p.py}`; }).join(" ");
}

/** 客户端坐标 → 某个 SVG 元素的用户坐标(浏览器给的 CTM 倒回去)。 */
function fromClient(el: SVGGraphicsElement | null, x: number, y: number): Pt | null {
  const m = el?.getScreenCTM();
  if (!m) return null;
  const i = m.inverse();
  return [i.a * x + i.c * y + i.e, i.b * x + i.d * y + i.f];
}

export function MapCanvas(props: {
  meta: PreviewMeta;
  /** 栅格预览(preview.png)的地址;站点给了平面图就画平面图,读不出来再退回它。 */
  raster: string;
  label: string;
  areas?: CanvasArea[];
  /** 页面自己在标记层画了的、要进图例的(狗、报警点)。 */
  legend?: LegendKey[];
  /** 世界层里多画的(预览像素坐标)。 */
  world?: ComponentChildren;
  /** 标记层里多画的(屏上坐标)。 */
  marks?: (s: Screen) => ComponentChildren;
  /** 标记层里区域名要让开的东西(狗、报警点),屏上坐标。 */
  avoid?: (s: Screen) => Obstacle[];
  drawing?: boolean;
  /** 点了图(不是拖):点的那一处的预览像素。 */
  onPick?: (px: number, py: number) => void;
  /** 浮在地图上的东西(比如「去这里」小卡片),放在图框里、不占图下面的高。 */
  overlay?: ComponentChildren;
  /** 图例那一行里多加的项(页面自己画的图层,比如规划路径、轨迹),跟底图图例排一行。 */
  legendExtra?: ComponentChildren;
}) {
  const { meta } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const world = useRef<SVGGElement>(null);
  const box = useBox(wrap);
  const [planBad, setPlanBad] = useState<string | null>(null);
  const plan = meta.plan && typeof meta.plan.url === "string" && meta.plan.url && planBad !== meta.plan.url ? meta.plan : null;
  const rot = rotationOf(meta);
  // 换了图(或者转角、大小变了)就回到全图;没动过也一直是全图(框变大小跟着重新放)
  const key = `${props.raster}|${rot}|${meta.width}x${meta.height}`;
  const [user, setUser] = useState<{ key: string; v: View } | null>(null);
  const view = user && user.key === key ? clampView(meta, box, user.v) : fitView(meta, box);
  const live = useRef(view);
  live.current = view;
  const M = worldMatrix(meta, view);
  const setView = (v: View) => {
    const c = clampView(meta, box, v);
    live.current = c;
    setUser({ key, v: c });
  };

  const drag = useRef<{ id: number; x: number; y: number; v: View; moved: boolean } | null>(null);
  const dragged = useRef(false);
  const [dragging, setDragging] = useState(false);

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * box.h : e.deltaY;
    if (!Number.isFinite(dy) || dy === 0) return;
    // 以指针下面那一点为定点;事件里没有指针位置就以框的中心
    const q = Number.isFinite(e.clientX) && Number.isFinite(e.clientY) ? fromClient(svg.current, e.clientX, e.clientY) : null;
    const [qx, qy] = q ?? [box.w / 2, box.h / 2];
    setView(zoomView(live.current, Math.exp(-Math.max(-600, Math.min(600, dy)) * 0.0015), qx, qy));
  }
  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return;
    dragged.current = false;
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, v: live.current, moved: false };
  }
  function onPointerMove(e: PointerEvent) {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    if (!d.moved) {
      if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 5) return;
      d.moved = true;
      setDragging(true);
      try { svg.current?.setPointerCapture(e.pointerId); } catch { /* 拿不到也能拖 */ }
    }
    const a = fromClient(svg.current, d.x, d.y);
    const b = fromClient(svg.current, e.clientX, e.clientY);
    if (a && b) setView({ k: d.v.k, cx: d.v.cx + b[0] - a[0], cy: d.v.cy + b[1] - a[1] });
  }
  function onPointerEnd(e: PointerEvent) {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    // 拖过:接下来那一下 click 不算点(不选狗、不加角)
    if (d.moved && e.type === "pointerup") dragged.current = true;
    drag.current = null;
    setDragging(false);
  }
  function onClickCapture(e: MouseEvent) {
    if (!dragged.current) return;
    dragged.current = false;
    e.stopPropagation();
    e.preventDefault();
  }
  function onClick(e: MouseEvent) {
    if (!props.onPick) return;
    const p = fromClient(world.current, e.clientX, e.clientY);
    if (p) props.onPick(p[0], p[1]);
  }

  const screen: Screen = {
    map: (x, y) => { const p = toPx(meta, x, y); return applyMat(M, p.px, p.py); },
    px: (px, py) => applyMat(M, px, py),
    k: view.k,
  };
  const obstacles = props.avoid?.(screen) ?? [];
  const zones = (meta.zones ?? []).filter((z) => Array.isArray(z.polygon) && z.polygon.length >= 3);
  const areas = props.areas ?? [];
  const present = new Set<LegendKey>(["floor", "wall", "unknown", ...(props.legend ?? [])]);
  if (zones.some((z) => z.kind === "nogo" && !z.hard && !z.wall)) present.add("nogo");
  if (zones.some((z) => z.kind === "nogo" && z.hard && !z.wall)) present.add("hard");
  if (zones.some((z) => !!z.wall)) present.add("vwall");
  if (zones.some((z) => z.kind === "slow")) present.add("slow");
  // 地图台账 M9、44:看到过、狗没走过(站点说这一版有才列);抹掉区(站点已经把那一块画成没建图,这里只描边)
  if (meta.seen === true) present.add("seen");
  if (zones.some((z) => z.kind === "erase")) present.add("erase");
  for (const a of areas) present.add(a.kind);
  const bar = scaleBar(meta.m_per_px, view.k);
  const cls = ["map", props.drawing ? "drawing" : "", dragging ? "dragging" : ""].filter(Boolean).join(" ");

  return (
    <div class="mapcanvas">
      <div class="mapwrap" ref={wrap}>
        <svg ref={svg} class={cls} viewBox={`0 0 ${box.w} ${box.h}`} role="img" aria-label={props.label}
          onWheel={onWheel} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd} onClickCapture={onClickCapture} onClick={onClick}>
          <g class="map-world" ref={world} transform={matrixAttr(M)}>
            <image key={plan ? plan.url : props.raster} href={plan ? plan.url : props.raster} x="0" y="0" width={meta.width} height={meta.height}
              preserveAspectRatio="none" class={plan ? "mapimg plan" : "mapimg"}
              onError={plan ? () => setPlanBad(plan.url) : undefined} />
            {/* 虚拟墙(2026-10-15)画成一条粗线(画的那条),不画站点算的那条窄带子 */}
            {zones.map((z) => (z.kind === "nogo" || z.kind === "slow" || z.kind === "erase") && !z.wall && (
              <polygon key={z.id} points={polyAttr(meta, z.polygon)}
                class={z.kind === "nogo" ? (z.hard ? "nogo-area hard" : "nogo-area") : z.kind === "erase" ? "erase-area" : "slow-area"}>
                {(z.kind === "nogo" || z.kind === "erase") && <title>{navZoneTip(z)}</title>}
              </polygon>
            ))}
            {zones.filter((z) => Array.isArray(z.wall) && z.wall.length >= 2).map((z) => (
              <polyline key={`w:${z.id}`} points={polyAttr(meta, z.wall!)} class={z.hard ? "vwall-line hard" : "vwall-line"} data-vwall={z.id}>
                <title>{navZoneTip(z)}</title>
              </polyline>
            ))}
            {areas.map((a, i) => (
              <polygon key={`${i}:${a.kind}:${a.name}`} points={polyAttr(meta, a.pts)} class={a.kind === "zone" ? "sec-area" : "sec-area quiet"} />
            ))}
            {props.world}
          </g>
          <g class="map-marks">
            {areas.map((a, i) => {
              const font = a.kind === "zone" ? 14 : 13;
              const at = placeLabel(a.pts.map(([x, y]) => screen.map(x, y)), a.name, font, obstacles);
              if (!at) return null;
              const [x, y] = at;
              return (
                <text key={`${i}:${a.kind}:${a.name}`} x={x} y={y} class={a.kind === "zone" ? "area-label" : "area-label quiet"}
                  font-size={font} dominant-baseline="central">{a.name}</text>
              );
            })}
            {props.marks?.(screen)}
          </g>
        </svg>
        {props.overlay}
      </div>
      <div class="mapbar">
        <ul class="maplegend" aria-label={t("legend")}>
          {LEGEND_ORDER.filter((k) => present.has(k)).map((k) => (
            <li key={k} data-legend={k}><Swatch k={k} plan={!!plan} />{t(LEGEND_TEXT[k])}</li>
          ))}
          {props.legendExtra}
        </ul>
        <div class="mapctl">
          <span class="scale" role="img" aria-label={t("scaleAria", { m: bar.m })}>
            <svg class="scalebar" width={bar.px + 2} height="10" data-px={bar.px} aria-hidden="true"><path d={`M1 1V9H${bar.px + 1}V1`} /></svg>
            <span class="scale-label" aria-hidden="true">{t("scaleM", { m: bar.m })}</span>
          </span>
          <button type="button" class="btn sm" title={t("wholeMapHint")} onClick={() => setUser(null)}>{t("wholeMap")}</button>
        </div>
      </div>
    </div>
  );
}

/** 悬停在禁行区、抹掉区上看到的那句话:名字、是不是硬禁区(决策 65)、临时区什么时候到期(决策 66)。 */
export function navZoneTip(z: NavZone): string {
  const name = z.label || z.id;
  const base = z.wall
    ? (z.hard ? t("hardVWallTip", { label: name }) : t("vwallTip", { label: name }))
    : z.kind === "erase"
      ? (z.hard ? t("hardEraseTip", { label: name }) : t("eraseTip", { label: name }))
      : z.hard ? t("hardNoGoTip", { label: name }) : t("noGoTip", { label: name });
  if (typeof z.until_ms !== "number") return base;
  const d = new Date(z.until_ms);
  const two = (v: number) => String(v).padStart(2, "0");
  return base + t("zoneUntil", { t: `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}` });
}

/** 图例里的一小块,跟图上画的一样(底图的灰度按现在的主题算,跟 --map-raster 一样)。 */
function Swatch({ k, plan }: { k: LegendKey; plan: boolean }) {
  const s = scheme.value;
  const shade = (g: number) => rasterShade(g, s);
  let body: ComponentChildren;
  if (k === "floor") body = <rect x="0.5" y="0.5" width="15" height="11" fill={shade(plan ? SHADE.plan.floor : SHADE.raster.floor)} class="sw-edge" />;
  else if (k === "wall") body = <rect x="0.5" y="0.5" width="15" height="11" fill={shade(plan ? SHADE.plan.wall : SHADE.raster.wall)} class="sw-edge" />;
  else if (k === "seen") body = plan
    ? <><rect x="0.5" y="0.5" width="15" height="11" class="sw-bg sw-edge" /><rect x="0.5" y="0.5" width="15" height="11" fill={shade(SHADE.plan.floor)} fill-opacity={SHADE.plan.seenAlpha} /></>
    : <rect x="0.5" y="0.5" width="15" height="11" fill={shade(SHADE.raster.seen)} class="sw-edge" />;
  else if (k === "erase") body = <rect x="1" y="1" width="14" height="10" class="erase-area" />;
  else if (k === "unknown") body = plan
    ? <rect x="0.5" y="0.5" width="15" height="11" class="sw-bg sw-edge" />
    : <rect x="0.5" y="0.5" width="15" height="11" fill={shade(SHADE.raster.unknown)} class="sw-edge" />;
  else if (k === "nogo") body = <rect x="1" y="1" width="14" height="10" class="nogo-area" />;
  else if (k === "hard") body = <rect x="1.5" y="1.5" width="13" height="9" class="nogo-area hard" />;
  else if (k === "slow") body = <rect x="1" y="1" width="14" height="10" class="slow-area" />;
  else if (k === "vwall") body = <path d="M1 9L7 3L15 3" class="vwall-line" />;
  else if (k === "area") body = <rect x="1" y="1" width="14" height="10" class="sec-area quiet" />;
  else if (k === "zone") body = <rect x="1" y="1" width="14" height="10" class="sec-area" />;
  else if (k === "robot") body = <circle cx="8" cy="6" r="5" class="sw-robot" />;
  else body = <path d="M8 0.5l5.5 5.5-5.5 5.5-5.5-5.5z" class="sw-alarm" />;
  return <svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true">{body}</svg>;
}
