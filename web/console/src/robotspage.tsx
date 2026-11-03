// 机器人页(W44 第二期,设计 §5.1「机器人」):每台一张卡片 —— 名字、在线、电量、今天巡了几趟、有没有毛病(站点
// /api/robots/overview 一次给全,今天按站点时区算)。点开是详情:改名字、软件版本、证书到期、最近的毛病、只能手动派 / 自动派、
// 重出开通码、解绑(确认 + 再输一次密码;解绑 = 机器人恢复出厂,W42)。
// W42:「添加」= 手机扫机器人贴纸;机器人连上站点后挂在本页顶上「等待批准加入」,核对 6 位核对码、再输密码批准。
// 机器人上有人提了换 WiFi:详情里确认 / 不换(卡片上标出来)。工程师的开通码流程收进「添加」里的一个按钮。
// 照片位这一期放图标;电池健康机器人还不报,不显示。
// W60 卡片分层:第一层名字、状态、在做什么、电量;第二层定位、网络、今天几趟;出了毛病才多一条琥珀色提示
// (正常时安静,不写「正常」)。在做什么、定位从值班的实时数据来(units),概况接口没有。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { ConfirmPassword } from "./confirmpw";
import { hm, robotNames } from "./data";
import { Confirm, Modal } from "./dialogs";
import { alertTitle, lang, refusalText, t, type Key } from "./i18n";
import { autoResolved, canDispatch, canPatrol, isOwnerLike } from "./model";
import { notAccepted, type CommandReply } from "./posepick";
import { siteHm } from "./todaypatrols";
import { Shape } from "./icons";
import { locText, StateText, type UnitInfo } from "./live";

type Say = (s: string) => void;
export interface RobotCard {
  robot_id: string; name: string; revoked: boolean; expires_at: number; online: boolean; battery_pct: number | null; patrols_today: number;
  /** W71 ``nav2``:Nav2 没起来(机器人退回自带规划)。 */
  problems: ({ key: string; kind: string; first_ms: number } | { device: string; state: string } | { nav2: string })[];
  /** W72:``level``、``resolved_by`` 老站点没有。 */
  recent_problems: RecentProblem[];
  agent: string | null; compat: string; manual_only: boolean;
  wifi?: Wifi | null;
  /** W72 详情:老站点没有这些。 */
  patrols_7d?: number; aborts_7d?: number; last_patrol_ms?: number | null; last_seen_ms?: number | null;
  storage?: { used_ratio: number; backlog_files: number; oldest_backlog_s: number | null } | null;
  map?: { id: string; version: string | null; match: boolean | null } | null;
  devices?: { id: string; state: string }[];
  enrolled_at?: number | null; manual_note?: string; held?: { by: string; at_ms: number } | null;
}
export interface Wifi {
  ssid: string; link: "wifi" | "4g" | "none";
  change: { id: string; ssid: string; mode: "online" | "trial"; expires_ms: number | null } | null;
}
export interface BindReq {
  id: string; serial: string; check: string; model: string; version: string; source: string; created_ms: number; expires_ms: number;
}
/** 等待批准、卡片多久刷一次(机器人申请过来要尽快看到)。 */
const REFRESH_MS = 10_000;

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

