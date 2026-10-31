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
import { alertTitle, lang, t, type Key } from "./i18n";
import { Shape } from "./icons";
import { StateText, type UnitInfo } from "./live";

type Say = (s: string) => void;
export interface RobotCard {
  robot_id: string; name: string; revoked: boolean; expires_at: number; online: boolean; battery_pct: number | null; patrols_today: number;
  problems: ({ key: string; kind: string; first_ms: number } | { device: string; state: string })[];
  recent_problems: { key: string; kind: string; first_ms: number; resolved_ms: number | null }[];
  agent: string | null; compat: string; manual_only: boolean;
  wifi?: Wifi | null;
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
  return "kind" in p ? alertTitle(lang.value, p.kind, p.kind) : t("robotDeviceBad", { d: p.device });
}

/** 定位怎么样(W60 卡片第二层):没报 = 「—」;变差、不可信 = 变差;没锚定 = 未定位。 */
export function locText(u: UnitInfo | undefined): Key | null {
  const l = u?.robot.loc;
  if (!u || u.stale || !l) return null;
  if (l.degraded || l.reason) return "locPoor";
  if (l.anchored === false || !l.pose) return "locNone";
  return "locGood";
}

export function RobotsPage({ say, now, units = [] }: { say: Say; now: number; units?: UnitInfo[] }) {
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
        <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addRobot")}</button>
      </div>
      <p class="muted lead">{t("robotsLead")}</p>
      {pending.length > 0 && (
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
        {cur && <RobotDetail r={cur} say={say} onChanged={load} onCode={setCode} onClose={() => setOpen(null)} />}
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

function RobotDetail({ r, say, onChanged, onCode, onClose }: {
  r: RobotCard; say: Say; onChanged: () => void; onCode: (c: { id: string; code: string; hours: number }) => void; onClose: () => void;
}) {
  const [name, setName] = useState(r.name);
  const [unbind, setUnbind] = useState<"ask" | "pw" | null>(null);
  useEffect(() => setName(r.name), [r.robot_id, r.name]);
  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try {
      await fn();
      if (msg) say(msg);
    } catch (e) {
      say(errText(e));
    }
    onChanged();
  };
  const compat: Key = r.compat === "ok" ? "compat_ok" : (`compat_${r.compat}` as Key);
  return (
    <aside class="panel robot-detail" aria-label={label(r)}>
      <div class="panel-head">
        <h2>{label(r)}</h2>
        <button type="button" class="btn sm push" onClick={onClose}>{t("close")}</button>
      </div>
      <form class="row" onSubmit={(e) => { e.preventDefault(); void act(() => api("POST", `/api/admin/robots/${enc(r.robot_id)}/name`, { name }), t("saved")); }}>
        <label class="field grow">{t("robotName")}<input name="name" value={name} maxLength={32} placeholder={r.robot_id}
          onInput={(e) => setName(e.currentTarget.value)} /></label>
        <button type="submit" class="btn" disabled={name === r.name}>{t("robotNameSave")}</button>
      </form>
      <dl class="kv small">
        <dt>{t("robotId")}</dt><dd class="mono">{r.robot_id}</dd>
        <dt>{t("colVersion")}</dt><dd>{r.agent ?? "—"} · {t(compat)}</dd>
        <dt>{t("certExpires")}</dt><dd class="mono">{r.expires_at ? day(r.expires_at) : "—"}</dd>
        <dt>{t("dispatchMode")}</dt><dd>{r.manual_only ? t("manualOnly") : t("autoDispatch")}</dd>
        {r.wifi && <><dt>{t("wifiLabel")}</dt><dd>{r.wifi.ssid || "—"} · {t(`link_${r.wifi.link}` as Key)}</dd></>}
      </dl>
      {r.wifi?.change && <WifiChange id={r.robot_id} ch={r.wifi.change} act={act} />}
      <h3>{t("robotRecent")}</h3>
      {r.recent_problems.length === 0 ? <p class="muted small">{t("robotRecentNone")}</p> : (
        <ul class="plain">
          {r.recent_problems.map((p) => (
            <li key={p.key}><time class="mono small">{day(p.first_ms)} {hm(p.first_ms)}</time> {alertTitle(lang.value, p.kind, p.kind)}
              {p.resolved_ms == null && <span class="tag">{t("robotOpen")}</span>}</li>
          ))}
        </ul>
      )}
      {r.problems.some((p) => "device" in p) && (
        <p class="small">{r.problems.filter((p) => "device" in p).map(problemText).join(" · ")}</p>
      )}
      <div class="row wrap">
        <button type="button" class="btn sm" onClick={() => void act(() => api("POST", `/api/robots/${enc(r.robot_id)}/service`, { manual_only: !r.manual_only }))}>
          {r.manual_only ? t("setAuto") : t("setManual")}
        </button>
        <button type="button" class="btn sm" onClick={() => void act(async () => {
          const d = await api<{ code: string; hours: number }>("POST", `/api/admin/robots/${enc(r.robot_id)}/code`, {});
          onCode({ id: r.robot_id, code: d.code, hours: d.hours });
        })}>{t("newCode")}</button>
        <button type="button" class="btn sm stop" onClick={() => setUnbind("ask")}>{t("robotUnbind")}</button>
      </div>
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
