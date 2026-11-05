// POC 第 5 项:路线页「派一趟」。挑一台机器人 → 站点估预计多久、现在走还是排在日程后面 → 确认了才派
// (走现成的临时巡逻:撞日程就排队、通知经理和管理员)。
import { useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Modal } from "./dialogs";
import { t } from "./i18n";
import { siteHm } from "./todaypatrols";

interface Plan { estimate_s?: number | null; start_ms?: number; queued?: boolean; because?: string; utc_offset_min?: number }
interface Sent { queued?: boolean; start_after_ms?: number; because?: string; utc_offset_min?: number; ack?: { result?: string; reason?: string } }

export function DispatchOnce({ route, say }: { route: string; say: (s: string) => void }) {
  const [open, setOpen] = useState(false);
  const [robots, setRobots] = useState<string[] | null>(null);
  const [robot, setRobot] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const why = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
  const start = () => {
    setOpen(true);
    setRobot("");
    setPlan(null);
    setErr(null);
    api<{ robots: { robot_id: string; fresh?: boolean; revoked?: boolean }[] }>("GET", "/api/robots")
      .then((d) => setRobots(d.robots.filter((r) => !r.revoked && r.fresh !== false).map((r) => r.robot_id)))
      .catch((e) => { setRobots([]); setErr(why(e)); });
  };
  const pick = (id: string) => {
    setRobot(id);
    setPlan(null);
    setErr(null);
    if (!id) return;
    api<Plan>("GET", `/api/robots/${enc(id)}/patrol-plan?mission_id=${encodeURIComponent(route)}`)
      .then(setPlan).catch((e) => setErr(why(e)));
  };
  const go = async () => {
    setBusy(true);
    try {
      const r = await api<Sent>("POST", `/api/robots/${enc(robot)}/patrol`, { mission_id: route });
      if (r.queued) say(t("trQueued", { id: robot, at: siteHm(r.start_after_ms ?? 0, r.utc_offset_min), why: r.because ?? "" }));
      else if (r.ack && r.ack.result !== "accepted" && r.ack.result !== "duplicate") {
        setErr(t("rdNotAccepted", { why: r.ack.reason || r.ack.result || "" }));
        return;
      } else say(t("rdPatrolSent", { id: robot, route }));
      setOpen(false);
    } catch (e) {
      setErr(why(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" class="btn" data-dispatch-once="1" onClick={start}>{t("doBtn")}</button>
      {open && (
        <Modal label={t("doTitle", { route })} onClose={() => setOpen(false)}>
          <div class="route-dispatch">
            <label class="field">{t("doRobot")}
              <select name="robot" value={robot} onChange={(e) => pick(e.currentTarget.value)}>
                <option value="">{t("doPickRobot")}</option>
                {(robots ?? []).map((id) => <option key={id} value={id}>{id}</option>)}
              </select>
            </label>
            {robots !== null && robots.length === 0 && <p class="muted small">{t("doNoRobot")}</p>}
            {plan && (
              <p class="small" data-plan="1">
                {plan.estimate_s != null && <>{t("doEta", { m: Math.max(1, Math.round(plan.estimate_s / 60)) })} · </>}
                {plan.queued
                  ? t("doQueued", { at: siteHm(plan.start_ms ?? 0, plan.utc_offset_min), why: plan.because ?? "" })
                  : t("doNow")}
              </p>
            )}
            {err && <p class="error small" role="alert">{err}</p>}
            <div class="row end">
              <button type="button" class="btn" onClick={() => setOpen(false)}>{t("cancel")}</button>
              <button type="button" class="btn primary" disabled={!plan || busy} onClick={() => void go()}>
                {plan?.queued ? t("doQueueIt") : t("doSend")}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
