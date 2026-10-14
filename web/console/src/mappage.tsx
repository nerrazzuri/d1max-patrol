// 地图页签(B0 专业版 2.4):地图铺满 + 右侧栏。正常的狗绿点 + 朝向;异常的用形状 + 颜色;P1 位置是
// 菱形 + 报警圈(未确认时闪)。侧栏只给**选中**的狗拉前后两路小画面(有 P1 时默认选 P1 的狗)。
// 地图本身(转正、全图、缩放平移、区域名、图例、比例尺、平面图)在 mapcanvas.tsx;狗和报警点画在屏上坐标里
// (大小不跟着缩放变),位置用跟底图同一个变换算。
import { useEffect, useState } from "preact/hooks";
import { api, enc } from "./api";
import { CameraWell } from "./camera";
import { t } from "./i18n";
import { StateText, type UnitInfo } from "./live";
import { MapCanvas, type CanvasArea, type LegendKey } from "./mapcanvas";
import { heading, namedAreas, onMap, toPx, type PreviewMeta, type Pt } from "./mapview";
import { abnormal, alarmState, robotMap, type Alert } from "./model";

/** 屏上的大小(像素):狗的圆点半径、报警圈半径、狗编号字号。 */
const BOT_R = 7;
const PIN_R = 14;
const LABEL_PX = 12;

/**
 * 地图页上的区域:站点在预览里给的 areas(有名字的区域)在前;防区接口(老站点只有它)里同名的不重画。
 */
export function mapAreas(meta: PreviewMeta, sec: { zone: string; points: Pt[] }[]): CanvasArea[] {
  const named = namedAreas(meta).map((a): CanvasArea => ({ name: a.name, pts: a.pts, kind: "area" }));
  const seen = new Set(named.map((a) => a.name));
  for (const s of sec) {
    if (!s.zone || seen.has(s.zone) || !Array.isArray(s.points) || s.points.length < 3) continue;
    seen.add(s.zone);
    named.push({ name: s.zone, pts: s.points, kind: "area" });
  }
  return named;
}

