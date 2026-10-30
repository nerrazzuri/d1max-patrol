// 用户页(W44 第二期,设计 §5.1「用户」;只给管理员,工程师不能管账号):一张表 —— 名字、角色(界面名字)、最后登录、
// 状态、改角色 / 停用 / 重设密码 / 删除。「给工程师开临时账号」单独一个按钮:4 或 24 小时,再输一次密码,口令只显示这一次;
// 工程师那一行写还剩多久,只能提前关、删。点一个人看他做过什么(审计)。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { ConfirmPassword } from "./confirmpw";
import { hm, hms } from "./data";
import { Confirm, Modal } from "./dialogs";
import { t } from "./i18n";
import { roleLabel } from "./shell";

type Say = (s: string) => void;
interface Account { name: string; role: string; disabled: boolean; display_name: string; expires_ms: number | null; last_login_ms: number | null; expired: boolean }
interface AuditRow { id: number; at: number; actor: string; actor_role?: string | null; action: string; target: string; status: number }

const ROLES = ["admin", "guard", "owner", "viewer"] as const;

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

function day(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** 最后登录:没登录过 / 今天 08:12 / 昨天 / 3 天前。 */
export function lastLoginText(ms: number | null, now: number): string {
  if (ms == null) return t("loginNever");
  if (day(ms) === day(now)) return t("loginToday", { t: hm(ms) });
  const days = Math.max(1, Math.round((new Date(day(now)).getTime() - new Date(day(ms)).getTime()) / 86_400_000));
  return days === 1 ? t("loginYesterday") : t("loginDaysAgo", { n: days });
}

/** 工程师还剩多久。 */
export function leftText(until: number, now: number): string {
  const m = Math.max(0, Math.round((until - now) / 60_000));
  return t("engLeft", { h: Math.floor(m / 60), m: m % 60 });
}

/** 审计里的动作翻成人话;认不得的原样。 */
const ACTIONS: [RegExp, string][] = [
  [/^login/, "actLogin"], [/^POST \/api\/logout/, "actLogout"], [/^POST \/api\/mode$/, "actMode"],
  [/^POST \/api\/alerts\/.+\/resolve$/, "actResolve"], [/^POST \/api\/alerts\/.+\/ack$/, "actAck"],
  [/^POST \/api\/robots\/[^/]+\/(goto|patrol)$/, "actDispatch"], [/^POST \/api\/robots\/[^/]+\/halt$/, "actHalt"],
  [/^POST \/api\/accounts\/engineer$/, "actEngineer"], [/^POST \/api\/accounts/, "actAccounts"],
  [/^POST \/api\/lists\/authorizations/, "actAuthz"], [/^POST \/api\/arming\//, "actSchedule"],
  [/^POST \/api\/shift-notes$/, "actNote"], [/^POST \/api\/routes/, "actRoutes"], [/^POST \/api\/(zones|nav-zones|maps)/, "actMaps"],
];
export function actionText(a: string): string {
  const hit = ACTIONS.find(([re]) => re.test(a));
  return hit ? t(hit[1] as Parameters<typeof t>[0]) : a;
}

function useAccounts(say: Say): [Account[], () => void, (a: Account[]) => void] {
  const [rows, setRows] = useState<Account[]>([]);
  const load = useCallback(() => {
    api<{ accounts: Account[] }>("GET", "/api/accounts").then((d) => setRows(d.accounts ?? [])).catch((e) => say(errText(e)));
  }, []);
  useEffect(load, [load]);
  return [rows, load, setRows];
}

export function UsersPage({ me, say, now }: { me: string; say: Say; now: number }) {
  const [rows, reload, setRows] = useAccounts(say);
  const [adding, setAdding] = useState(false);
  const [eng, setEng] = useState<"form" | { display: string; hours: number } | null>(null);
  const [made, setMade] = useState<{ name: string; password: string; expires_ms: number } | null>(null);
  const [resetFor, setResetFor] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Account | null>(null);
  const [who, setWho] = useState<string | null>(null);

  const change = async (name: string, body: Record<string, unknown>, msg = t("saved")) => {
    try {
      const d = await api<{ accounts: Account[] }>("POST", `/api/accounts/${enc(name)}`, body);
      if (d.accounts) setRows(d.accounts);
      say(msg);
    } catch (e) {
      say(errText(e));
      reload();
    }
  };

  return (
    <div class="users">
      <div class="panel-head">
        <h1>{t("users")}</h1>
        <button type="button" class="btn push" onClick={() => setEng("form")}>{t("engOpen")}</button>
        <button type="button" class="btn primary" onClick={() => setAdding(true)}>{t("addUser")}</button>
      </div>
      <p class="muted lead">{t("usersLead")}</p>
      <Requests say={say} onDone={reload} />
      <div class={who ? "users-split" : undefined}>
        <table class="grid users-table">
          <thead><tr><th>{t("name")}</th><th>{t("role")}</th><th>{t("lastLogin")}</th><th>{t("status")}</th><th>{t("colActions")}</th></tr></thead>
          <tbody>
            {rows.map((a) => {
              const isEng = a.role === "engineer";
              const self = a.name === me;
              const off = a.disabled || a.expired;
              return (
                <tr key={a.name} data-name={a.name} class={off ? "off" : undefined}>
                  <td>
                    <button type="button" class="linklike user-name" onClick={() => setWho(a.name)}>{a.display_name || a.name}</button>
                    <div class="muted small mono">{a.name}</div>
                  </td>
                  <td>
                    {isEng || self ? t(roleLabel(a.role)) : (
                      <select aria-label={`${t("role")} ${a.name}`} value={a.role} onChange={(e) => void change(a.name, { role: e.currentTarget.value })}>
                        {ROLES.map((r) => <option key={r} value={r}>{t(roleLabel(r))}</option>)}
                      </select>
                    )}
                  </td>
                  <td>{lastLoginText(a.last_login_ms, now)}</td>
                  <td>
                    {isEng && !off && a.expires_ms ? leftText(a.expires_ms, now)
                      : a.expired ? t("engExpired") : a.disabled ? t("disabled") : t("active")}
                  </td>
                  <td class="actions">
                    {isEng ? (
                      <>
                        {!off && <button type="button" class="btn sm" onClick={() => void change(a.name, { disabled: true }, t("engClosed"))}>{t("engClose")}</button>}
                        <button type="button" class="btn sm" onClick={() => setRemoving(a)}>{t("remove")}</button>
                      </>
                    ) : !self && (
                      <>
                        <button type="button" class="btn sm" onClick={() => void change(a.name, { disabled: !a.disabled })}>{a.disabled ? t("enable") : t("disable")}</button>
                        <button type="button" class="btn sm" onClick={() => setResetFor(a.name)}>{t("resetPassword")}</button>
                        <button type="button" class="btn sm" onClick={() => setRemoving(a)}>{t("remove")}</button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {who && <UserAudit name={who} account={rows.find((r) => r.name === who)} onClose={() => setWho(null)} />}
      </div>

      {adding && <AddUser onClose={() => setAdding(false)} say={say} onDone={() => { setAdding(false); say(t("saved")); reload(); }} />}
      {eng === "form" && <EngineerForm onClose={() => setEng(null)} onNext={(display, hours) => setEng({ display, hours })} />}
      {eng && eng !== "form" && (
        <ConfirmPassword title={t("engOpen")} onClose={() => setEng(null)}
          onConfirm={async (pw) => {
            const d = await api<{ name: string; password: string; expires_ms: number; accounts: Account[] }>(
              "POST", "/api/accounts/engineer", { display_name: eng.display, hours: eng.hours, confirm_password: pw });
            setEng(null);
            setMade({ name: d.name, password: d.password, expires_ms: d.expires_ms });
            if (d.accounts) setRows(d.accounts);
          }} />
      )}
      {made && (
        <Modal label={t("engMade")} onClose={() => setMade(null)}>
          <p class="muted small">{t("engMadeHint", { t: `${day(made.expires_ms)} ${hm(made.expires_ms)}` })}</p>
          <dl class="kv">
            <dt>{t("name")}</dt><dd class="mono">{made.name}</dd>
            <dt>{t("password")}</dt><dd class="mono">{made.password}</dd>
          </dl>
          <p class="warn">{t("engOnce")}</p>
          <div class="row end">
            <button type="button" class="btn" onClick={() => { void navigator.clipboard?.writeText(`${made.name}\n${made.password}`).then(() => say(t("copied"))); }}>{t("copy")}</button>
            <button type="button" class="btn primary" onClick={() => setMade(null)}>{t("close")}</button>
          </div>
        </Modal>
      )}
      {resetFor && (
        <PasswordDialog name={resetFor} onClose={() => setResetFor(null)}
          onSave={(pw) => { const n = resetFor; setResetFor(null); void change(n, { password: pw }, t("passwordReset", { name: n })); }} />
      )}
      {removing && (
        <Confirm text={t("userRemoveConfirm", { name: removing.display_name || removing.name })} yes={t("remove")} danger onNo={() => setRemoving(null)}
          onYes={() => {
            const a = removing;
            setRemoving(null);
            api<{ accounts: Account[] }>("POST", `/api/accounts/${enc(a.name)}/remove`, {})
              .then((d) => { setRows(d.accounts ?? []); if (who === a.name) setWho(null); say(t("saved")); })
              .catch((e) => say(errText(e)));
          }} />
      )}
    </div>
  );
}

function UserAudit({ name, account, onClose }: { name: string; account?: Account; onClose: () => void }) {
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  useEffect(() => {
    setRows(null);
    api<{ audit: AuditRow[] }>("GET", `/api/audit?actor=${encodeURIComponent(name)}`).then((d) => setRows(d.audit ?? [])).catch(() => setRows([]));
  }, [name]);
  return (
    <aside class="panel user-audit" aria-label={t("userDid", { name })}>
      <div class="panel-head">
        <h2>{t("userDid", { name: account?.display_name || name })}</h2>
        <button type="button" class="btn sm push" onClick={onClose}>{t("close")}</button>
      </div>
      {rows == null ? <p class="muted">…</p> : rows.length === 0 ? <p class="muted">{t("userDidNone")}</p> : (
        <ul class="plain">
          {rows.slice(0, 200).map((r) => (
            <li key={r.id} class={r.status >= 400 ? "bad" : undefined}>
              <time class="mono small">{day(r.at)} {hms(r.at)}</time>
              <span>{actionText(r.action)}</span>
              {r.target && <span class="muted small">{r.target}</span>}
              {r.actor_role === "engineer" && <span class="tag">{t("roleEngineer")}</span>}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}

function EngineerForm({ onClose, onNext }: { onClose: () => void; onNext: (display: string, hours: number) => void }) {
  const [display, setDisplay] = useState("");
  const [hours, setHours] = useState(4);
  return (
    <Modal label={t("engOpen")} onClose={onClose}>
      <form class="form eng-form" onSubmit={(e) => { e.preventDefault(); if (display.trim()) onNext(display.trim(), hours); }}>
        <p class="muted small">{t("engHint")}</p>
        <label class="field">{t("engWho")}<input name="display" value={display} maxLength={64} required onInput={(e) => setDisplay(e.currentTarget.value)} /></label>
        <fieldset class="checks row-wrap">
          <legend>{t("engFor")}</legend>
          {[4, 24].map((h) => (
            <label key={h}><input type="radio" name="hours" value={String(h)} checked={hours === h} onChange={() => setHours(h)} /> {t("accHours", { n: h })}</label>
          ))}
        </fieldset>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!display.trim()}>{t("engNext")}</button>
        </div>
      </form>
    </Modal>
  );
}

function AddUser({ onClose, onDone, say }: { onClose: () => void; onDone: () => void; say: Say }) {
  const [name, setName] = useState("");
  const [display, setDisplay] = useState("");
  const [role, setRole] = useState("guard");
  const [pw, setPw] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    try {
      await api("POST", "/api/accounts", { name, display_name: display, role, password: pw });
      onDone();
    } catch (x) {
      say(errText(x));
    }
  }
  return (
    <Modal label={t("addUser")} onClose={onClose}>
      <form class="form" onSubmit={submit}>
        <label class="field">{t("displayName")}<input value={display} maxLength={64} onInput={(e) => setDisplay(e.currentTarget.value)} /></label>
        <label class="field">{t("accountName")}<input value={name} pattern="[A-Za-z0-9._\-]{1,64}" required autocomplete="off" onInput={(e) => setName(e.currentTarget.value)} /></label>
        <label class="field">{t("role")}
          <select value={role} onChange={(e) => setRole(e.currentTarget.value)}>
            {(["guard", "owner", "admin"] as const).map((r) => <option key={r} value={r}>{t(roleLabel(r))}</option>)}
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

/** W52:App 上申请的账号,等管理员批准(选权限,默认只看;批准再输一次密码)。没有就不占地方。 */
interface AccountRequest { id: string; name: string; display: string; created_ms: number }

export function Requests({ say, onDone }: { say: Say; onDone: () => void }) {
  const [rows, setRows] = useState<AccountRequest[]>([]);
  const [roles, setRoles] = useState<Record<string, string>>({});
  const [approving, setApproving] = useState<AccountRequest | null>(null);
  const load = useCallback(() => {
    api<{ requests: AccountRequest[] }>("GET", "/api/account-requests").then((d) => setRows(d.requests ?? [])).catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);
  if (rows.length === 0) return null;
  const roleOf = (r: AccountRequest) => roles[r.id] ?? "viewer";
  return (
    <section class="panel requests" aria-label={t("reqTitle")}>
      <h2>{t("reqTitle")}</h2>
      <table class="grid">
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} data-req={r.id}>
              <td><b>{r.display}</b><div class="muted small mono">{r.name}</div></td>
              <td class="muted small">{hm(r.created_ms)}</td>
              <td>
                <select aria-label={`${t("role")} ${r.name}`} value={roleOf(r)} onChange={(e) => setRoles({ ...roles, [r.id]: e.currentTarget.value })}>
                  {(["viewer", "guard", "owner", "admin"] as const).map((x) => <option key={x} value={x}>{t(roleLabel(x))}</option>)}
                </select>
              </td>
              <td class="actions">
                <button type="button" class="btn sm" onClick={async () => {
                  try { await api("POST", `/api/account-requests/${enc(r.id)}/reject`, {}); say(t("reqRejected", { name: r.name })); }
                  catch (e) { say(errText(e)); }
                  load();
                }}>{t("reqReject")}</button>
                <button type="button" class="btn sm primary" onClick={() => setApproving(r)}>{t("reqApprove")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {approving && (
        <ConfirmPassword title={t("reqApproveTitle", { name: approving.display, role: t(roleLabel(roleOf(approving))) })}
          onClose={() => setApproving(null)}
          onConfirm={async (pw) => {
            await api("POST", `/api/account-requests/${enc(approving.id)}/approve`, { role: roleOf(approving), confirm_password: pw });
            say(t("reqApproved", { name: approving.name }));
            setApproving(null);
            load();
            onDone();
          }} />
      )}
    </section>
  );
}
