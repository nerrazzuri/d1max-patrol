// 高级 → 诊断(W44 第四期,设计 §5.3「诊断」):什么时候用 —— 有东西不正常,或者工程师让你发日志。
// 站点主机一张检查单(/api/health 各项,大白话,通过 / 不通过 + 站点写的说明);每台机器人一张(在线、设备健康:摄像头有画面、
// 网络、卫星定位、存储盘……认不得的原样);「打包日志」下载站点的诊断包(不含密钥、令牌、数据库)。
import { useEffect } from "preact/hooks";
import { useLoad } from "./admin";
import { nameOf } from "./data";
import { t, type Key } from "./i18n";
import type { Robot } from "./model";

interface Health { ok: boolean; version: string; checks: Record<string, { ok: boolean; detail?: string }> }
type DevRobot = Robot & { devices?: { items: { id: string; state: string; detail?: string }[]; stale?: boolean } | null };

const CHECK: Record<string, Key> = {
  db: "diagDb", loop: "diagLoop", broker: "diagBroker", disk: "diagDisk", certs: "diagCerts", lanes: "diagLanes",
  keys: "diagKeys", backup: "diagBackup", push: "diagPush", robots: "diagRobots",
};

/** 设备编号说人话:camera:front → 前摄像头有画面;link:router → 连得上路由器;认不得的原样。 */
export function deviceText(id: string): string {
  const [kind, name] = id.split(":");
  if (kind === "camera") return t("devCam", { c: name === "front" ? t("front") : name === "back" ? t("back") : name === "thermal" ? t("thermal") : name });
  if (kind === "link") return t("devLink", { n: name });
  if (kind === "rtk") return t("devRtk");
  if (kind === "disk") return t("devDisk");
  if (kind === "board") return t("devBoard");
  if (kind === "fan") return t("devFan", { n: name });
  if (kind === "amp") return t("devAmp");
  if (kind === "lens") return t("devLens");
  if (kind === "nav2") return t("devNav2");
  return id;
}

export function DiagPage() {
  const [health, reload] = useLoad<Health>("/api/health");
  const [robots, reloadRobots] = useLoad<{ robots: DevRobot[] }>("/api/robots");
  useEffect(() => {
    const id = window.setInterval(() => { reload(); reloadRobots(); }, 30_000);
    return () => window.clearInterval(id);
  }, [reload, reloadRobots]);
  return (
    <section class="advdiag">
      <div class="panel-head">
        <h1>{t("tabDiag")}</h1>
        <a class="btn push" href="/api/support-bundle" download="d1max-support.tar.gz">{t("supportBundle")}</a>
      </div>
      <p class="muted lead">{t("diagWhen")}</p>
      <section class="panel checklist">
        <h2>{t("diagSite")}</h2>
        {!health ? <p class="muted">…</p> : (
          <ul class="plain">
            {Object.entries(health.checks).map(([k, c]) => (
              <li key={k} class={c.ok ? "chk ok" : "chk bad"} data-check={k}>
                <span class="mark" aria-hidden="true">{c.ok ? "✓" : "✕"}</span>
                <b>{CHECK[k] ? t(CHECK[k]) : k}</b>
                {c.detail && <span class="muted small">{c.detail}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>
      {(robots?.robots ?? []).filter((r) => !r.revoked).map((r) => (
        <section key={r.robot_id} class="panel checklist" data-robot={r.robot_id}>
          <h2>{nameOf(r.robot_id)}</h2>
          <ul class="plain">
            <li class={r.fresh ? "chk ok" : "chk bad"}><span class="mark" aria-hidden="true">{r.fresh ? "✓" : "✕"}</span><b>{t("diagOnline")}</b></li>
            {(r.devices?.items ?? []).map((d) => (
              <li key={d.id} class={d.state === "online" ? "chk ok" : d.state === "degraded" ? "chk warn" : "chk bad"}>
                <span class="mark" aria-hidden="true">{d.state === "online" ? "✓" : d.state === "degraded" ? "!" : "✕"}</span>
                <b>{deviceText(d.id)}</b>
                {d.detail && <span class="muted small">{d.detail}</span>}
              </li>
            ))}
            {!r.devices && <li class="muted small">{t("diagNoDevices")}</li>}
            {r.devices?.stale && <li class="muted small">{t("devStale")}</li>}
          </ul>
        </section>
      ))}
    </section>
  );
}
