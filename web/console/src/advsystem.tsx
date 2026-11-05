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

/** 速度档上限(m/s),跟站点、机器人一致(用户 11-05:目前最高到 SDK 中速档)。 */
const SPEED_MAX = 2.0;
const SPEED_MAX_N = 8;

/** 管理员填的速度表:逗号或空格隔开的数;不合规回 null。 */
export function parseSpeeds(text: string): number[] | null {
  const parts = text.split(/[,，\s]+/).filter((p) => p !== "");
  if (parts.length < 1 || parts.length > SPEED_MAX_N) return null;
  const n = parts.map(Number);
  if (!n.every((x) => Number.isFinite(x) && x > 0 && x <= SPEED_MAX)) return null;
  return n.every((x, i) => i === 0 || x > n[i - 1]) ? n : null;
}

/** POC 遥控速度档(11-05 按速度分):手机上一个数一个按钮。管理员改。 */
function TeleopSpeeds({ say }: { say: (s: string) => void }) {
  const [got] = useLoad<{ speeds: number[] }>("/api/teleop-speeds");
  const [v, setV] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (got?.speeds) setV(got.speeds.join(", ")); }, [got]);
  if (v === null) return null;
  const n = parseSpeeds(v);
  const save = async () => {
    if (!n) return;
    setErr(null);
    try {
      await api("POST", "/api/teleop-speeds", { speeds: n });
      say(t("tsSaved"));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };
  return (
    <section class="panel sys-teleop">
      <h2>{t("tsTitle")}</h2>
      <p class="muted small">{t("tsHint")}</p>
      <label class="field">
        {t("tsList")}
        <input name="speeds" value={v} onInput={(e) => setV(e.currentTarget.value)} />
      </label>
      {!n && <p class="error small" role="alert">{t("tsBad", { n: SPEED_MAX_N, max: SPEED_MAX })}</p>}
      {err && <p class="error small" role="alert">{err}</p>}
      <button type="button" class="btn sm primary" disabled={!n} onClick={() => void save()}>{t("tsSave")}</button>
    </section>
  );
}
