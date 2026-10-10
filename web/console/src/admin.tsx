// 管理组(商业化 B1b,只有管理员看得到):用户、机器狗与摄像头、系统。都是站点已有的接口,
// 外加这一单新加的 /api/admin/robots(登记 / 出开通码 / 吊销)。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm, hms } from "./data";
import { Confirm, Modal } from "./dialogs";
import { t, type Key } from "./i18n";
import { Shape } from "./icons";

type Say = (s: string) => void;

function useLoad<T>(path: string): [T | null, () => void, string] {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api<T>("GET", path)
      .then((d) => {
        setData(d);
        setErr("");
      })
      .catch((e) => setErr(e instanceof ApiError ? e.message : String(e)));
  }, [path]);
  useEffect(load, [load]);
  return [data, load, err];
}

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

function day(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ------------------------------------------------------------------ 用户

interface Account { name: string; role: string; disabled: boolean; display_name: string }
interface AuditRow { id: number; at: number; actor: string; action: string; target: string; status: number }

export function UsersPage({ me, say }: { me: string; say: Say }) {
  const [data, reload] = useLoad<{ accounts: Account[] }>("/api/accounts");
  const [audit, reloadAudit] = useLoad<{ audit: AuditRow[] }>("/api/audit");
  const [adding, setAdding] = useState(false);
  const [resetFor, setResetFor] = useState<string | null>(null);
  const change = async (name: string, body: Record<string, unknown>, msg = t("saved")) => {
    try {
      await api("POST", `/api/accounts/${enc(name)}`, body);
      say(msg);
    } catch (e) {
      say(errText(e));
    }
    reload();
    reloadAudit();
  };
  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("users")}</h1>
          <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addUser")}</button>
        </div>
        <table class="grid">
          <thead><tr><th>{t("name")}</th><th>{t("displayName")}</th><th>{t("role")}</th><th>{t("status")}</th><th>{t("colActions")}</th></tr></thead>
          <tbody>
            {(data?.accounts ?? []).map((a) => (
              <tr key={a.name} class={a.disabled ? "off" : undefined}>
                <td class="mono">{a.name}</td>
                <td>{a.display_name || "—"}</td>
                <td>
                  <select aria-label={`${t("role")} ${a.name}`} value={a.role} disabled={a.name === me}
                    onChange={(e) => void change(a.name, { role: e.currentTarget.value })}>
                    {["admin", "guard", "owner"].map((r) => <option key={r} value={r}>{t(`role_${r}` as Key)}</option>)}
                  </select>
                </td>
                <td>{a.disabled ? t("disabled") : t("active")}</td>
                <td class="actions">
                  {a.name !== me && (
                    <>
                      <button type="button" class="btn sm" onClick={() => void change(a.name, { disabled: !a.disabled })}>
                        {a.disabled ? t("enable") : t("disable")}
                      </button>
                      <button type="button" class="btn sm" onClick={() => setResetFor(a.name)}>{t("resetPassword")}</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("auditLog")}</h2></div>
        <div class="scrollbox">
          <table class="grid">
            <thead><tr><th>{t("colTime")}</th><th>{t("colWho")}</th><th>{t("colAction")}</th><th>{t("colTarget")}</th><th>{t("colResult")}</th></tr></thead>
            <tbody>
              {(audit?.audit ?? []).slice(0, 100).map((r) => (
                <tr key={r.id}>
                  <td class="mono">{day(r.at)} {hms(r.at)}</td>
                  <td>{r.actor}</td>
                  <td class="mono">{r.action}</td>
                  <td>{r.target || "—"}</td>
                  <td class={r.status >= 400 ? "bad" : undefined}>{r.status || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {adding && <AddUser onClose={() => setAdding(false)} onDone={(n) => { setAdding(false); say(t("saved")); reload(); reloadAudit(); void n; }} say={say} />}
      {resetFor && (
        <PasswordDialog name={resetFor} onClose={() => setResetFor(null)}
          onSave={(pw) => { const n = resetFor; setResetFor(null); void change(n, { password: pw }, t("passwordReset", { name: n })); }} />
      )}
    </div>
  );
}

function AddUser({ onClose, onDone, say }: { onClose: () => void; onDone: (name: string) => void; say: Say }) {
  const [name, setName] = useState("");
  const [display, setDisplay] = useState("");
  const [role, setRole] = useState("guard");
  const [pw, setPw] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    try {
      await api("POST", "/api/accounts", { name, display_name: display, role, password: pw });
      onDone(name);
    } catch (x) {
      say(errText(x));
    }
  }
  return (
    <Modal label={t("addUser")} onClose={onClose}>
      <form class="form" onSubmit={submit}>
        <label class="field">{t("name")}<input value={name} pattern="[A-Za-z0-9._\-]{1,64}" required autocomplete="off" onInput={(e) => setName(e.currentTarget.value)} /></label>
        <label class="field">{t("displayName")}<input value={display} maxLength={64} onInput={(e) => setDisplay(e.currentTarget.value)} /></label>
        <label class="field">{t("role")}
          <select value={role} onChange={(e) => setRole(e.currentTarget.value)}>
            {["guard", "owner", "admin"].map((r) => <option key={r} value={r}>{t(`role_${r}` as Key)}</option>)}
          </select>
        </label>
        <label class="field">{t("newPassword")}<input type="password" minLength={10} required autocomplete="new-password" value={pw} onInput={(e) => setPw(e.currentTarget.value)} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary">{t("addUser")}</button>
        </div>
      </form>
    </Modal>
  );
}

function PasswordDialog({ name, onClose, onSave }: { name: string; onClose: () => void; onSave: (pw: string) => void }) {
  const [pw, setPw] = useState("");
  return (
    <Modal label={`${t("resetPassword")}: ${name}`} onClose={onClose}>
      <form class="form" onSubmit={(e) => { e.preventDefault(); onSave(pw); }}>
        <label class="field">{t("newPassword")}<input type="password" minLength={10} required autocomplete="new-password" value={pw} onInput={(e) => setPw(e.currentTarget.value)} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary">{t("save")}</button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ 机器狗与摄像头

interface AdminRobot {
  robot_id: string;
  revoked: boolean;
  expires_at: number;
  manual_only: unknown;
  status?: { online: boolean } | null;
  capabilities?: { agent?: string } | null;
}
interface Versions { site: { version: string }; robots: { robot_id: string; verdict: string; sidecar_proto?: number | null }[] }
interface CameraRow { name: string; zone: string; connected: boolean; error: string }

export function RobotsAdminPage({ say }: { say: Say }) {
  const [robots, reload] = useLoad<{ robots: AdminRobot[] }>("/api/robots");
  const [versions] = useLoad<Versions>("/api/versions");
  const [cams] = useLoad<{ cameras: CameraRow[] }>("/api/cameras");
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<{ id: string; code: string; hours: number } | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const verdict = new Map((versions?.robots ?? []).map((v) => [v.robot_id, v.verdict]));
  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try {
      await fn();
      if (msg) say(msg);
    } catch (e) {
      say(errText(e));
    }
    reload();
  };
  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("robots")}</h1>
          <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addRobot")}</button>
        </div>
        <table class="grid">
          <thead><tr><th>{t("robotId")}</th><th>{t("colOnline")}</th><th>{t("colVersion")}</th><th>{t("colCompat")}</th><th>{t("dispatchMode")}</th><th>{t("certExpires")}</th><th>{t("colActions")}</th></tr></thead>
          <tbody>
            {(robots?.robots ?? []).map((r) => {
              const v = verdict.get(r.robot_id) ?? "unknown";
              return (
                <tr key={r.robot_id} class={r.revoked ? "off" : undefined}>
                  <td class="mono strong">{r.robot_id}</td>
                  <td>{r.revoked ? t("revokedTag") : r.status?.online ? t("online") : <span class="state p2"><Shape kind="P2" />{t("st_offline")}</span>}</td>
                  <td class="mono">{r.capabilities?.agent ?? "—"}</td>
                  <td>{v === "ok" ? t("compat_ok") : <span class="state p2"><Shape kind="P2" />{t(`compat_${v}` as Key)}</span>}</td>
                  <td>{r.manual_only ? t("manualOnly") : t("autoDispatch")}</td>
                  <td class="mono">{r.expires_at ? day(r.expires_at) : "—"}</td>
                  <td class="actions">
                    {!r.revoked && (
                      <>
                        <button type="button" class="btn sm" onClick={() => void act(() => api("POST", `/api/robots/${enc(r.robot_id)}/service`, { manual_only: !r.manual_only }))}>
                          {r.manual_only ? t("setAuto") : t("setManual")}
                        </button>
                        <button type="button" class="btn sm" onClick={() => void act(async () => {
                          const d = await api<{ code: string; hours: number }>("POST", `/api/admin/robots/${enc(r.robot_id)}/code`, {});
                          setCode({ id: r.robot_id, code: d.code, hours: d.hours });
                        })}>{t("newCode")}</button>
                        <button type="button" class="btn sm" onClick={() => setRevoking(r.robot_id)}>{t("revoke")}</button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("cameras")}</h2><span class="muted small push">{t("camerasHint")}</span></div>
        <table class="grid">
          <thead><tr><th>{t("cameraName")}</th><th>{t("colZone")}</th><th>{t("colOnline")}</th></tr></thead>
          <tbody>
            {cams && cams.cameras.length === 0 && <tr><td colSpan={3} class="muted">{t("noCameras")}</td></tr>}
            {(cams?.cameras ?? []).map((c) => (
              <tr key={c.name}>
                <td class="mono">{c.name}</td><td>{c.zone}</td>
                <td>{c.connected ? t("connected") : <span class="state p2"><Shape kind="P2" />{t("notConnected")}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {adding && (
        <AddRobot onClose={() => setAdding(false)} say={say}
          onDone={(id, c, h) => { setAdding(false); setCode({ id, code: c, hours: h }); reload(); }} />
      )}
      {code && <CodeDialog {...code} onClose={() => setCode(null)} say={say} />}
      {revoking && (
        <Confirm text={t("revokeConfirm", { id: revoking })} yes={t("revoke")} danger onNo={() => setRevoking(null)}
          onYes={() => {
            const id = revoking;
            setRevoking(null);
            void act(async () => {
              const d = await api<{ summary: string; broker_restart: string }>("POST", `/api/admin/robots/${enc(id)}/revoke`, {});
              say(t("revoked", { id, summary: d.summary, cmd: d.broker_restart }));
            });
          }} />
      )}
    </div>
  );
}

function AddRobot({ onClose, onDone, say }: { onClose: () => void; onDone: (id: string, code: string, hours: number) => void; say: Say }) {
  const [id, setId] = useState("");
  const [days, setDays] = useState(365);
  const [busy, setBusy] = useState(false);
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
      <form class="form" onSubmit={submit}>
        <p class="muted small">{t("addRobotHint")}</p>
        <label class="field">{t("robotId")}<input value={id} pattern="[A-Za-z0-9._\-]{1,64}" required autocomplete="off" onInput={(e) => setId(e.currentTarget.value)} /></label>
        <label class="field">{t("certDays")}<input type="number" min={1} max={3650} value={days} onInput={(e) => setDays(Number(e.currentTarget.value))} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={busy}>{t("addRobot")}</button>
        </div>
      </form>
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

// ------------------------------------------------------------------ 系统

interface Health { ok: boolean; version: string; checks: Record<string, { ok: boolean; detail?: string }> }

const CRITICAL = new Set(["db", "loop", "broker", "disk"]);

export function SystemPage() {
  const [health, reload] = useLoad<Health>("/api/health");
  const [versions] = useLoad<Versions>("/api/versions");
  useEffect(() => {
    const id = window.setInterval(reload, 30_000);
    return () => window.clearInterval(id);
  }, [reload]);
  const checks = Object.entries(health?.checks ?? {});
  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("health")}</h1>
          {health && (
            <span class={health.ok ? "muted" : "state p2"}>{!health.ok && <Shape kind="P2" />}{health.ok ? t("healthOk") : t("healthBad")}</span>
          )}
          <span class="muted small push">{health ? t("siteVersion", { v: health.version }) : ""} {health ? hm(Date.now()) : ""}</span>
        </div>
        <table class="grid">
          <tbody>
            {checks.map(([k, c]) => (
              <tr key={k}>
                <td class="strong">{t(`check_${k}` as Key)}{CRITICAL.has(k) ? <span class="muted small"> · {t("critical")}</span> : null}</td>
                <td>{c.ok ? "OK" : <span class={CRITICAL.has(k) ? "state p1" : "state p2"}><Shape kind={CRITICAL.has(k) ? "P1" : "P2"} />{t("checkFail")}</span>}</td>
                <td class="muted small">{c.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("versions")}</h2></div>
        <table class="grid">
          <tbody>
            {(versions?.robots ?? []).map((r) => (
              <tr key={r.robot_id}><td class="mono strong">{r.robot_id}</td><td>{t(`compat_${r.verdict}` as Key)}</td><td class="muted small">{r.sidecar_proto != null ? `sidecar ${r.sidecar_proto}` : ""}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>{t("supportBundle")}</h2>
          <a class="btn push" href="/api/support-bundle" download="d1max-support.tar.gz">{t("supportBundle")}</a>
        </div>
        <p class="muted small pad">{t("supportHint")}</p>
      </section>
    </div>
  );
}