function day(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const label = (r: RobotCard) => r.name || r.robot_id;

function problemText(p: RobotCard["problems"][number]): string {
  if ("nav2" in p) return t("robotNav2Down", { why: p.nav2 });
  return "kind" in p ? alertTitle(lang.value, p.kind, p.kind) : t("robotDeviceBad", { d: p.device });
}

/** ``readOnly``(W60):经理只看 —— 没有添加、批准加入、改名、改派单方式、开通码、解绑、确认换 WiFi。 */
export function RobotsPage({ say, now, units = [], readOnly = false, role = "admin" }: {
  say: Say; now: number; units?: UnitInfo[]; readOnly?: boolean; role?: string;
}) {
  const [rows, setRows] = useState<RobotCard[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<{ id: string; code: string; hours: number } | null>(null);
  const [pending, setPending] = useState<BindReq[]>([]);
  const [approving, setApproving] = useState<BindReq | null>(null);
  const [rejecting, setRejecting] = useState<BindReq | null>(null);
  void now;
  const loadPending = useCallback(() => {
    // 没开绑定的站点(没接站点目录)回 404:当没有
    api<{ requests: BindReq[] }>("GET", "/api/admin/bind").then((d) => setPending(d.requests ?? [])).catch(() => setPending([]));
  }, []);
  const load = useCallback(() => {
    api<{ robots: RobotCard[] }>("GET", "/api/robots/overview")
      .then((d) => {
        const list = (d.robots ?? []).filter((r) => !r.revoked);
        setRows(list);
        robotNames.value = { ...robotNames.value, ...Object.fromEntries(list.filter((r) => r.name).map((r) => [r.robot_id, r.name])) };
      })
      .catch((e) => say(errText(e)));
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    loadPending();
    const h = window.setInterval(() => { load(); loadPending(); }, REFRESH_MS);
    return () => window.clearInterval(h);
  }, [load, loadPending]);
  const cur = rows?.find((r) => r.robot_id === open) ?? null;
  return (
    <div class="robots">
      <div class="panel-head">
        <h1>{t("robots")}</h1>
        {!readOnly && <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addRobot")}</button>}
      </div>
      <p class="muted lead">{t("robotsLead")}</p>
      {!readOnly && pending.length > 0 && (
        <section class="panel bind-pending" aria-label={t("bindPending")}>
          <h2>{t("bindPending")}</h2>
          <p class="muted small">{t("bindPendingHint")}</p>
          <ul class="plain">
            {pending.map((p) => (
              <li key={p.id} class="bind-req row wrap" data-serial={p.serial}>
                <span class="mono">{p.serial}</span>
                <span>{t("bindCheck")} <strong class="mono check-digits">{p.check}</strong></span>
                <span class="muted small">{t("bindFrom", { t: hm(p.created_ms), src: p.source })}</span>
                <span class="push row">
                  <button type="button" class="btn sm primary" onClick={() => setApproving(p)}>{t("bindApprove")}</button>
                  <button type="button" class="btn sm" onClick={() => setRejecting(p)}>{t("bindReject")}</button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {rows && rows.length === 0 && <p class="empty">{t("robotsNone")}</p>}
      <div class={cur ? "robots-split" : undefined}>
        <div class="robot-cards">
          {(rows ?? []).map((r) => {
            const u = units.find((x) => x.robot.robot_id === r.robot_id);
            const loc = locText(u);
            const warn = r.problems.length > 0 || !!r.wifi?.change;
            return (
              <button key={r.robot_id} type="button" data-robot={r.robot_id}
                class={["robot-card", r.robot_id === open ? "on" : "", warn ? "warn" : ""].filter(Boolean).join(" ")}
                onClick={() => setOpen(r.robot_id === open ? null : r.robot_id)}>
                <span class="fleet-head">
                  <span class="robot-name">{label(r)}</span>
                  {r.name && <small class="muted mono">{r.robot_id}</small>}
                  <span class="fleet-state">
                    {!r.online ? <span class="state tone-error">{t("st_offline")}</span>
                      : warn ? <span class="state tone-warning"><Shape kind="P2" />{t("fleetAttention")}</span>
                      : <span class="state tone-success">{t("online")}</span>}
                  </span>
                </span>
                {u && r.online && <span class="fleet-doing"><StateText s={u.state} stale={u.stale} />{u.task && <span class="muted">{u.task}</span>}</span>}
                <span class="fleet-batt">
                  <span class="muted">{t("fleetBattery")}</span>
                  <span class="mono fleet-pct">{r.battery_pct == null ? "—" : `${r.battery_pct}%`}</span>
                  <span class="fleet-bar" aria-hidden="true"><i style={{ width: `${Math.max(0, Math.min(100, r.battery_pct ?? 0))}%` }} /></span>
                </span>
                {r.problems.length > 0 && (
                  <span class="fleet-warn"><Shape kind="P2" />{problemText(r.problems[0])}{r.problems.length > 1 ? ` · ${t("robotProblems", { n: r.problems.length })}` : ""}</span>
                )}
                {r.wifi?.change && <span class="fleet-warn"><Shape kind="P2" />{t("wifiPending")}</span>}
                <span class="fleet-facts">
                  <span><small>{t("fleetLoc")}</small>{loc ? t(loc) : "—"}</span>
                  <span><small>{t("fleetNet")}</small>{r.wifi ? t(`link_${r.wifi.link}` as Key) : "—"}</span>
                  <span><small>{t("fleetToday")}</small>{t("fleetPatrols", { n: r.patrols_today })}</span>
                </span>
              </button>
            );
          })}
        </div>
        {cur && <RobotDetail readOnly={readOnly} r={cur} u={units.find((x) => x.robot.robot_id === cur.robot_id)} now={now} role={role}
          say={say} onChanged={load} onCode={setCode} onClose={() => setOpen(null)} />}
      </div>
      {adding && (
        <AddRobot onClose={() => setAdding(false)} say={say}
          onDone={(id, c, h) => { setAdding(false); setCode({ id, code: c, hours: h }); load(); }} />
      )}
      {code && <CodeDialog {...code} onClose={() => setCode(null)} say={say} />}
      {approving && (
        <BindApprove req={approving} onClose={() => setApproving(null)}
          onDone={() => { say(t("bindApproved", { id: approving.serial })); setApproving(null); loadPending(); load(); }} />
      )}
      {rejecting && (
        <Confirm text={t("bindRejectConfirm", { id: rejecting.serial })} yes={t("bindReject")} danger onNo={() => setRejecting(null)}
          onYes={() => {
            const r = rejecting;
            setRejecting(null);
            api("POST", `/api/admin/bind/${enc(r.id)}/reject`, {})
              .then(() => say(t("bindRejected", { id: r.serial })))
              .catch((e) => say(errText(e)))
              .finally(loadPending);
          }} />
      )}
    </div>
  );
}

type RecentProblem = { key: string; kind: string; level?: string; title?: string; first_ms: number; resolved_ms: number | null; resolved_by?: string };

/** 今天的只写钟点,别的天写月-日 钟点。 */
function when(ms: number, now: number = Date.now()): string {
  const d = new Date(ms);
  const same = day(ms) === day(now);
  return same ? hm(ms) : `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm(ms)}`;
}

/** W72(用户 11-03):详情里的告警分三组放在最下面 —— 要处理(没解决的 P1)、待跟进(没解决的其余,不急)、
 * 记录(已解决的,收起来;自动恢复的标出来)。老站点没报级别的算要处理(宁可多看)。 */
function RecentAlerts({ items }: { items: RecentProblem[] }) {
  const open = items.filter((p) => p.resolved_ms == null);
  const act = open.filter((p) => !p.level || p.level === "P1");
  const later = open.filter((p) => p.level && p.level !== "P1");
  const log = items.filter((p) => p.resolved_ms != null);
  const row = (p: RecentProblem, tag?: { text: string; tone: string }) => {
    const name = alertTitle(lang.value, p.kind, p.title || p.kind);
    // 站点写的具体标题(「断联 20 秒后恢复」)是中文的:中文界面下跟在类型名后面
    const more = lang.value === "zh" && p.title && p.title !== name ? p.title : "";
    return (
    <li key={p.key}>
      <time class="mono">{when(p.first_ms)}</time>
      <span class="rd-what">{name}{more && <small>{more}</small>}</span>
      {tag ? <span class={`rd-tag ${tag.tone}`}>{tag.text}</span> : <span />}
    </li>
    );
  };
  const head = (key: Key, n: number) => <h4><span>{t(key)}</span><span class="rd-count">{n}</span></h4>;
  return (
    <section class="robot-alerts rd-sec">
      <h3 class="rd-h">{t("robotRecent")}</h3>
      {items.length === 0 && <p class="muted small">{t("robotRecentNone")}</p>}
      {act.length > 0 && (
        <div class="rd-group act" data-group="act">{head("robotAct", act.length)}
          <ul class="rd-list">{act.map((p) => row(p, { text: t("robotOpen"), tone: "open" }))}</ul></div>
      )}
      {later.length > 0 && (
        <div class="rd-group later" data-group="later">{head("robotLater", later.length)}
          <ul class="rd-list">{later.map((p) => row(p))}</ul></div>
      )}
      {log.length > 0 && (
        <details class="rd-group rd-log" data-group="log">
          <summary>{t("robotLog", { n: log.length })}</summary>
          <ul class="rd-list">{log.map((p) => row(p, autoResolved(p) ? { text: t("vAuto"), tone: "auto" } : { text: t("vDone"), tone: "done" }))}</ul>
        </details>
      )}
    </section>
  );
}

/** 多久前(最后联系):一分钟内按秒,一小时内按分钟,再久写钟点。 */
function ago(ms: number, now: number): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 5) return t("rdJustNow");
  if (s < 60) return t("rdSecAgo", { n: s });
  if (s < 3600) return t("rdMinAgo", { n: Math.floor(s / 60) });
  return when(ms, now);
}

type Act = (fn: () => Promise<unknown>, msg?: string) => Promise<void>;

/** W72(用户 11-03):手机上有的操作网页也要 —— 选路线开始巡逻、回待命点、停止当前任务、自动定位、叫停后恢复。
 * 权限跟手机一样:派巡逻、停止 = 能派单的 + 经理;回待命点、自动定位、恢复 = 能派单的(经理不能让它再动)。 */
function Controls({ r, role, say, onChanged }: { r: RobotCard; role: string; say: Say; onChanged: () => void }) {
  const [routes, setRoutes] = useState<string[] | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const patrol = canPatrol(role);
  const dispatch = canDispatch(role);
  useEffect(() => {
    if (!patrol) return;
    api<{ missions?: string[] }>("GET", "/api/schedule")
      .then((d) => { const m = d.missions ?? []; setRoutes(m); setPick(m.length === 1 ? m[0] : ""); })
      .catch(() => setRoutes([]));
  }, [r.robot_id, patrol]);
  useEffect(() => setErr(null), [r.robot_id]);
  const id = r.name || r.robot_id;
  const send = async (fn: () => Promise<(CommandReply & { queued?: boolean; start_after_ms?: number; because?: string; utc_offset_min?: number }) | null>,
    ok: string) => {
    setBusy(true);
    setErr(null);
    try {
      const res = await fn();
      if (res === null) return;
      if (res.queued) say(t("trQueued", { id, at: siteHm(res.start_after_ms ?? 0, res.utc_offset_min), why: res.because ?? "" }));
      else {
        const no = res.ack ? notAccepted(res) : null;
        if (no) setErr(t("rdNotAccepted", { why: no }));
        else say(ok);
      }
    } catch (e) {
      setErr(e instanceof ApiError ? refusalText(e.message, e.data) : String(e));
    } finally {
      setBusy(false);
      onChanged();
    }
  };
  const path = (a: string) => `/api/robots/${enc(r.robot_id)}/${a}`;
  const abort = () => send(async () => {
    const v = await api<{ status?: { task?: { task_id?: string } | null } | null }>("GET", `/api/robots/${enc(r.robot_id)}`);
    const tid = v.status?.task?.task_id;
    if (!tid) { say(t("rdNoTask", { id })); return null; }
    return api("POST", path("abort"), { task_id: tid });
  }, t("rdSent", { id, what: t("rdAbort") }));
  const off = !r.online;
  return (
    <section class="rd-sec rd-control">
      <h3 class="rd-h">{t("rdControl")}</h3>
      {r.held && (
        <div class="note warn rd-held">
          <span>{t("rdHeld", { by: r.held.by, t: when(r.held.at_ms) })}</span>
          {dispatch && <button type="button" class="btn sm" disabled={busy}
            onClick={() => void send(() => api("POST", path("resume"), {}), t("rdSent", { id, what: t("rdResume") }))}>{t("rdResume")}</button>}
        </div>
      )}
      {off && <p class="muted small">{t("rdOfflineNoCtl")}</p>}
      {patrol && (
        <div class="rd-name">
          <label class="rd-label" for={`rd-route-${r.robot_id}`}>{t("rdRoute")}</label>
          {routes !== null && routes.length === 0 ? <p class="muted small">{t("rdNoRoutes")}</p> : (
            <div class="rd-inline">
              <select id={`rd-route-${r.robot_id}`} name="route" value={pick} onChange={(e) => setPick(e.currentTarget.value)}>
                <option value="">{t("rdPickRoute")}</option>
                {(routes ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
              <button type="button" class="btn sm primary" disabled={busy || off || !pick}
                onClick={() => void send(() => api("POST", path("patrol"), { mission_id: pick }), t("rdPatrolSent", { id, route: pick }))}>
                {t("rdStartPatrol")}
              </button>
            </div>
          )}
        </div>
      )}
      <div class="rd-actions">
        {dispatch && <button type="button" class="btn sm" disabled={busy || off}
          onClick={() => void send(() => api("POST", path("standby/return"), {}), t("rdSent", { id, what: t("rdReturn") }))}>{t("rdReturn")}</button>}
        {dispatch && <button type="button" class="btn sm" disabled={busy || off}
          onClick={() => void send(() => api("POST", path("relocalize"), { global: true }), t("rdSent", { id, what: t("rdAutoLoc") }))}>{t("rdAutoLoc")}</button>}
        {patrol && <button type="button" class="btn sm rd-danger" disabled={busy} onClick={() => void abort()}>{t("rdAbort")}</button>}
      </div>
      {err && <p class="error small" role="alert">{err}</p>}
    </section>
  );
}

function RobotDetail({ r, u, now, role, say, onChanged, onCode, onClose, readOnly = false }: {
  readOnly?: boolean; u?: UnitInfo; now: number; role: string;
  r: RobotCard; say: Say; onChanged: () => void; onCode: (c: { id: string; code: string; hours: number }) => void; onClose: () => void;
}) {
  const [name, setName] = useState(r.name);
  const [unbind, setUnbind] = useState<"ask" | "pw" | null>(null);
  useEffect(() => setName(r.name), [r.robot_id, r.name]);
  const act: Act = async (fn, msg) => {
    try {
      await fn();
      if (msg) say(msg);
    } catch (e) {
      say(errText(e));
    }
    onChanged();
  };
  const compat: Key = r.compat === "ok" ? "compat_ok" : (`compat_${r.compat}` as Key);
  const devices = r.problems.filter((p) => "device" in p);
  const loc = locText(u);
  const fact = (k: Key, v: string, mono = false) => <div><dt>{t(k)}</dt><dd class={mono ? "mono" : undefined}>{v}</dd></div>;
  return (
    <aside class="panel robot-detail" aria-label={label(r)}>
      <div class="panel-head">
        <h2>{label(r)}</h2>
        {r.name && <small class="muted mono">{r.robot_id}</small>}
        {r.online ? <span class="state tone-success">{t("online")}</span> : <span class="state tone-error">{t("st_offline")}</span>}
        <button type="button" class="btn sm push" onClick={onClose}>{t("close")}</button>
      </div>
      <section class="rd-sec">
        <h3 class="rd-h">{t("rdNow")}</h3>
        <dl class="rd-facts">
          <div><dt>{t("rdState")}</dt><dd>{u && r.online ? <><StateText s={u.state} stale={u.stale} />{u.task && <small class="muted"> {u.task}</small>}</>
            : r.online ? t("online") : t("st_offline")}</dd></div>
          {fact("fleetBattery", r.battery_pct == null ? "—" : `${r.battery_pct}%`, true)}
          {fact("fleetLoc", `${loc ? t(loc) : "—"}${u?.robot.loc?.localizer ? ` · ${u.robot.loc.localizer}` : ""}`)}
          {fact("wifiLabel", r.wifi ? `${r.wifi.ssid || "—"} · ${t(`link_${r.wifi.link}` as Key)}` : "—")}
          {fact("rdLastSeen", r.last_seen_ms ? ago(r.last_seen_ms, now) : "—")}
          {r.storage && fact("rdStorage", t("rdStorageVal", { p: Math.round(r.storage.used_ratio * 100), n: r.storage.backlog_files }))}
        </dl>
        {devices.length > 0 && <p class="note small">{devices.map(problemText).join(" · ")}</p>}
      </section>
      {(canPatrol(role) || isOwnerLike(role)) && !r.revoked && <Controls r={r} role={role} say={say} onChanged={onChanged} />}
      <section class="rd-sec">
        <h3 class="rd-h">{t("rdPatrol")}</h3>
        <dl class="rd-facts">
          {fact("rdToday", t("fleetPatrols", { n: r.patrols_today }))}
          {r.patrols_7d != null && fact("rd7d", t("fleetPatrols", { n: r.patrols_7d }))}
          {r.aborts_7d != null && fact("rdAborts", t("rdAbortsN", { n: r.aborts_7d }))}
          {r.last_patrol_ms != null && fact("rdLastPatrol", when(r.last_patrol_ms, now), true)}
        </dl>
      </section>
      {(r.devices?.length ?? 0) > 0 && (
        <section class="rd-sec">
          <h3 class="rd-h">{t("rdDevices")}</h3>
          <ul class="rd-devs">
            {r.devices!.map((x) => (
              <li key={x.id} class={`dev-${x.state}`}><span class="mono">{x.id}</span><span>{t(`dev_${x.state}` as Key)}</span></li>
            ))}
          </ul>
        </section>
      )}
      <section class="rd-sec">
        <h3 class="rd-h">{t("rdBasics")}</h3>
        <dl class="rd-facts">
          {fact("robotId", r.robot_id, true)}
          {fact("colVersion", `${r.agent ?? "—"} · ${t(compat)}`)}
          {r.map && <div><dt>{t("rdMap")}</dt><dd><span class="mono">{r.map.id} v{r.map.version ?? "?"}</span>
            {r.map.match === false && <small class="rd-bad"> {t("rdMapBad")}</small>}
            {r.map.match === true && <small class="muted"> {t("rdMapOk")}</small>}</dd></div>}
          {fact("certExpires", r.expires_at ? day(r.expires_at) : "—", true)}
          {r.enrolled_at != null && fact("rdEnrolled", day(r.enrolled_at), true)}
          <div><dt>{t("dispatchMode")}</dt><dd>{r.manual_only ? t("manualOnly") : t("autoDispatch")}
            {r.manual_only && r.manual_note && <small class="muted"> · {r.manual_note}</small>}</dd></div>
        </dl>
      </section>
      {!readOnly && r.wifi?.change && <section class="rd-sec"><WifiChange id={r.robot_id} ch={r.wifi.change} act={act} /></section>}
      {!readOnly && (
        <section class="rd-sec">
          <h3 class="rd-h">{t("rdManage")}</h3>
          <form class="rd-name" onSubmit={(e) => { e.preventDefault(); void act(() => api("POST", `/api/admin/robots/${enc(r.robot_id)}/name`, { name }), t("saved")); }}>
            <label class="rd-label" for={`rd-name-${r.robot_id}`}>{t("robotName")}</label>
            <div class="rd-inline">
              <input id={`rd-name-${r.robot_id}`} name="name" value={name} maxLength={32} placeholder={r.robot_id}
                onInput={(e) => setName(e.currentTarget.value)} />
              <button type="submit" class="btn sm" disabled={name === r.name}>{t("robotNameSave")}</button>
            </div>
          </form>
          <div class="rd-actions">
            <button type="button" class="btn sm" onClick={() => void act(() => api("POST", `/api/robots/${enc(r.robot_id)}/service`, { manual_only: !r.manual_only }))}>
              {r.manual_only ? t("setAuto") : t("setManual")}
            </button>
            <button type="button" class="btn sm" onClick={() => void act(async () => {
              const d = await api<{ code: string; hours: number }>("POST", `/api/admin/robots/${enc(r.robot_id)}/code`, {});
              onCode({ id: r.robot_id, code: d.code, hours: d.hours });
            })}>{t("newCode")}</button>
            <button type="button" class="btn sm rd-danger" onClick={() => setUnbind("ask")}>{t("robotUnbind")}</button>
          </div>
        </section>
      )}
      {unbind === "ask" && (
        <Confirm text={t("robotUnbindConfirm", { id: label(r) })} yes={t("robotUnbind")} danger onNo={() => setUnbind(null)} onYes={() => setUnbind("pw")} />
      )}
      {unbind === "pw" && (
        <ConfirmPassword title={t("robotUnbind")} onClose={() => setUnbind(null)}
          onConfirm={async (pw) => {
            const d = await api<{ robot_wipe?: string }>("POST", `/api/admin/robots/${enc(r.robot_id)}/revoke`, { confirm_password: pw });
            setUnbind(null);
            say(t(d.robot_wipe === "sent" ? "robotUnbound" : "robotUnboundOffline", { id: label(r) }));
            onClose();
            onChanged();
          }} />
      )}
      <RecentAlerts items={r.recent_problems} />
    </aside>
  );
}

function AddRobot({ onClose, onDone, say }: { onClose: () => void; onDone: (id: string, code: string, hours: number) => void; say: Say }) {
  const [id, setId] = useState("");
  const [days, setDays] = useState(365);
  const [busy, setBusy] = useState(false);
  const [manual, setManual] = useState(false);
  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    try {
      const d = await api<{ code: string; hours: number }>("POST", "/api/admin/robots", { robot_id: id, days });
      onDone(id, d.code, d.hours);
    } catch (x) {
      say(errText(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal label={t("addRobot")} onClose={onClose}>
      <p>{t("addRobotScan")}</p>
      {!manual && (
        <div class="row end">
          <button type="button" class="btn sm" onClick={() => setManual(true)}>{t("addRobotManual")}</button>
          <button type="button" class="btn primary" onClick={onClose}>{t("close")}</button>
        </div>
      )}
      {manual && <form class="form" onSubmit={submit}>
        <p class="muted small">{t("addRobotHint")}</p>
        <label class="field">{t("robotId")}<input value={id} pattern="[A-Za-z0-9._\-]{1,64}" required autocomplete="off" onInput={(e) => setId(e.currentTarget.value)} /></label>
        <label class="field">{t("certDays")}<input type="number" min={1} max={3650} value={days} onInput={(e) => setDays(Number(e.currentTarget.value))} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={busy}>{t("addRobot")}</button>
        </div>
      </form>}
    </Modal>
  );
}

function CodeDialog({ id, code, hours, onClose, say }: { id: string; code: string; hours: number; onClose: () => void; say: Say }) {
  return (
    <Modal label={t("activationCode", { id })} onClose={onClose}>
      <p class="muted small">{t("codeHint", { h: hours })}</p>
      <textarea class="codebox mono" readOnly rows={4} value={code} onFocus={(e) => e.currentTarget.select()} aria-label={t("activationCode", { id })} />
      <div class="row end">
        <button type="button" class="btn" onClick={() => { void navigator.clipboard?.writeText(code).then(() => say(t("copied"))); }}>{t("copy")}</button>
        <button type="button" class="btn primary" onClick={onClose}>{t("close")}</button>
      </div>
    </Modal>
  );
}

function WifiChange({ id, ch, act }: {
  id: string; ch: NonNullable<Wifi["change"]>; act: (fn: () => Promise<unknown>, msg?: string) => Promise<void>;
}) {
  const send = (approve: boolean) => void act(() => api("POST", `/api/robots/${enc(id)}/wifi`, { id: ch.id, approve }),
    t(approve ? "wifiApproved" : "wifiRejected"));
  return (
    <div class="wifi-change" role="status">
      <p>{t(ch.mode === "trial" ? "wifiChangeTrial" : "wifiChangeAsk", { ssid: ch.ssid })}</p>
      <div class="row">
        <button type="button" class="btn sm primary" onClick={() => send(true)}>{t("wifiApprove")}</button>
        <button type="button" class="btn sm" onClick={() => send(false)}>{t("wifiReject")}</button>
      </div>
    </div>
  );
}

function BindApprove({ req, onClose, onDone }: { req: BindReq; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api("POST", `/api/admin/bind/${enc(req.id)}/approve`, { confirm_password: pw, name });
      onDone();
    } catch (x) {
      setErr(x instanceof ApiError && x.status === 403 ? t("pwWrong")
        : x instanceof ApiError && x.status === 429 ? t("pwLocked") : errText(x));
      setPw("");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal label={t("bindApproveTitle", { id: req.serial })} onClose={onClose}>
      <form class="form bind-approve" onSubmit={submit}>
        <p>{t("bindApproveHint", { code: req.check })}</p>
        <p class="check-digits big mono" aria-label={t("bindCheck")}>{req.check}</p>
        <label class="field">{t("robotName")}<input value={name} maxLength={32} placeholder={req.serial}
          onInput={(e) => setName(e.currentTarget.value)} /></label>
        <label class="field">{t("pwAgain")}<input type="password" autoComplete="current-password" value={pw} required
          onInput={(e) => setPw(e.currentTarget.value)} /></label>
        {err && <p class="error" role="alert">{err}</p>}
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={busy || !pw}>{t("bindApprove")}</button>
        </div>
      </form>
    </Modal>
  );
}
