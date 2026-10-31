// W55 远程运维(网页):工程师不到现场,看得见、动得了。
// - RobotOps:诊断页每台机器人下面一块 —— 诊断快照、各服务日志、重启服务 / 整机、详细日志、对时、清理、立即续签证书。
// - SiteOps:系统页 —— 重启站点、测试推送、备份与恢复、VPN 状态、运行指标曲线。
// - SiteUpdate、AppPackages:软件升级页 —— 升级站点(公司签名的包)、App 安装包(站点分发给手机)。
// 只给管理员、工程师(站点按 maintain 权限挡);要命的(重启整机、重启站点、恢复备份、升级站点)再输一次密码。
import { useEffect, useState } from "preact/hooks";
import { useLoad } from "./admin";
import { api, apiUpload, ApiError, enc } from "./api";
import { ConfirmPassword } from "./confirmpw";
import { hms } from "./data";
import { t, type Key } from "./i18n";

type Say = (s: string) => void;
const why = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const mb = (n?: number | null) => (n == null ? "—" : `${(n / 1048576).toFixed(n > 1048576 * 10 ? 0 : 1)} MB`);
const gb = (n?: number | null) => (n == null ? "—" : `${(n / 1073741824).toFixed(1)} GB`);

interface Svc { unit: string; active: string; sub: string; enabled: string; restarts: number | null; since: string }
interface Diag {
  at_ms: number; host?: string; busy?: string; uptime_s?: number | null; load?: string[];
  services?: Svc[]; release?: { current?: string | null; previous?: string | null };
  memory?: Record<string, number>; temps_c?: Record<string, number>;
  disks?: Record<string, { total: number; used: number; free: number } | null>;
  network?: { ssid?: string; wifi?: { if: string; signal_dbm: string }[] };
  time?: { synced?: boolean | null }; cert?: { not_after?: string; error?: string };
  env?: Record<string, string>; debug_until_ms?: number;
}

const UNITS = ["d1max-agent", "d1max-localizer", "d1max-obstacles", "d1max-persons", "d1max-lidar-merge",
  "d1max-nav2", "d1max-sidecar", "d1max-zenohd", "d1max-bind"];
/** 能远程重启的:全部(配网服务的代码在 root 独占目录里)。 */
const RESTARTABLE = UNITS;

