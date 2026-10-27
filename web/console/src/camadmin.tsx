// 固定摄像头的登记 / 改 / 删(管理员;原来只有站点主机上的命令行 camera-add / camera-rm)。
// 站点管校验;这里只管填表、显示站点回的警告(防区没映射到拦截点、防区没画多边形)。
// 口令:站点从不把口令发回来(只说「有没有」);改的时候不勾「换口令」就不带 password,站点保留原来的。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm, Modal } from "./dialogs";
import { Shape } from "./icons";
import { L } from "./runviz";

type Say = (s: string) => void;

export interface CamWarning { code: string; text: string }
export interface CamRow {
  name: string;
  zone: string;
  onvif_url: string;
  username: string;
  has_password: boolean;
  rtsp_set: boolean;
  rtsp_url: string;
  motion: boolean;
  zone_mapped: boolean;
  zone_polygon: boolean;
  warnings: CamWarning[];
}
interface LiveRow { name: string; connected: boolean; error: string }

export function warningText(w: CamWarning, zone: string): string {
  if (w.code === "zone_unmapped") return L("Zone “{z}” is not mapped to an intercept point: an intrusion from this camera has no robot to send.", "防区「{z}」还没映射到拦截点:这台摄像头报入侵时没有机器人可派。", { z: zone });
  if (w.code === "zone_no_polygon") return L("Zone “{z}” has no polygon on any map: the lists (time slots / people) cannot tell whether someone is on authorised ground.", "防区「{z}」没在任何一张图上画多边形:名单(时段 / 人员授权)判不了。", { z: zone });
  return w.text;
}

function errText(e: unknown): string {
  return L("Failed: {why}", "没成:{why}", { why: e instanceof ApiError ? e.message : String(e) });
}

