// W73 第五期 外部设备（门磁、报警主机）按位置登记：站点按位置算查看点、所在防区；每台一个专属回调（自己的密钥）。
// 摄像头不在这里：在「布置 → 摄像头」放到图上就行（ONVIF 订阅，站点按它的位置、朝向算查看点）。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import { lang, type Lang } from "./i18n";

export interface Device { id: string; kind: string; map_id: string; map_version: string; x: number; y: number; yaw: number;
  view_x: number; view_y: number; zone: string; note: string; source: string }

const en = {
  dTitle: "Alarm devices by position",
  dLead: "Door contacts and alarm panels: put them on the map; the site works out where a robot should go and which zone it is in. Each device gets its own signed callback. Cameras: place them under Layout → Cameras.",
  dNone: "No devices yet.",
  dId: "Device ID", dKind: "Type", dMap: "Map", dVer: "Version", dX: "x (m)", dY: "y (m)",
  kContact: "Door contact", kAlarm: "Alarm panel", kOther: "Other",
  dAdd: "Add / update", dRemove: "Delete", dRotate: "New key",
  dView: "robot goes to ({x}, {y})", dZone: "zone {z}", dNoZone: "not in any zone (site-wide)",
  dSecret: "Callback for {id}: header X-Nexwar-Source: {src}, key (shown once): {key}",
};
type DKey = keyof typeof en;
const zh: Record<DKey, string> = {
  dTitle: "外部设备（按位置）",
  dLead: "门磁、报警主机：放到图上，站点自己算机器人去哪看、属于哪个防区；每台一个专属签名回调。摄像头在「布置 → 摄像头」放到图上就行。",
  dNone: "还没有登记设备。",
  dId: "设备编号", dKind: "类型", dMap: "地图", dVer: "版本", dX: "x（米）", dY: "y（米）",
  kContact: "门磁", kAlarm: "报警主机", kOther: "其他",
  dAdd: "登记 / 更新", dRemove: "删除", dRotate: "换密钥",
  dView: "机器人去 ({x}, {y})", dZone: "防区 {z}", dNoZone: "不在任何防区（按全站）",
  dSecret: "{id} 的回调：请求头 X-Nexwar-Source: {src}，密钥（只显示这一次）：{key}",
};
const DICTS: Record<Lang, Record<DKey, string>> = { en, zh };
function dt(k: DKey, v?: Record<string, string | number>): string {
  let s = (DICTS[lang.value] ?? zh)[k];
  for (const [a, b] of Object.entries(v ?? {})) s = s.split(`{${a}}`).join(String(b));
  return s;
}

export function DevicesPanel({ canEdit, say }: { canEdit: boolean; say: (s: string) => void }) {
  const [list, setList] = useState<Device[] | null>(null);
  const [f, setF] = useState({ id: "", kind: "contact", map_id: "", map_version: "", x: "", y: "" });
  const [secret, setSecret] = useState<string>("");
  const load = () => api<{ devices: Device[] }>("GET", "/api/devices").then((d) => setList(d.devices)).catch(() => setList(null));
  useEffect(() => { void load(); }, []);
  if (list === null) return null;                        // 站点没开这一项
  const err = (e: unknown) => say(e instanceof ApiError ? e.message : String(e));
  async function add() {
    try {
      const r = await api<{ secret: string | null; source: string }>("POST", "/api/devices",
        { id: f.id, kind: f.kind, map_id: f.map_id, map_version: f.map_version, x: Number(f.x), y: Number(f.y) });
      setSecret(r.secret ? dt("dSecret", { id: f.id, src: r.source, key: r.secret }) : "");
      await load();
    } catch (e) { err(e); }
  }
  async function act(id: string, what: "remove" | "rotate") {
    try {
      const r = await api<{ secret?: string; source?: string }>("POST", "/api/devices", { id, [what]: true });
      setSecret(r.secret ? dt("dSecret", { id, src: r.source ?? `dev-${id}`, key: r.secret }) : "");
      await load();
    } catch (e) { err(e); }
  }
  const kind = (k: string) => (k === "contact" ? dt("kContact") : k === "alarm" ? dt("kAlarm") : dt("kOther"));
  return (
    <section class="card devices-panel">
      <h2>{dt("dTitle")}</h2>
      <p class="muted small">{dt("dLead")}</p>
      {list.length === 0 ? <p class="muted">{dt("dNone")}</p> : (
        <ul class="plain">
          {list.map((d) => (
            <li key={d.id} class="device-row">
              <b>{d.id}</b> · {kind(d.kind)} · {dt("dView", { x: d.view_x.toFixed(1), y: d.view_y.toFixed(1) })} · {d.zone ? dt("dZone", { z: d.zone }) : dt("dNoZone")}
              {canEdit && <> <button type="button" onClick={() => act(d.id, "rotate")}>{dt("dRotate")}</button>
                <button type="button" onClick={() => act(d.id, "remove")}>{dt("dRemove")}</button></>}
            </li>
          ))}
        </ul>
      )}
      {secret && <p class="device-secret" role="status">{secret}</p>}
      {canEdit && (
        <div class="device-form">
          <input name="dev-id" placeholder={dt("dId")} value={f.id} onInput={(e) => setF({ ...f, id: e.currentTarget.value })} />
          <select name="dev-kind" value={f.kind} onChange={(e) => setF({ ...f, kind: e.currentTarget.value })}>
            <option value="contact">{dt("kContact")}</option><option value="alarm">{dt("kAlarm")}</option><option value="other">{dt("kOther")}</option>
          </select>
          <input name="dev-map" placeholder={dt("dMap")} value={f.map_id} onInput={(e) => setF({ ...f, map_id: e.currentTarget.value })} />
          <input name="dev-ver" placeholder={dt("dVer")} value={f.map_version} onInput={(e) => setF({ ...f, map_version: e.currentTarget.value })} />
          <input name="dev-x" type="number" step="0.1" placeholder={dt("dX")} value={f.x} onInput={(e) => setF({ ...f, x: e.currentTarget.value })} />
          <input name="dev-y" type="number" step="0.1" placeholder={dt("dY")} value={f.y} onInput={(e) => setF({ ...f, y: e.currentTarget.value })} />
          <button type="button" onClick={add} disabled={!f.id || !f.map_id || !f.map_version || f.x === "" || f.y === ""}>{dt("dAdd")}</button>
        </div>
      )}
    </section>
  );
}
