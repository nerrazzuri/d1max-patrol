// 布置 → 禁行区(W44 第三期,设计 §5.2、§9;规则是决策 66 和 W41 的,一条不改,只换成人话):
// - 三种:禁行区(圈一块)、虚拟墙(画一条线,站点存成约 0.3 m 宽的禁行带)、限速区(圈一块,填限速);可以设成临时的(几天)。
// - 加、收紧:保存就生效。删、缩小(放宽):要确认,确认之前机器人照旧绕开原来的范围;网页上确认放宽要再输一次密码。
// - 机器人还没收到在生效的那一份:写「等机器人确认中」。
// 改的先记在页面上,外框「保存」整份存(带打开时看到的修订号;别人改过回 409,外框说「刚才有人改过」)。
// 抹掉区(地图编辑)、人员忽略区不在这里画,原样带着存。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import { ConfirmPassword } from "./confirmpw";
import { Confirm, Modal } from "./dialogs";
import { t } from "./i18n";
import { polyPx, toMap, type NavZones, type TabProps } from "./layoutpage";
import type { NavZone, Pt } from "./mapview";

type Draw = "nogo" | "wall" | "slow";
interface Form { label: string; hard: boolean; speed: number; temp: boolean; days: number }
/** 新画的:站点要的格式(临时的带 for_s,站点按自己的钟换成到期时刻)。 */
type Draft = NavZone & { for_s?: number };
const DAYS = [1, 3, 7, 30];

/** 一个区域给人看的一行字:种类、硬、临时到什么时候。 */
export function nogoLine(z: Draft): string {
  const kind = z.wall ? t("kindVWall") : z.kind === "slow" ? t("kindSlow", { v: z.max_speed_mps ?? "?" }) : z.kind === "ignore" ? t("kindIgnore") : t("kindNoGo");
  const bits = [kind];
  if (z.hard) bits.push(t("kindHard"));
  if (typeof z.until_ms === "number") {
    const d = new Date(z.until_ms);
    const two = (v: number) => String(v).padStart(2, "0");
    bits.push(t("kindTemp", { t: `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}` }));
  } else if (z.for_s) bits.push(t("lyTempDays", { n: Math.round(z.for_s / 86400) }));
  return bits.join(" · ");
}

interface RobotRun { robot_id: string; running?: { map_id?: string; map_version?: string; zones?: { match?: boolean | null } | null } | null }

