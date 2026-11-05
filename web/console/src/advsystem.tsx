// 高级 → 系统(W44 第四期,设计 §5.3「系统」):站点名字、时间、存储、证书、证据加密、手机配对;外网和 VPN(W45 还没做,
// 先写明还没装);备份 —— 最近一次备份和校验(站点体检的备份那一项),超过 7 天没成功写红。备份目录在站点主机上配,网页只看。
import { useEffect, useState } from "preact/hooks";
import { useLoad, PairPhone } from "./admin";
import { api, ApiError } from "./api";
import type { Me } from "./app";
import { hms } from "./data";
import { t, type Key } from "./i18n";
import { SiteMetrics, SiteOps } from "./remoteops";

interface Health { ok: boolean; version: string; checks: Record<string, { ok: boolean; detail?: string }> }

function Row({ k, check }: { k: Key; check?: { ok: boolean; detail?: string } }) {
  return (
    <div class={check && !check.ok ? "sys-row bad" : "sys-row"}>
      <span class="muted">{t(k)}</span>
      <b>{check ? (check.detail || (check.ok ? t("sysOk") : t("checkFail"))) : "—"}</b>
    </div>
  );
}

export function SystemPage2({ me, now, say }: { me: Me; now: number; say: (s: string) => void }) {
  const [health] = useLoad<Health>("/api/health");
  const c = health?.checks ?? {};
  return (
    <section class="advsystem">
      <h1>{t("tabSystem")}</h1>
      <p class="muted lead">{t("sysWhen")}</p>
      <section class="panel">
        <h2>{t("sysSite")}</h2>
        <div class="sys-row"><span class="muted">{t("sysName")}</span><b>{me.site_name || "—"}</b></div>
        <div class="sys-row"><span class="muted">{t("sysTime")}</span><b class="mono">{new Date(now).toLocaleDateString("sv-SE")} {hms(now)}</b></div>
        <div class="sys-row"><span class="muted">{t("sysVersion")}</span><b class="mono">{health?.version ?? "—"}</b></div>
        <Row k="sysDisk" check={c.disk} />
        <Row k="sysCerts" check={c.certs} />
        <Row k="sysKeys" check={c.keys} />
      </section>
      <section class="panel sys-backup">
        <h2>{t("sysBackup")}</h2>
        <Row k="sysBackupLast" check={c.backup} />
        <p class="muted small">{t("sysBackupHint")}</p>
      </section>
      <TeleopSpeeds say={say} />
      <SiteOps say={say} />
      <SiteMetrics />
      <PairPhone />
    </section>
  );
}

type Tier = "slow" | "mid" | "fast";
const TIERS: Tier[] = ["slow", "mid", "fast"];
const TIER_KEY: Record<Tier, Key> = { slow: "tsSlow", mid: "tsMid", fast: "tsFast" };
/** 速度档上限(m/s),跟站点、机器人一致。 */
const SPEED_MAX = 1.5;

/** POC 遥控速度档:手机摇杆满推按哪一档走。管理员改;要 0 < 慢 ≤ 中 ≤ 快 ≤ 1.5。 */
function TeleopSpeeds({ say }: { say: (s: string) => void }) {
  const [got] = useLoad<Record<Tier, number>>("/api/teleop-speeds");
  const [v, setV] = useState<Record<Tier, string> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (got) setV({ slow: String(got.slow), mid: String(got.mid), fast: String(got.fast) }); }, [got]);
  if (!v) return null;
  const n = TIERS.map((k) => Number(v[k]));
  const ok = n.every((x) => Number.isFinite(x)) && n[0] > 0 && n[0] <= n[1] && n[1] <= n[2] && n[2] <= SPEED_MAX;
  const save = async () => {
    setErr(null);
    try {
      await api("POST", "/api/teleop-speeds", { slow: n[0], mid: n[1], fast: n[2] });
      say(t("tsSaved"));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };
  return (
    <section class="panel sys-teleop">
      <h2>{t("tsTitle")}</h2>
      <p class="muted small">{t("tsHint")}</p>
      <div class="rd-inline">
        {TIERS.map((k) => (
          <label key={k} class="field">
            {t(TIER_KEY[k])}
            <input name={k} type="number" step="0.1" min="0.1" max={String(SPEED_MAX)} value={v[k]}
              onInput={(e) => setV({ ...v, [k]: e.currentTarget.value })} />
          </label>
        ))}
      </div>
      {!ok && <p class="error small" role="alert">{t("tsBad", { max: SPEED_MAX })}</p>}
      {err && <p class="error small" role="alert">{err}</p>}
      <button type="button" class="btn sm primary" disabled={!ok} onClick={() => void save()}>{t("tsSave")}</button>
    </section>
  );
}