export function CamerasPanel({ say }: { say: Say }) {
  const [rows, setRows] = useState<CamRow[] | null>(null);
  const [live, setLive] = useState<Map<string, LiveRow>>(new Map());
  const [form, setForm] = useState<CamRow | "new" | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const load = useCallback(() => {
    api<{ cameras: CamRow[] }>("GET", "/api/admin/cameras").then((d) => setRows(d.cameras)).catch((e) => say(errText(e)));
    api<{ cameras: LiveRow[] }>("GET", "/api/cameras").then((d) => setLive(new Map(d.cameras.map((c) => [c.name, c])))).catch(() => setLive(new Map()));
  }, [say]);
  useEffect(load, [load]);
  return (
    <section class="panel" aria-label={L("Cameras", "摄像头")}>
      <div class="panel-head">
        <h2>{L("Cameras", "摄像头")}</h2>
        <span class="muted small push">{L("Fixed ONVIF cameras: their intrusion events dispatch a robot. Changes reach the site within about 30 s.", "固定的 ONVIF 摄像头:它们报的入侵会派机器人。改动 30 秒内生效。")}</span>
        <button type="button" class="btn primary" onClick={() => setForm("new")}>{L("Add camera", "添加摄像头")}</button>
      </div>
      <table class="grid">
        <thead>
          <tr>
            <th>{L("Name", "名字")}</th><th>{L("Zone", "防区")}</th><th>ONVIF</th><th>{L("Account", "账号")}</th>
            <th>{L("Live view", "画面")}</th><th>{L("Motion counts", "动了也算")}</th><th>{L("Status", "状态")}</th><th>{L("Actions", "操作")}</th>
          </tr>
        </thead>
        <tbody>
          {rows && rows.length === 0 && <tr><td colSpan={8} class="muted">{L("No cameras yet.", "还没有摄像头。")}</td></tr>}
          {(rows ?? []).map((c) => {
            const l = live.get(c.name);
            return (
              <tr key={c.name}>
                <td class="mono strong">{c.name}</td>
                <td>
                  {c.zone}
                  {c.warnings.map((w) => <div key={w.code} class="warncell" data-warning={w.code}><Shape kind="P2" size={10} /> {warningText(w, c.zone)}</div>)}
                </td>
                <td class="mono">{c.onvif_url}</td>
                <td>{c.username || "—"} {c.username && <span class="muted small">{c.has_password ? L("(password set)", "(口令已设)") : L("(no password)", "(没口令)")}</span>}</td>
                <td>{c.rtsp_set ? L("yes", "有") : "—"}</td>
                <td>{c.motion ? L("yes", "是") : "—"}</td>
                <td>{l ? (l.connected ? <span class="state tone-success">{L("Connected", "已连上")}</span> : <span class="state p2"><Shape kind="P2" />{L("Not connected", "没连上")}</span>) : <span class="muted small">{L("Waiting for the site to pick it up", "等站点接上")}</span>}</td>
                <td class="actions">
                  <button type="button" class="btn sm" onClick={() => setForm(c)}>{L("Edit", "改")}</button>
                  <button type="button" class="btn sm" onClick={() => setRemoving(c.name)}>{L("Remove", "删除")}</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {form && (
        <CameraForm initial={form === "new" ? null : form} onClose={() => setForm(null)} say={say}
          onSaved={(warnings, zone) => {
            setForm(null);
            say(warnings.length ? L("Saved. Check: {w}", "存好了。注意:{w}", { w: warnings.map((w) => warningText(w, zone)).join(" ") }) : L("Saved.", "存好了。"));
            load();
          }} />
      )}
      {removing && (
        <Confirm text={L("Remove camera {n}? Its intrusion events will no longer reach the site.", "删除摄像头 {n}?它的入侵事件将不再进站点。", { n: removing })}
          yes={L("Remove", "删除")} danger onNo={() => setRemoving(null)}
          onYes={() => {
            const name = removing;
            setRemoving(null);
            api("POST", `/api/admin/cameras/${enc(name)}/remove`, {}).then(() => { say(L("Removed {n}.", "删了 {n}。", { n: name })); load(); }).catch((e) => say(errText(e)));
          }} />
      )}
    </section>
  );
}

export function CameraForm({ initial, onClose, onSaved, say }: { initial: CamRow | null; onClose: () => void; onSaved: (w: CamWarning[], zone: string) => void; say: Say }) {
  const edit = initial !== null;
  const [name, setName] = useState(initial?.name ?? "");
  const [onvif, setOnvif] = useState(initial?.onvif_url ?? "");
  const [user, setUser] = useState(initial?.username ?? "");
  const [pw, setPw] = useState("");
  const [changePw, setChangePw] = useState(!edit || !initial?.has_password);
  const [zone, setZone] = useState(initial?.zone ?? "");
  const [rtsp, setRtsp] = useState(initial?.rtsp_url ?? "");
  const [motion, setMotion] = useState(initial?.motion ?? false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    const body: Record<string, unknown> = { name, onvif_url: onvif, username: user, zone, rtsp_url: rtsp, motion, edit };
    if (changePw) body.password = pw;                 // 不换就不带:站点保留原来的
    try {
      const d = await api<{ warnings: CamWarning[] }>("POST", "/api/admin/cameras", body);
      setPw("");
      onSaved(d.warnings, zone);
    } catch (x) {
      setErr(x instanceof ApiError ? x.message : String(x));
      say(errText(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal label={edit ? L("Edit camera {n}", "改摄像头 {n}", { n: name }) : L("Add camera", "添加摄像头")} onClose={() => { setPw(""); onClose(); }}>
      <form class="form camform" onSubmit={submit}>
        <label class="field">{L("Name", "名字")}
          <input value={name} readOnly={edit} required pattern="[A-Za-z0-9._\-]{1,64}" autocomplete="off" onInput={(e) => setName(e.currentTarget.value)} />
        </label>
        <label class="field">{L("ONVIF address (camera or NVR)", "ONVIF 地址(摄像头或 NVR)")}
          <input value={onvif} required placeholder="http://192.168.1.50" autocomplete="off" onInput={(e) => setOnvif(e.currentTarget.value)} />
        </label>
        <label class="field">{L("Account", "账号")}
          <input value={user} autocomplete="off" onInput={(e) => setUser(e.currentTarget.value)} />
        </label>
        {edit && initial?.has_password && (
          <label class="field row">
            <input type="checkbox" checked={changePw} onChange={(e) => setChangePw(e.currentTarget.checked)} />
            {L("Change the password", "换口令")}
          </label>
        )}
        {(changePw || !edit) && (
          <label class="field">{L("Password", "口令")}
            <input type="password" value={pw} autocomplete="new-password" onInput={(e) => setPw(e.currentTarget.value)} />
          </label>
        )}
        <label class="field">{L("Zone (the camera's intrusions count as this zone)", "防区(这台摄像头报的入侵算这个防区)")}
          <input value={zone} required autocomplete="off" onInput={(e) => setZone(e.currentTarget.value)} />
        </label>
        <label class="field">{L("RTSP address for live view (optional)", "看画面的 RTSP 地址(可选)")}
          <input value={rtsp} placeholder="rtsp://192.168.1.50/stream1" autocomplete="off" onInput={(e) => setRtsp(e.currentTarget.value)} />
        </label>
        <label class="field row">
          <input type="checkbox" checked={motion} onChange={(e) => setMotion(e.currentTarget.checked)} />
          {L("Plain motion also counts as intrusion (more false alarms: shadows, headlights)", "普通「画面动了」也算入侵(树影、车灯会多误报)")}
        </label>
        {err && <p class="state p2" role="alert"><Shape kind="P2" />{err}</p>}
        <div class="row end">
          <button type="button" class="btn" onClick={() => { setPw(""); onClose(); }}>{L("Cancel", "取消")}</button>
          <button type="submit" class="btn primary" disabled={busy}>{edit ? L("Save", "保存") : L("Add camera", "添加摄像头")}</button>
        </div>
      </form>
    </Modal>
  );
}