export function MapPage(props: { units: UnitInfo[]; p1: Alert[]; now: number; selected: string | null; onSelect: (id: string) => void; onVideo: (id: string) => void }) {
  const which = props.units.map((u) => robotMap(u.robot)).find((m) => m) ?? null;
  const [meta, setMeta] = useState<PreviewMeta | null>(null);
  const [areas, setAreas] = useState<{ zone: string; points: Pt[] }[]>([]);
  const key = which ? `${which.map_id}/${which.version}` : "";
  useEffect(() => {
    setMeta(null);
    setAreas([]);
    if (!which) return;
    const base = `/api/maps/${enc(which.map_id)}/${enc(which.version)}`;
    api<PreviewMeta>("GET", `${base}/preview`).then(setMeta).catch(() => setMeta(null));
    api<{ areas: { zone: string; points: Pt[] }[] }>("GET", `${base}/areas`).then((d) => setAreas(d.areas)).catch(() => setAreas([]));
  }, [key]);
  const sel = props.units.find((u) => u.robot.robot_id === props.selected) ?? props.units[0];

  // 图上真画得出来的报警点、狗(图例只列有的)
  const pins = !which || !meta ? [] : props.p1.flatMap((a) => {
    const p = a.context?.pose;
    if (!p || p.map_id !== which.map_id || p.map_version !== which.version) return [];
    const { px, py } = toPx(meta, p.x, p.y);
    return onMap(meta, px, py) ? [{ a, px, py }] : [];
  });
  const bots = !meta ? [] : props.units.flatMap((u) => {
    const pose = u.robot.loc?.pose;
    if (!pose) return [];
    const { px, py } = toPx(meta, pose.x, pose.y);
    return onMap(meta, px, py, 20) ? [{ u, px, py, yaw: pose.yaw }] : [];
  });
  const legend: LegendKey[] = [...(bots.length ? ["robot" as const] : []), ...(pins.length ? ["alarm" as const] : [])];

  return (
    <div class="mappage">
      <div class="mapbox">
        {!which || !meta ? (
          <p class="empty">{t("noMap")}</p>
        ) : (
          <MapCanvas meta={meta} raster={`/api/maps/${enc(which.map_id)}/${enc(which.version)}/preview.png`} label={t("mapAria")}
            areas={mapAreas(meta, areas)} legend={legend}
            // 区域名让开报警圈、狗(圆点 + 下面的编号)
            avoid={(s) => [
              ...pins.map(({ px, py }) => { const [x, y] = s.px(px, py); return { x, y, r: PIN_R + 2 }; }),
              ...bots.map(({ px, py }) => { const [x, y] = s.px(px, py); return { x, y: y + LABEL_PX / 2, r: BOT_R + LABEL_PX + 4 }; }),
            ]}
            marks={(s) => (
              <>
                {pins.map(({ a, px, py }) => {
                  const [x, y] = s.px(px, py);
                  const h = PIN_R / 2.5;
                  return (
                    <g key={a.key} class={alarmState(a, props.now) === "unacked" ? "alarm-pin flash" : "alarm-pin"}>
                      <circle cx={x} cy={y} r={PIN_R} class="ring" />
                      <path d={`M${x} ${y - h}l${h} ${h}-${h} ${h}-${h}-${h}z`} class="diamond" />
                    </g>
                  );
                })}
                {bots.map(({ u, px, py, yaw }) => {
                  const [x, y] = s.px(px, py);
                  // 朝向:在预览上往 yaw 走一小段,跟着图一起变换,再在屏上取定长
                  const e = heading(px, py, yaw, 10);
                  const [ex, ey] = s.px(e.hx, e.hy);
                  const n = Math.hypot(ex - x, ey - y) || 1;
                  const len = BOT_R * 2.2;
                  const a = abnormal(u.state);
                  const on = u.robot.robot_id === sel?.robot.robot_id;
                  return (
                    <g key={u.robot.robot_id} class={`bot ${a ?? "ok"}${on ? " on" : ""}`} onClick={() => props.onSelect(u.robot.robot_id)}>
                      <line x1={x} y1={y} x2={x + ((ex - x) / n) * len} y2={y + ((ey - y) / n) * len} class="bot-head" />
                      <circle cx={x} cy={y} r={BOT_R} class="bot-dot" />
                      <text x={x} y={y + BOT_R + LABEL_PX + 2} class="bot-label" font-size={LABEL_PX}>{u.robot.robot_id}</text>
                    </g>
                  );
                })}
              </>
            )} />
        )}
      </div>
      <aside class="sidebar" aria-label={t("tabMap")}>
        {sel && (
          <div class="sel">
            <div class="row"><span class="unit-id">{sel.robot.robot_id}</span><StateText s={sel.state} stale={sel.stale} />{sel.battery != null && <span class="batt push">{Math.round(sel.battery)}%</span>}</div>
            {sel.state !== "offline" && (
              <div class="pair small">
                <CameraWell key={`${sel.robot.robot_id}-f`} robot={sel.robot.robot_id} cam="front" now={props.now} />
                <CameraWell key={`${sel.robot.robot_id}-b`} robot={sel.robot.robot_id} cam="back" now={props.now} />
              </div>
            )}
            <span class="muted">{sel.task}</span>
            <button type="button" class="link" onClick={() => props.onVideo(sel.robot.robot_id)}>{t("goToVideo")}</button>
          </div>
        )}
        <ul class="botlist">
          {props.units.filter((u) => u !== sel).map((u) => (
            <li key={u.robot.robot_id}>
              <button type="button" class="botrow" onClick={() => props.onSelect(u.robot.robot_id)}>
                <span class="unit-id">{u.robot.robot_id}</span><StateText s={u.state} stale={u.stale} />
                {u.battery != null && <span class="batt push">{Math.round(u.battery)}%</span>}
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
