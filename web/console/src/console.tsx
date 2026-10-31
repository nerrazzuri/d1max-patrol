// 值班台外框(W44 第一期):外壳(shell.tsx 的顶栏、路由、主题)+ 站点数据 + 各页 + 共用的对话框。
// 保安五页:值班、实况、地图、录像、报警记录(深色);管理各页(浅色)只有管理员进得去。
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import type { Me } from "./app";
import type { Cam } from "./camera";
import { hms, isStale, useNow, useSiteData } from "./data";
import { Confirm } from "./dialogs";
import { lang, t, type Key } from "./i18n";
import { Shape } from "./icons";
import { FullCamera, type UnitInfo } from "./live";
import { LocPage } from "./advloc";
import { RecordingsPage } from "./recpage";
import { ReportsPage } from "./reportpage";
import { MapPage } from "./mappage";
import { FELL_KINDS, forGuard, isOwnerLike, queue, robotCams, robotState, type Alert, type Mode } from "./model";
import { AlarmLogPage } from "./alarmlog";
import { HelpDialog, WEB_TOUR } from "./guide";
import { ArmingPage } from "./armingpage";
import { DutyPage } from "./duty";
import { LiveWall } from "./livewall";
import { HomePage } from "./homepage";
import { DiagPage } from "./advdiag";
import { SystemPage2 } from "./advsystem";
import { UpgradePage } from "./advupgrade";
import { RobotsPage } from "./robotspage";
import { UsersPage } from "./userspage";
import { AdvMapsPage } from "./advmaps";
import { editTarget, MapEditPage } from "./mapedit";
import { AccessPage } from "./accesspage";
import { LayoutPage } from "./layoutpage";
import { LAYOUT_TABS } from "./layouttabs";
import { AdminNav, isAdminPage, loadThemePref, pageAllowed, pinFor, readRoute, saveThemePref, TopBar, type Page, type ThemePref } from "./shell";
import { pinScheme, systemIsDark } from "./theme";

const TASK: Record<string, Key> = { patrol: "st_patrolling", goto: "st_patrolling", standby: "st_returning" };

type Pending =
  | { kind: "mode"; mode: Mode["mode"] }
  | { kind: "stop" }
  | { kind: "cam"; robot: string; cam: Cam }
  | null;