export function NogoTab(p: TabProps) {
  const nav = p.data.nav;
  const [zones, setZones] = useState<Draft[]>(nav?.zones ?? []);
  const [draw, setDraw] = useState<Draw | null>(null);
  const [pts, setPts] = useState<Pt[]>([]);
  const [form, setForm] = useState(false);
  const [removing, setRemoving] = useState<Draft | null>(null);
  const [confirming, setConfirming] = useState<"ask" | "pw" | null>(null);
  const [behind, setBehind] = useState<string[]>([]);

  const changed = JSON.stringify(zones) !== JSON.stringify(nav?.zones ?? []);
  useEffect(() => p.setDirty(changed), [changed]);
  useEffect(() => {
    p.registerSave(async () => {
      if (!nav) return;
      await api<NavZones>("POST", `${p.base}/zones`, { zones, base_revision: nav.revision });
      await p.reload("nav");
    });
    return () => p.registerSave(null);
  }, [zones, nav?.revision]);
  useEffect(() => {
    let live = true;
    api<{ robots: RobotRun[] }>("GET", "/api/robots").then((d) => {
      if (!live) return;
      setBehind((d.robots ?? []).filter((r) => r.running?.map_id === p.map.map_id && r.running?.map_version === p.map.version
        && r.running?.zones?.match === false).map((r) => r.robot_id));
    }).catch(() => {});
    return () => { live = false; };
  }, [nav?.revision]);

  const held = nav?.held ?? [];
  const current = !!nav?.confirmed?.current;
  const shown = zones.filter((z) => z.kind !== "erase");
  const minPts = draw === "wall" ? 2 : 3;

  async function confirm(pw?: string) {
    if (!nav) return;
    await api("POST", `${p.base}/zones/confirm`, {
      revision: nav.revision, confirm: true, ...(nav.zones.length === 0 ? { empty_ok: true } : {}), ...(pw ? { confirm_password: pw } : {}),
    });
    setConfirming(null);
    p.say(t("navConfirmedOk", { rev: nav.revision }));
    await p.reload("nav");
  }

  return (
    <div class="ly-tab">
      <p.Map
        drawing={!!draw}
        onPick={draw ? (px, py) => setPts((d) => [...d, toMap(p.meta, px, py)]) : undefined}
        world={<>
          {shown.map((z) => z.wall
            ? <polyline key={z.id} points={polyPx(p.meta, z.wall)} class="nogo-line" />
            : <polygon key={z.id} points={polyPx(p.meta, z.polygon)} class={z.kind === "slow" ? "slow-area" : z.kind === "ignore" ? "ignore-area" : z.hard ? "nogo-area hard" : "nogo-area"} />)}
          {held.map((z) => <polygon key={`held-${z.id}`} points={polyPx(p.meta, z.polygon)} class="nogo-area held" />)}
          {pts.length > 0 && <polyline points={polyPx(p.meta, pts)} class={draw === "wall" ? "draft-line vwall" : "draft-line"} />}
        </>}
        marks={(s) => pts.map(([x, y], i) => { const [sx, sy] = s.map(x, y); return <circle key={i} cx={sx} cy={sy} r={4} class="draft-pt" />; })} />
      <div class="ly-side">
        <div class="nogo-status">
          {!nav ? <p class="muted">…</p> : current && held.length === 0
            ? <p class="ok-text">{t("lyNogoLive")}</p>
            : <p class="warn-text">{t("lyNogoWaitYou", { names: held.map((z) => z.label || z.id).join("、") || "—" })}</p>}
          {behind.length > 0 && <p class="warn-text">{t("lyNogoWaitRobot", { ids: behind.join("、") })}</p>}
          {nav && !current && !changed && <button type="button" class="btn sm primary" onClick={() => setConfirming("ask")}>{t("navConfirmBtn")}</button>}
        </div>
        {draw ? (
          <div class="row wrap">
            <span class="muted small">{draw === "wall" ? t("vwallDrawHint", { n: pts.length, w: 0.3 }) : t("navDrawHint", { n: pts.length })}</span>
            <button type="button" class="btn sm" disabled={!pts.length} onClick={() => setPts(pts.slice(0, -1))}>{t("undoPoint")}</button>
            <button type="button" class="btn sm" onClick={() => { setDraw(null); setPts([]); }}>{t("cancel")}</button>
            <button type="button" class="btn sm primary" disabled={pts.length < minPts} onClick={() => setForm(true)}>{t("lyDrawDone")}</button>
          </div>
        ) : (
          <div class="row wrap">
            <button type="button" class="btn" onClick={() => { setDraw("nogo"); setPts([]); }}>{t("drawNoGo")}</button>
            <button type="button" class="btn" onClick={() => { setDraw("wall"); setPts([]); }}>{t("drawVWall")}</button>
            <button type="button" class="btn" onClick={() => { setDraw("slow"); setPts([]); }}>{t("drawSlow")}</button>
          </div>
        )}
        <p class="muted small">{t("lyNogoHint")}</p>
        <ul class="plain ly-list">
          {shown.length === 0 && <li class="muted small">{t("noNavZones")}</li>}
          {shown.map((z) => (
            <li key={z.id} class="row">
              <span><b>{z.label || z.id}</b> <span class="muted small">{nogoLine(z)}</span></span>
              <button type="button" class="btn sm push" onClick={() => setRemoving(z)}>{t("remove")}</button>
            </li>
          ))}
        </ul>
      </div>
      {form && draw && (
        <NogoForm kind={draw} onClose={() => setForm(false)} onSave={(f) => {
          let id = `${draw === "wall" ? "w" : draw === "slow" ? "s" : "n"}${(nav?.revision ?? 0) + 1}`;
          while (zones.some((z) => z.id === id)) id += "x";
          const z: Draft = { id, kind: draw === "slow" ? "slow" : "nogo", label: f.label.trim(), polygon: draw === "wall" ? [] : pts };
          if (draw === "wall") { z.wall = pts; delete (z as Partial<Draft>).polygon; }
          if (draw === "slow") z.max_speed_mps = f.speed;
          else if (f.hard) z.hard = true;
          if (f.temp) z.for_s = f.days * 86400;
          setZones([...zones, z]);
          setForm(false);
          setDraw(null);
          setPts([]);
        }} />
      )}
      {removing && (
        <Confirm text={t("lyNogoRemoveAsk", { label: removing.label || removing.id })} yes={t("remove")} danger onNo={() => setRemoving(null)}
          onYes={() => { const z = removing; setRemoving(null); setZones(zones.filter((o) => o.id !== z.id)); }} />
      )}
      {confirming === "ask" && (
        <SureDialog onNo={() => setConfirming(null)} onYes={() => {
          if (held.length > 0) setConfirming("pw");
          else void confirm().catch((e) => p.say(t("failed", { why: e instanceof ApiError ? e.message : String(e) })));
        }} />
      )}
      {confirming === "pw" && <ConfirmPassword title={t("navConfirmBtn")} onClose={() => setConfirming(null)} onConfirm={(pw) => confirm(pw)} />}
    </div>
  );
}

