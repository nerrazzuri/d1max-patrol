// 机器人页(W44 第二期,设计 §5.1「机器人」):每台一张卡片 —— 名字、在线、电量、今天巡了几趟、有没有毛病(站点
// /api/robots/overview 一次给全,今天按站点时区算)。点开是详情:改名字、软件版本、证书到期、最近的毛病、只能手动派 / 自动派、
// 重出开通码、解绑(确认 + 再输一次密码)。「添加一台机器人」先接现有登记码流程,以后改成扫机器人身上的码(W42)。
// 照片位这一期放图标;电池健康机器人还不报,不显示。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { ConfirmPassword } from "./confirmpw";
import { hm, robotNames } from "./data";
import { Confirm, Modal } from "./dialogs";
import { alertTitle, lang, t, type Key } from "./i18n";
import { Icon } from "./icons";

type Say = (s: string) => void;
export interface RobotCard {
  robot_id: string; name: string; revoked: boolean; expires_at: number; online: boolean; battery_pct: number | null; patrols_today: number;
  problems: ({ key: string; kind: string; first_ms: number } | { device: string; state: string })[];
  recent_problems: { key: string; kind: string; first_ms: number; resolved_ms: number | null }[];
  agent: string | null; compat: string; manual_only: boolean;
}

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

export function RobotsPage({ say, now }: { say: Say; now: number }) {
  const [rows, setRows] = useState<RobotCard[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<{ id: string; code: string; hours: number } | null>(null);
  void now;
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
  const cur = rows?.find((r) => r.robot_id === open) ?? null;
  return (
    <div class="robots">
      <div class="panel-head">
        <h1>{t("robots")}</h1>
        <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addRobot")}</button>
      </div>
      <p class="muted lead">{t("robotsLead")}</p>
      {rows && rows.length === 0 && <p class="empty">{t("robotsNone")}</p>}
      <div class={cur ? "robots-split" : undefined}>
        <div class="robot-cards">
          {(rows ?? []).map((r) => (
            <button key={r.robot_id} type="button" class={r.robot_id === open ? "robot-card on" : "robot-card"} data-robot={r.robot_id}
              onClick={() => setOpen(r.robot_id === open ? null : r.robot_id)}>
              <span class="robot-pic" aria-hidden="true"><Icon name="robot" /></span>
              <span class="robot-name">{label(r)}{r.name && <small class="muted mono"> {r.robot_id}</small>}</span>
              <span class={r.online ? "state tone-success" : "state tone-muted"}>{r.online ? t("online") : t("st_offline")}</span>
              <span class="robot-nums">
                <span>{r.battery_pct == null ? "—" : `${r.battery_pct}%`}</span>
                <span>{t("robotPatrolsToday", { n: r.patrols_today })}</span>
              </span>
              <span class={r.problems.length ? "robot-health warn" : "robot-health ok"}>
                {r.problems.length ? t("robotProblems", { n: r.problems.length }) : t("robotOk")}
              </span>
            </button>
          ))}
        </div>
        {cur && <RobotDetail r={cur} say={say} onChanged={load} onCode={setCode} onClose={() => setOpen(null)} />}
      </div>
      {adding && (
        <AddRobot onClose={() => setAdding(false)} say={say}
          onDone={(id, c, h) => { setAdding(false); setCode({ id, code: c, hours: h }); load(); }} />
      )}
      {code && <CodeDialog {...code} onClose={() => setCode(null)} say={say} />}
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
      </dl>
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
            await api("POST", `/api/admin/robots/${enc(r.robot_id)}/revoke`, { confirm_password: pw });
            setUnbind(null);
            say(t("robotUnbound", { id: label(r) }));
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
        <p class="muted small">{t("addRobotLater")}</p>
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
