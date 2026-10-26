// 地图上的运行可视化图层:规划路径(虚线)、最近轨迹(细线)、被挡的位置、定位出过问题的地方(点)。
// 都是叠加,交给 MapCanvas 的 world(预览像素坐标,跟底图一起转、缩放)和 marks(屏上坐标,字和点不跟着缩放)。
// 转正(rotation_deg)靠 MapCanvas 的世界层变换,这里一个角度都不算:只负责把地图坐标换成预览像素
// (mapview.toPx),所以转角、缩放、平移对它们跟对底图一样。
import { toPx, onMap, type PreviewMeta, type Pt } from "./mapview";
import type { Screen } from "./mapcanvas";
import { L, blockedText, kindText, trailSegs, type Episode, type VizData, type VizRobot } from "./runviz";

/** 屏上的大小(像素)。 */
const DOT_R = 4;
const BLOCK_R = 9;

/** 这张图上要画的狗(它们的路径、轨迹是不是这张图的,按图号 + 版本认)。 */
export function vizOnMap(viz: VizData | null, mapId: string, version: string): [string, VizRobot][] {
  if (!viz) return [];
  return Object.entries(viz.robots).filter(([, r]) => r.map?.map_id === mapId && r.map.version === version);
}

function line(meta: PreviewMeta, pts: Pt[]): string {
  return pts.map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px.toFixed(1)},${p.py.toFixed(1)}`; }).join(" ");
}

/** 世界层:轨迹在下、规划路径在上。 */
export function VizWorld({ meta, robots }: { meta: PreviewMeta; robots: [string, VizRobot][] }) {
  return (
    <g class="rv-world">
      {robots.flatMap(([id, r]) => trailSegs(r).map((seg, i) => (
        <polyline key={`t:${id}:${i}`} class="rv-trail" data-robot={id} points={line(meta, seg)} />
      )))}
      {robots.map(([id, r]) => r.path.length >= 2 && (
        <polyline key={`p:${id}`} class="rv-path" data-robot={id} points={line(meta, r.path)} />
      ))}
    </g>
  );
}

/** 标记层:路径终点、被挡的位置、定位问题的点。 */
export function vizMarks(s: Screen, meta: PreviewMeta, robots: [string, VizRobot][], episodes: Episode[]) {
  const dots = episodes.flatMap((e) => {
    if (e.x == null || e.y == null) return [];
    const { px, py } = toPx(meta, e.x, e.y);
    return onMap(meta, px, py) ? [{ e, at: s.px(px, py) }] : [];
  });
  return (
    <g class="rv-marks">
      {dots.map(({ e, at: [x, y] }) => (
        <circle key={`e:${e.id}`} cx={x} cy={y} r={DOT_R} class={`rv-ep ${e.kind}`} data-kind={e.kind}>
          <title>{`${e.robot_id}: ${kindText(e.kind)} · ${e.cause}`}</title>
        </circle>
      ))}
      {robots.map(([id, r]) => {
        const end = r.path.length >= 2 ? r.path[r.path.length - 1] : null;
        if (!end) return null;
        const { px, py } = toPx(meta, end[0], end[1]);
        const [x, y] = s.px(px, py);
        return <path key={`g:${id}`} class="rv-goal" d={`M${x - 5} ${y - 5}L${x + 5} ${y + 5}M${x + 5} ${y - 5}L${x - 5} ${y + 5}`} />;
      })}
      {robots.map(([id, r]) => {
        const b = r.blocked;
        if (!b || b.x == null || b.y == null) return null;
        const { px, py } = toPx(meta, b.x, b.y);
        if (!onMap(meta, px, py, 20)) return null;
        const [x, y] = s.px(px, py);
        const h = BLOCK_R;
        return (
          <g key={`b:${id}`} class="rv-blocked">
            <path d={`M${x} ${y - h}L${x + h} ${y + h * 0.8}L${x - h} ${y + h * 0.8}Z`} />
            <title>{`${id}: ${blockedText(b)}`}</title>
          </g>
        );
      })}
    </g>
  );
}

/** 标记层上让区域名躲开的东西(被挡的位置)。 */
export function vizAvoid(s: Screen, meta: PreviewMeta, robots: [string, VizRobot][]) {
  return robots.flatMap(([, r]) => {
    const b = r.blocked;
    if (!b || b.x == null || b.y == null) return [];
    const { px, py } = toPx(meta, b.x, b.y);
    const [x, y] = s.px(px, py);
    return [{ x, y, r: BLOCK_R + 2 }];
  });
}

export interface VizHas { path: boolean; trail: boolean; episode: boolean; blocked: boolean }

export function vizHas(robots: [string, VizRobot][], episodes: Episode[]): VizHas {
  return {
    path: robots.some(([, r]) => r.path.length >= 2),
    trail: robots.some(([, r]) => trailSegs(r).length > 0),
    episode: episodes.some((e) => e.x != null && e.y != null),
    blocked: robots.some(([, r]) => r.blocked?.x != null),
  };
}

/** 图例(只列图上有的),放在地图下面,不动 MapCanvas 自己的图例。 */
/** 运行图层的图例项(只是 <li>):放进地图那一行图例里,不另占一行。 */
export function RunLegendItems({ has }: { has: VizHas }) {
  return (
    <>
      {has.path && <li data-legend="path"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M1 6H15" class="rv-path" /></svg>{L("Planned path", "规划路径")}</li>}
      {has.trail && <li data-legend="trail"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M1 6H15" class="rv-trail" /></svg>{L("This run's trail", "这一趟走过的")}</li>}
      {has.blocked && <li data-legend="blocked"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M8 1L14 11H2Z" class="rv-blocked-sw" /></svg>{L("Blocked here", "被挡的地方")}</li>}
      {has.episode && <li data-legend="episode"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><circle cx="8" cy="6" r="4" class="rv-ep lost" /></svg>{L("Localization problem (filled: lost, hollow: degraded)", "定位出过问题(实心:丢了,空心:变差)")}</li>}
    </>
  );
}

export function RunLegend({ has }: { has: VizHas }) {
  if (!has.path && !has.trail && !has.episode && !has.blocked) return null;
  return (
    <ul class="maplegend runlegend" aria-label={L("Run overlays", "运行图层")} data-testid="run-legend">
      {has.path && <li data-legend="path"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M1 6H15" class="rv-path" /></svg>{L("Planned path", "规划路径")}</li>}
      {has.trail && <li data-legend="trail"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M1 6H15" class="rv-trail" /></svg>{L("This run's trail", "这一趟走过的")}</li>}
      {has.blocked && <li data-legend="blocked"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><path d="M8 1L14 11H2Z" class="rv-blocked-sw" /></svg>{L("Blocked here", "被挡的地方")}</li>}
      {has.episode && <li data-legend="episode"><svg class="sw" width="16" height="12" viewBox="0 0 16 12" aria-hidden="true"><circle cx="8" cy="6" r="4" class="rv-ep lost" /></svg>{L("Localization problem (filled: lost, hollow: degraded)", "定位出过问题(实心:丢了,空心:变差)")}</li>}
    </ul>
  );
}
