// 高级 → 系统(W44 第四期,设计 §5.3「系统」):站点名字、时间、存储、证书、证据加密、手机配对;外网和 VPN(W45 还没做,
// 先写明还没装);备份 —— 最近一次备份和校验(站点体检的备份那一项),超过 7 天没成功写红。备份目录在站点主机上配,网页只看。
import { useLoad, PairPhone } from "./admin";
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
      <SiteOps say={say} />
      <SiteMetrics />
      <PairPhone />
    </section>
  );
}