export function Console({ me, onOut }: { me: Me; onOut: () => void }) {
  const [data, refresh, conn] = useSiteData();
  const now = useNow();
  // B 阶段外审 I5:拿不到新数据时,旧的照显示但标明过期 —— 不能把旧的「在线、没有报警」当成现在
  const stale = isStale(conn, now);
  const [route, setRoute] = useState(readRoute);
  const [pending, setPending] = useState<Pending>(null);
  const [toast, setToast] = useState("");
  // W53:第一次登录(这个账号没看过当前版本的网页教程)自动打开;老站点不带 guide 就不弹
  const [help, setHelp] = useState(() => me.guide != null && (me.guide.web_tour ?? 0) < WEB_TOUR);
  void lang.value; // 换语言整页重画

  useEffect(() => {
    const on = () => setRoute(readRoute());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const [pref, setPref] = useState<ThemePref>(loadThemePref);
  useEffect(() => {
    pinScheme(pinFor(route.page, pref, systemIsDark()));
  }, [route.page, pref]);
  useEffect(() => () => pinScheme(null), []);
  const go = (page: Page, robot?: string) => {
    location.hash = `#/${page}${robot ? `?robot=${encodeURIComponent(robot)}` : ""}`;
  };

  const say = (s: string) => {
    setToast(s);
    window.setTimeout(() => setToast(""), 6000);
  };
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      say(t("failed", { why: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const alerts = data?.alerts ?? [];
  const { active } = queue(alerts, now);
  const guardAlarms = active.filter(forGuard).length;
  const p1 = active.filter((a) => a.level === "P1");
  const fell = new Set(alerts.filter((a) => a.resolved_ms == null && FELL_KINDS.has(a.kind)).map((a) => a.robot));
  const units: UnitInfo[] = useMemo(() => {
    const deter = new Map((data?.deter ?? []).map((d) => [d.robot_id, d]));
    const deterUnknown = data != null && data.deter === null;
    const batt = new Map((data?.summary?.robots ?? []).map((r) => [r.robot_id, r.battery_pct]));
    return (data?.robots ?? [])
      .filter((r) => r.active && !r.revoked)
      .map((r) => {
        const kind = r.status?.task?.kind ?? "";
        const state = robotState(r, deter.get(r.robot_id), kind.includes("dock") || kind.includes("charg"), fell.has(r.robot_id));
        return { robot: r, state, battery: stale ? null : batt.get(r.robot_id) ?? null, deter: deter.get(r.robot_id),
          task: stale ? "" : kind && TASK[kind] ? t(TASK[kind]) : "", stale, deterUnknown };
      });
  }, [data, lang.value, stale]);
  const online = units.filter((u) => u.state !== "offline");

  async function stopAll() {
    setPending(null);
    const res = await Promise.allSettled(online.map((u) => api("POST", `/api/robots/${enc(u.robot.robot_id)}/halt`, {})));
    const bad = online.map((u, i) => [u.robot.robot_id, res[i]] as const).filter(([, r]) => r.status === "rejected");
    say(bad.length
      ? bad.map(([id, r]) => t("stopFailed", { id, why: r.status === "rejected" && r.reason instanceof ApiError ? r.reason.message : "?" })).join(" ")
      : t("stopAllDone", { n: online.length }));
    await refresh();
  }

  const ack = (a: Alert) => act(() => api("POST", `/api/alerts/${enc(a.key)}/ack`, {}));
  const resolve = (a: Alert, verdict: string) => act(() => api("POST", `/api/alerts/${enc(a.key)}/resolve`, { verdict }));
  const firstP1 = p1.find(forGuard);
  const notices = alerts.filter((a) => a.resolved_ms == null && !forGuard(a)).length;
  const admin = isOwnerLike(me.role);

  // 带地图的页整页一屏(地图撑满顶栏以下):地图、防区、巡检路线
  const fill = route.page === "duty" || route.page === "map" || route.page === "admin/layout" || route.page === "admin/mapedit";
  return (
    <div class={fill ? "console fill" : "console"}>
      <TopBar me={me} page={route.page} mode={data?.mode ?? null} alarms={stale ? null : guardAlarms} now={now}
        pref={pref} onPref={(p) => { saveThemePref(p); setPref(p); }}
        onStop={() => setPending({ kind: "stop" })} onSay={say}
        onHelp={() => setHelp(true)}
        onSignOut={async () => { try { await api("POST", "/api/logout", {}); } finally { onOut(); } }} />
      {help && <HelpDialog role={me.role} onClose={() => setHelp(false)} />}
      {stale && (
        <div class="stalebar" role="alert">
          <Shape kind="P2" />
          {conn.okAt ? t("staleBanner", { t: hms(conn.okAt) }) : t("staleNever")}
        </div>
      )}

      <main class="page" id="main" data-page={route.page}>
        {route.page === "duty" && (
          <DutyPage units={units} alerts={alerts} now={now} role={me.role} say={say} refresh={refresh}
            onZoom={(id) => setPending({ kind: "cam", robot: id, cam: "front" })} />
        )}
        {route.page === "live" && (
          <LiveWall units={units} alerts={alerts} now={now} onZoom={(id) => setPending({ kind: "cam", robot: id, cam: "front" })} />
        )}
        {route.page === "alarms" && <AlarmLogPage now={now} role={me.role} />}
        {!pageAllowed(route.page, me.role) && <p class="empty noaccess" role="alert">{t("noAccess")}</p>}
        {isAdminPage(route.page) && admin && (
          <div class="adminwrap">
            <AdminNav page={route.page} notices={notices} role={me.role} />
            <div class="adminbody">
              {route.page === "admin/home" && (
                <HomePage alerts={alerts} mode={data?.mode ?? null} now={now} onAck={ack} onDone={(a) => resolve(a, "handled")}
                  onMode={(m) => setPending({ kind: "mode", mode: m })} />
              )}
              {route.page === "admin/arming" && <ArmingPage say={say} now={now} role={me.role} />}
              {route.page === "admin/users" && pageAllowed(route.page, me.role) && <UsersPage me={me.name} say={say} now={now} />}
              {route.page === "admin/robots" && <RobotsPage say={say} now={now} />}
              {route.page === "admin/system" && <SystemPage2 me={me} now={now} say={say} />}
              {route.page === "admin/diag" && <DiagPage say={say} />}
              {route.page === "admin/mapedit" && <MapEditPage say={say} target={editTarget(location.hash)} />}
              {route.page === "admin/upgrade" && <UpgradePage say={say} />}
              {route.page === "admin/layout" && <LayoutPage say={say} robots={units.map((u) => u.robot.robot_id)} tabs={LAYOUT_TABS} />}
              {route.page === "admin/maps" && <AdvMapsPage say={say} />}
              {route.page === "admin/access" && <AccessPage say={say} now={now} />}
              {route.page === "admin/localization" && <LocPage units={units} alerts={alerts} now={now} role={me.role} />}
              {route.page === "admin/reports" && <ReportsPage now={now} />}
            </div>
          </div>
        )}
        {route.page === "recordings" && (
          <RecordingsPage key={`${route.robot ?? ""}@${route.at ?? ""}`} robots={units.map((u) => u.robot.robot_id)} role={me.role} now={now}
            camsOf={(id) => robotCams(units.find((u) => u.robot.robot_id === id)?.robot)}
            robot={route.robot} at={route.at} say={say} />
        )}
        {route.page === "map" && (
          <MapPage units={units} p1={p1} now={now} role={me.role} selected={route.robot ?? firstP1?.robot ?? null}
            onSelect={(id) => go("map", id)}
            onVideo={(id) => go("duty", id)} mode={data?.mode ?? null} say={say}
            onArm={() => setPending({ kind: "mode", mode: "armed" })} />
        )}
      </main>

      {pending?.kind === "mode" && (
        <Confirm text={t(pending.mode === "armed" ? "modeArmConfirm" : pending.mode === "home" ? "modeHomeConfirm" : "modeVisitorConfirm")}
          yes={t(pending.mode === "armed" ? "modeArmed" : pending.mode === "home" ? "modeHome" : "modeVisitor")}
          onNo={() => setPending(null)}
          onYes={() => { const m = pending.mode; setPending(null); void act(() => api("POST", "/api/mode", { mode: m })); }} />
      )}
      {pending?.kind === "stop" && (
        <Confirm text={t("stopAllConfirm", { n: online.length })} yes={t("stopAll")} danger onNo={() => setPending(null)} onYes={stopAll} />
      )}
      {pending?.kind === "cam" && <FullCamera robot={pending.robot} cam={pending.cam} now={now} onClose={() => setPending(null)}
        cams={robotCams(units.find((u) => u.robot.robot_id === pending.robot)?.robot)} />}
      <div class="toast" role="status" aria-live="polite">{toast}</div>
    </div>
  );
}