export function RobotOps({ robotId, tasks, say }: { robotId: string; tasks: string[]; say: Say }) {
  const base = `/api/robots/${enc(robotId)}/ops`;
  const [diag, setDiag] = useState<Diag | null>(null);
  const [log, setLog] = useState<{ unit: string; text: string; truncated: boolean } | null>(null);
  const [unit, setUnit] = useState("d1max-agent");
  const [hours, setHours] = useState(6);
  const [svc, setSvc] = useState("d1max-agent");
  const [rebooting, setRebooting] = useState(false);
  const [clean, setClean] = useState<{ items: Record<string, { bytes: number | null; detail?: string[] }> } | null>(null);
  const [busy, setBusy] = useState(false);
  const has = (k: string) => tasks.includes(k);
  if (!has("ops_diag")) return <p class="muted small">{t("opsUnsupported")}</p>;

  async function run<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) say(ok);
      return r;
    } catch (e) {
      say(t("failed", { why: why(e) }));
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  const loadDiag = () => run(async () => setDiag(await api<Diag>("GET", `${base}/diag`)));
  const loadLog = () => run(async () => setLog(await api("GET", `${base}/journal?unit=${enc(unit)}&hours=${hours}`)));
  const debugOn = (diag?.debug_until_ms ?? 0) > Date.now();

  return (
    <div class="robot-ops" data-ops={robotId}>
      <div class="row-wrap">
        <button type="button" class="btn" disabled={busy} onClick={() => void loadDiag()}>{t("opsDiag")}</button>
        {has("ops_restart") && (
          <span class="row">
            <select aria-label={t("opsService")} value={svc} onChange={(e) => setSvc(e.currentTarget.value)}>
              {RESTARTABLE.map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
            <button type="button" class="btn" disabled={busy}
              onClick={() => void run(() => api("POST", `${base}/restart`, { unit: svc }), t("opsRestarted", { u: svc }))}>{t("opsRestart")}</button>
          </span>
        )}
        {has("ops_reboot") && <button type="button" class="btn danger" disabled={busy} onClick={() => setRebooting(true)}>{t("opsReboot")}</button>}
        {has("ops_debug") && (
          <button type="button" class="btn" disabled={busy}
            onClick={() => void run(() => api("POST", `${base}/debug`, { minutes: debugOn ? 0 : 60 }), debugOn ? t("opsDebugOff") : t("opsDebugOn")).then(loadDiag)}>
            {debugOn ? t("opsDebugStop") : t("opsDebug")}
          </button>
        )}
        {has("ops_timesync") && <button type="button" class="btn" disabled={busy} onClick={() => void run(() => api("POST", `${base}/timesync`), t("opsTimesynced"))}>{t("opsTimesync")}</button>}
        {has("cert_csr") && <button type="button" class="btn" disabled={busy} onClick={() => void run(() => api("POST", `${base}/cert-renew`), t("opsCertRenewed"))}>{t("opsCertRenew")}</button>}
        {has("ops_cleanup") && <button type="button" class="btn" disabled={busy} onClick={() => void run(async () => setClean(await api("GET", `${base}/cleanup`)))}>{t("opsCleanup")}</button>}
      </div>

      {diag && <DiagView d={diag} />}

      <div class="row-wrap ops-log">
        <select aria-label={t("opsLogUnit")} value={unit} onChange={(e) => setUnit(e.currentTarget.value)}>
          {[...UNITS, "ssh", "wg-quick@d1max", "chrony"].map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
        <select aria-label={t("opsLogHours")} value={String(hours)} onChange={(e) => setHours(Number(e.currentTarget.value))}>
          {[1, 6, 24, 72, 168].map((h) => <option key={h} value={String(h)}>{t("opsHours", { n: h })}</option>)}
        </select>
        <button type="button" class="btn" disabled={busy} onClick={() => void loadLog()}>{t("opsLog")}</button>
        {log && (
          <a class="btn" download={`${robotId}-${log.unit}.log`}
            href={`data:text/plain;charset=utf-8,${encodeURIComponent(log.text)}`}>{t("download")}</a>
        )}
      </div>
      {log && (
        <>
          {log.truncated && <p class="muted small">{t("opsLogTruncated")}</p>}
          <pre class="ops-logtext">{log.text || t("opsLogEmpty")}</pre>
        </>
      )}

      {clean && (
        <div class="ops-clean">
          {Object.entries(clean.items).map(([k, v]) => (
            <div key={k} class="sys-row">
              <span class="muted">{t(k === "releases" ? "opsCleanReleases" : "opsCleanJournal")}</span>
              <b>{mb(v.bytes)}{v.detail?.length ? ` · ${v.detail.join(", ")}` : ""}</b>
              <button type="button" class="btn sm" disabled={busy || !v.bytes}
                onClick={() => void run(() => api("POST", `${base}/cleanup`, { do: [k] }), t("opsCleaned")).then(() => setClean(null))}>{t("opsCleanDo")}</button>
            </div>
          ))}
        </div>
      )}

      {rebooting && (
        <ConfirmPassword title={t("opsRebootConfirm", { id: robotId })} onClose={() => setRebooting(false)}
          onConfirm={async (pw) => {
            await api("POST", `${base}/reboot`, { confirm_password: pw });
            setRebooting(false);
            say(t("opsRebooting"));
          }} />
      )}
    </div>
  );
}

function DiagView({ d }: { d: Diag }) {
  const mem = d.memory ?? {};
  return (
    <div class="ops-diag">
      <p class="muted small">{t("opsDiagAt", { t: hms(d.at_ms) })}{d.busy ? ` · ${t("opsBusy", { w: d.busy })}` : ""}</p>
      <table class="grid ops-svc">
        <thead><tr><th>{t("opsService")}</th><th>{t("opsState")}</th><th>{t("opsRestarts")}</th><th>{t("opsSince")}</th></tr></thead>
        <tbody>
          {(d.services ?? []).filter((s) => s.active || s.enabled).map((s) => (
            <tr key={s.unit} class={s.active === "failed" ? "bad" : undefined}>
              <td class="mono">{s.unit}</td>
              <td>{s.active}/{s.sub}{s.enabled ? ` · ${s.enabled}` : ""}</td>
              <td>{s.restarts ?? "—"}</td>
              <td class="small">{s.since || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div class="sys-row"><span class="muted">{t("opsRelease")}</span><b class="mono">{d.release?.current ?? "—"}{d.release?.previous ? ` (${t("opsPrev")} ${d.release.previous})` : ""}</b></div>
      <div class="sys-row"><span class="muted">{t("opsUptime")}</span><b>{d.uptime_s != null ? `${(d.uptime_s / 3600).toFixed(1)} h` : "—"} · {t("opsLoad")} {(d.load ?? []).join(" ")}</b></div>
      <div class="sys-row"><span class="muted">{t("opsMemory")}</span><b>{mb(mem.MemAvailable)} / {mb(mem.MemTotal)}</b></div>
      {Object.entries(d.disks ?? {}).map(([k, v]) => (
        <div key={k} class="sys-row"><span class="muted">{t("opsDisk", { k })}</span><b>{v ? `${gb(v.free)} / ${gb(v.total)}` : "—"}</b></div>
      ))}
      <div class="sys-row"><span class="muted">{t("opsTemps")}</span><b>{Object.entries(d.temps_c ?? {}).map(([k, v]) => `${k} ${v.toFixed(0)}°C`).join(" · ") || "—"}</b></div>
      <div class="sys-row"><span class="muted">{t("opsNet")}</span><b>{d.network?.ssid ?? "—"}{(d.network?.wifi ?? []).map((w) => ` · ${w.if} ${w.signal_dbm} dBm`).join("")}</b></div>
      <div class="sys-row"><span class="muted">{t("opsTime")}</span><b>{d.time?.synced == null ? t("unknown") : d.time.synced ? t("opsSynced") : t("opsNotSynced")}</b></div>
      <div class="sys-row"><span class="muted">{t("opsCert")}</span><b>{d.cert?.not_after ?? d.cert?.error ?? "—"}</b></div>
      {d.env && (
        <details>
          <summary>{t("opsEnv")}</summary>
          <dl class="kv small">{Object.entries(d.env).map(([k, v]) => <><dt key={k} class="mono">{k}</dt><dd class="mono">{v}</dd></>)}</dl>
        </details>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ 系统页:站点运维

interface Metric { ts: number; key: string; value: number }
const METRICS: { key: string; label: Key }[] = [
  { key: "site_disk_free", label: "mtDisk" },              // 站点报的是剩余比例(0–1) { key: "robots_online", label: "mtOnline" },
  { key: "upload_backlog", label: "mtBacklog" }, { key: "loop_lag_s", label: "mtLag" },
  { key: "alerts_open_P1", label: "mtP1" },
];

function Spark({ pts }: { pts: Metric[] }) {
  if (pts.length < 2) return <span class="muted small">—</span>;
  const xs = pts.map((p) => p.ts), ys = pts.map((p) => p.value);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const w = 320, h = 48;
  const d = pts.map((p, i) => `${i ? "L" : "M"}${(((p.ts - x0) / (x1 - x0 || 1)) * w).toFixed(1)},${(h - ((p.value - y0) / (y1 - y0 || 1)) * h).toFixed(1)}`).join(" ");
  return (
    <svg class="spark" viewBox={`0 0 ${w} ${h}`} width={w} height={h} role="img" aria-label={`${y0}–${y1}`}>
      <path d={d} fill="none" stroke="currentColor" stroke-width="1.5" />
    </svg>
  );
}

export function SiteMetrics() {
  const since = Date.now() - 30 * 86_400_000;
  const [m] = useLoad<{ metrics: Metric[] }>(`/api/metrics?since=${since}`);
  const rows = m?.metrics ?? [];
  return (
    <section class="panel sys-metrics">
      <h2>{t("mtTitle")}</h2>
      {METRICS.map(({ key, label }) => {
        const pts = rows.filter((r) => r.key === key);
        const last = pts.at(-1)?.value;
        return (
          <div key={key} class="sys-row metric" data-metric={key}>
            <span class="muted">{t(label)}</span>
            <Spark pts={pts} />
            <b>{last == null ? "—" : key === "site_disk_free" ? `${Math.round(last * 100)}%` : String(Math.round(last * 100) / 100)}</b>
          </div>
        );
      })}
    </section>
  );
}

interface Peer { name: string; kind: string; address: string; public: string; expires_ms?: number | null }
interface WgPeer { public_key: string; latest_handshake: number }

export function SiteOps({ say }: { say: Say }) {
  const [vpn] = useLoad<{ ready: boolean; peers: Peer[]; status?: { up?: boolean; peers?: WgPeer[]; error?: string } }>("/api/system/vpn");
  const [backup] = useLoad<{ last_ok_ms?: number | null; last_verify_ms?: number | null; dest?: string | null }>("/api/system/backups");
  const [ask, setAsk] = useState<"restart" | "restore" | null>(null);
  const [busy, setBusy] = useState(false);
  const hs = (pub: string) => vpn?.status?.peers?.find((p) => p.public_key === pub)?.latest_handshake ?? 0;
  return (
    <>
      <section class="panel sys-ops">
        <h2>{t("sysOps")}</h2>
        <p class="muted small">{t("sysOpsHint")}</p>
        <div class="row-wrap">
          <button type="button" class="btn" onClick={() => setAsk("restart")}>{t("sysRestart")}</button>
          <button type="button" class="btn" disabled={busy} onClick={() => {
            setBusy(true);
            api<{ sent: number; errors: string[] }>("POST", "/api/system/push-test", {})
              .then((r) => say(r.errors.length ? t("failed", { why: r.errors.join(";") }) : t("sysPushSent", { n: r.sent })))
              .catch((e) => say(t("failed", { why: why(e) })))
              .finally(() => setBusy(false));
          }}>{t("sysPushTest")}</button>
          <button type="button" class="btn danger" disabled={!backup?.dest} onClick={() => setAsk("restore")}>{t("sysRestore")}</button>
        </div>
      </section>
      <section class="panel sys-vpn">
        <h2>{t("sysNet")}</h2>
        {!vpn?.ready ? <p>{t("sysVpnNotYet")}</p> : (
          <>
            <p class="muted small">{vpn.status?.up ? t("sysVpnUp") : vpn.status?.error ? t("failed", { why: vpn.status.error }) : t("sysVpnDown")}</p>
            <table class="grid">
              <thead><tr><th>{t("name")}</th><th>{t("sysVpnKind")}</th><th>{t("sysVpnAddr")}</th><th>{t("sysVpnSeen")}</th></tr></thead>
              <tbody>
                {vpn.peers.map((p) => (
                  <tr key={p.name} data-peer={p.name}>
                    <td>{p.name}</td>
                    <td>{t(p.kind === "robot" ? "sysVpnRobot" : p.kind === "engineer" ? "sysVpnEngineer" : "sysVpnPhone")}
                      {p.expires_ms ? ` · ${t("sysVpnUntil", { t: hms(p.expires_ms) })}` : ""}</td>
                    <td class="mono">{p.address}</td>
                    <td>{hs(p.public) ? hms(hs(p.public) * 1000) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
      {ask && (
        <ConfirmPassword title={t(ask === "restart" ? "sysRestartConfirm" : "sysRestoreConfirm")} onClose={() => setAsk(null)}
          onConfirm={async (pw) => {
            await api("POST", `/api/system/${ask}`, { confirm_password: pw });
            setAsk(null);
            say(t(ask === "restart" ? "sysRestarting" : "sysRestoring"));
          }} />
      )}
    </>
  );
}

// ------------------------------------------------------------------ 软件升级页:站点升级、App 安装包

interface LastOp { last: null | { id: string; by: string; at_ms: number; result?: { ok: boolean; error: string; version?: string; rolled_back?: boolean } | null } }

export function SiteUpdate({ say }: { say: Say }) {
  const [last, reload] = useLoad<LastOp>("/api/system/update");
  const [file, setFile] = useState<File | null>(null);
  const [ask, setAsk] = useState(false);
  useEffect(() => {
    if (!last?.last || last.last.result) return;
    const id = window.setInterval(reload, 10_000);       // 升级中:站点会重启,结果出来前隔 10 秒问一次
    return () => window.clearInterval(id);
  }, [last, reload]);
  const r = last?.last;
  return (
    <section class="panel up-site">
      <h2>{t("upSiteTitle")}</h2>
      <p class="muted small">{t("upSiteHint")}</p>
      {r && (
        <p class={r.result && !r.result.ok ? "error" : "muted small"}>
          {!r.result ? t("upSiteRunning", { t: hms(r.at_ms) })
            : r.result.ok ? t("upSiteDone", { v: r.result.version ?? "" })
            : t("failed", { why: r.result.error })}
        </p>
      )}
      <div class="row-wrap">
        <input type="file" accept=".tar.gz,application/gzip" aria-label={t("upSiteFile")}
          onChange={(e) => setFile(e.currentTarget.files?.[0] ?? null)} />
        <button type="button" class="btn primary" disabled={!file} onClick={() => setAsk(true)}>{t("upSiteGo")}</button>
      </div>
      {ask && file && (
        <ConfirmPassword title={t("upSiteConfirm")} onClose={() => setAsk(false)}
          onConfirm={async (pw) => {
            await apiUpload("/api/system/update", file, { "X-D1Max-Confirm": pw });
            setAsk(false);
            say(t("upSiteStarted"));
            reload();
          }} />
      )}
    </section>
  );
}

const PLATFORMS = ["android", "linux", "windows", "macos"] as const;

export function AppPackages({ say }: { say: Say }) {
  const [cur, setCur] = useState<Record<string, { available: boolean; version?: string } | null>>({});
  const [pf, setPf] = useState<(typeof PLATFORMS)[number]>("android");
  const [file, setFile] = useState<File | null>(null);
  const [man, setMan] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    for (const p of PLATFORMS) {
      api<{ available: boolean; version?: string }>("GET", `/api/app-update?platform=${p}&build=0`)
        .then((d) => setCur((c) => ({ ...c, [p]: d })))
        .catch(() => setCur((c) => ({ ...c, [p]: null })));
    }
  };
  useEffect(load, []);
  async function upload() {
    if (!file || !man) return;
    setBusy(true);
    try {
      const manifest = btoa(String.fromCharCode(...new TextEncoder().encode(await man.text())));
      await apiUpload(`/api/app-update/upload?platform=${pf}`, file, { "X-App-Manifest": manifest });
      say(t("appUploaded"));
      load();
    } catch (e) {
      say(t("failed", { why: why(e) }));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section class="panel up-apps">
      <h2>{t("appTitle")}</h2>
      <p class="muted small">{t("appHint")}</p>
      {PLATFORMS.map((p) => (
        <div key={p} class="sys-row" data-platform={p}>
          <span class="muted">{p}</span><b>{cur[p]?.available ? cur[p]?.version : t("appNone")}</b>
        </div>
      ))}
      <div class="row-wrap">
        <select aria-label={t("appPlatform")} value={pf} onChange={(e) => setPf(e.currentTarget.value as typeof pf)}>
          {PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <label class="field">{t("appFile")}<input type="file" onChange={(e) => setFile(e.currentTarget.files?.[0] ?? null)} /></label>
        <label class="field">{t("appManifest")}<input type="file" accept=".json,application/json" onChange={(e) => setMan(e.currentTarget.files?.[0] ?? null)} /></label>
        <button type="button" class="btn primary" disabled={busy || !file || !man} onClick={() => void upload()}>{t("appUpload")}</button>
      </div>
    </section>
  );
}
