// POC 第 8 项:告警测试模式。开着的时候谁都看得到条幅(测试期间的告警标题带「测试：」);管理员能开
// (最长 2 小时)、结束并清掉带测试标记的告警(要确认),或者只结束、留着记录。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import { Confirm } from "./dialogs";
import { t } from "./i18n";
import { isOwnerLike } from "./model";

interface Mode { active: boolean; until_ms: number | null; by: string }
const hm = (ms: number) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };

/** 条幅多久重新问一次站点(外审 r1 #10:别的管理员开了、结束了,这边也要跟着变)。 */
export const ALERT_TEST_POLL_MS = 15_000;

export function AlertTestBar({ role, onCleared, pollMs = ALERT_TEST_POLL_MS }:
  { role: string; onCleared: () => void; pollMs?: number }) {
  const [mode, setMode] = useState<Mode | null>(null);
  const [mins, setMins] = useState(30);
  const [ask, setAsk] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const admin = isOwnerLike(role);
  const load = () => api<Mode>("GET", "/api/alert-test").then(setMode).catch(() => setMode(null));
  const [, setTick] = useState(0);
  useEffect(() => {
    void load();
    const h = setInterval(() => void load(), pollMs);
    return () => clearInterval(h);
  }, [pollMs]);
  // 到点就当已结束(不等下一次轮询):测试结束之后的真告警不能被当成演练
  useEffect(() => {
    if (!mode?.active || mode.until_ms == null) return;
    const h = setTimeout(() => { setTick((n) => n + 1); void load(); }, Math.max(0, mode.until_ms - Date.now()) + 50);
    return () => clearTimeout(h);
  }, [mode?.active, mode?.until_ms]);
  const active = !!mode?.active && (mode.until_ms == null || Date.now() < mode.until_ms);
  const post = async (body: Record<string, unknown>) => {
    setMsg(null);
    try {
      const r = await api<Mode & { cleared?: number }>("POST", "/api/alert-test", body);
      setMode(r);
      if (r.cleared != null) { setMsg(t("atCleared", { n: r.cleared })); onCleared(); }
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  };
  if (!mode || (!active && !admin)) return null;
  return (
    <div class={active ? "note warn alert-test" : "alert-test row"} data-alert-test={active ? "on" : "off"}>
      {active ? (
        <>
          <span>{t("atOn", { at: hm(mode.until_ms ?? 0), by: mode.by })}</span>
          {admin && <button type="button" class="btn sm stop" onClick={() => setAsk(true)}>{t("atEndClear")}</button>}
          {admin && <button type="button" class="btn sm" onClick={() => void post({ action: "end", clear: false })}>{t("atEndKeep")}</button>}
        </>
      ) : (
        <>
          <label class="field-inline small">{t("atLen")}
            <select name="minutes" value={String(mins)} onChange={(e) => setMins(Number(e.currentTarget.value))}>
              {[15, 30, 60, 120].map((m) => <option key={m} value={String(m)}>{t("atMin", { n: m })}</option>)}
            </select>
          </label>
          <button type="button" class="btn sm" onClick={() => void post({ action: "start", minutes: mins })}>{t("atStart")}</button>
        </>
      )}
      {msg && <span class="small" role="status">{msg}</span>}
      {ask && <Confirm text={t("atEndClearAsk")} yes={t("atEndClear")} danger onNo={() => setAsk(false)}
        onYes={() => { setAsk(false); void post({ action: "end", clear: true }); }} />}
    </div>
  );
}
