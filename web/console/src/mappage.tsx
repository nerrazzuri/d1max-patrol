// 地图页签(B0 专业版 2.4):地图铺满 + 右侧栏。正常的狗中性灰白圆 + 朝向;异常的用形状 + 颜色;P1 位置是
// 菱形 + 报警圈(未确认时闪)。侧栏只给**选中**的狗拉前后两路小画面(有 P1 时默认选 P1 的狗)。
import { useEffect, useState } from "preact/hooks";
import { api, enc } from "./api";
import { CameraWell } from "./camera";
import { t } from "./i18n";
import { StateText, type UnitInfo } from "./live";
import { heading, onMap, toPx, type PreviewMeta } from "./mapview";
import { abnormal, alarmState, robotMap, type Alert } from "./model";

export function MapPage(props: { units: UnitInfo[]; p1: Alert[]; now: number; selected: string | null; onSelect: (id: string) => void; onVideo: (id: string) => void }) {
  const which = props.units.map((u) => robotMap(u.robot)).find((m) => m) ?? null;
  const [meta, setMeta] = useState<PreviewMeta | null>(null);
  const [areas, setAreas] = useState<{ zone: string; points: [number, number][] }[]>([]);
  const key = which ? `${which.map_id}/${which.version}` : "";
  useEffect(() => {
    setMeta(null);
    setAreas([]);
    if (!which) return;
    const base = `/api/maps/${enc(which.map_id)}/${enc(which.version)}`;
    api<PreviewMeta>("GET", `${base}/preview`).then(setMeta).catch(() => setMeta(null));
    api<{ areas: { zone: string; points: [number, number][] }[] }>("GET", `${base}/areas`).then((d) => setAreas(d.areas)).catch(() => setAreas([]));
  }, [key]);
  const sel = props.units.find((u) => u.robot.robot_id === props.selected) ?? props.units[0];

  return (
    <div class="mappage">
      <div class="mapbox">
        {!which || !meta ? (
          <p class="empty">{t("noMap")}</p>
        ) : (
          <svg class="map" viewBox={`0 0 ${meta.width} ${meta.height}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label={t("mapAria")}>
            <image href={`/api/maps/${enc(which.map_id)}/${enc(which.version)}/preview.png`} x="0" y="0" width={meta.width} height={meta.height} class="mapimg" />
            {areas.map((a) => {
              const pts = a.points.map(([x, y]) => toPx(meta, x, y));
              const cx = pts.reduce((s, p) => s + p.px, 0) / pts.length;
              const cy = pts.reduce((s, p) => s + p.py, 0) / pts.length;
              return (
                <g key={a.zone}>
                  <polygon points={pts.map((p) => `${p.px},${p.py}`).join(" ")} class="sec-area quiet" />
                  <text x={cx} y={cy} class="area-label quiet" font-size={Math.max(10, meta.width / 70)}>{a.zone}</text>
                </g>
              );
            })}
            {props.p1.map((a) => {
              const p = a.context?.pose;
              if (!p || p.map_id !== which.map_id || p.map_version !== which.version) return null;
              const { px, py } = toPx(meta, p.x, p.y);
              if (!onMap(meta, px, py)) return null;
              const r = meta.width / 35;
              return (
                <g key={a.key} class={alarmState(a, props.now) === "unacked" ? "alarm-pin flash" : "alarm-pin"}>
                  <circle cx={px} cy={py} r={r} class="ring" />
                  <path d={`M${px} ${py - r / 2.5}l${r / 2.5} ${r / 2.5}-${r / 2.5} ${r / 2.5}-${r / 2.5}-${r / 2.5}z`} class="diamond" />
                </g>
              );
            })}
            {props.units.map((u) => {
              const pose = u.robot.loc?.pose;
              if (!pose) return null;
              const { px, py } = toPx(meta, pose.x, pose.y);
              if (!onMap(meta, px, py, 20)) return null;
              const rad = meta.width / 90;
              const { hx, hy } = heading(px, py, pose.yaw, rad * 2.2);
              const a = abnormal(u.state);
              const on = u.robot.robot_id === sel?.robot.robot_id;
              return (
                <g key={u.robot.robot_id} class={`bot ${a ?? "ok"}${on ? " on" : ""}`} onClick={() => props.onSelect(u.robot.robot_id)}>
                  <line x1={px} y1={py} x2={hx} y2={hy} class="bot-head" />
                  <circle cx={px} cy={py} r={rad} class="bot-dot" />
                  <text x={px} y={py + rad * 2.8} class="bot-label" font-size={rad * 1.4}>{u.robot.robot_id}</text>
                </g>
              );
            })}
          </svg>
        )}
        <div class="legend" aria-label="Legend">
          <span>{t("legendRoute")}</span><span class="nogo">{t("legendNoGo")}</span><span>● {t("legendRobot")}</span>
        </div>
      </div>
      <aside class="sidebar" aria-label={t("tabMap")}>
        {sel && (
          <div class="sel">
            <div class="row"><span class="unit-id">{sel.robot.robot_id}</span><StateText s={sel.state} />{sel.battery != null && <span class="batt push">{Math.round(sel.battery)}%</span>}</div>
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
                <span class="unit-id">{u.robot.robot_id}</span><StateText s={u.state} />
                {u.battery != null && <span class="batt push">{Math.round(u.battery)}%</span>}
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