/** 确认那一句(人话):勾上才能确认。 */
function SureDialog({ onYes, onNo }: { onYes: () => void; onNo: () => void }) {
  const [sure, setSure] = useState(false);
  return (
    <Modal label={t("navConfirmBtn")} onClose={onNo}>
      <p>{t("lyNogoConfirmLead")}</p>
      <label class="field-check"><input type="checkbox" name="sure" checked={sure} onChange={(e) => setSure(e.currentTarget.checked)} />{t("lyNogoSure")}</label>
      <div class="row end">
        <button type="button" class="btn" onClick={onNo}>{t("cancel")}</button>
        <button type="button" class="btn primary" disabled={!sure} onClick={onYes}>{t("lyConfirm")}</button>
      </div>
    </Modal>
  );
}

function NogoForm({ kind, onClose, onSave }: { kind: Draw; onClose: () => void; onSave: (f: Form) => void }) {
  const [f, setF] = useState<Form>({
    label: kind === "wall" ? t("legendVWall") : kind === "slow" ? t("legendSlow") : t("legendNoGo"), hard: false, speed: 0.3, temp: false, days: 3,
  });
  const ok = f.label.trim().length > 0 && f.label.trim().length <= 64 && (kind !== "slow" || (f.speed > 0 && f.speed <= 5));
  return (
    <Modal label={kind === "wall" ? t("navFormTitleVWall") : kind === "slow" ? t("navFormTitleSlow") : t("navFormTitleNoGo")} onClose={onClose}>
      <form class="form" onSubmit={(e) => { e.preventDefault(); if (ok) onSave(f); }}>
        <label class="field">{t("navName")}<input name="label" value={f.label} maxLength={64} onInput={(e) => setF({ ...f, label: e.currentTarget.value })} /></label>
        {kind === "slow" ? (
          <label class="field">{t("navSpeed")}<input type="number" min={0.1} max={5} step={0.1} value={f.speed}
            onInput={(e) => setF({ ...f, speed: Number(e.currentTarget.value) })} /></label>
        ) : (
          <label class="field-check"><input type="checkbox" name="hard" checked={f.hard} onChange={(e) => setF({ ...f, hard: e.currentTarget.checked })} />{t("navHard")}</label>
        )}
        <label class="field-check"><input type="checkbox" name="temp" checked={f.temp} onChange={(e) => setF({ ...f, temp: e.currentTarget.checked })} />{t("lyTemp")}</label>
        {f.temp && (
          <label class="field">{t("lyTempFor")}
            <select name="days" value={String(f.days)} onChange={(e) => setF({ ...f, days: Number(e.currentTarget.value) })}>
              {DAYS.map((d) => <option key={d} value={String(d)}>{t("lyTempDays", { n: d })}</option>)}
            </select>
          </label>
        )}
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!ok}>{t("save")}</button>
        </div>
      </form>
    </Modal>
  );
}
