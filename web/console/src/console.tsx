// 值班台外框(商业化 B1,B0 专业版):顶栏(页签、P1/P2 计数、模式、时钟、语言、Stop all)、页面
// (Live 视频墙 / Map / Alarms)、Live 以外的页签顶上的全站 P1 条、底部常驻的报警队列。
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import type { Me } from "./app";
import type { Cam } from "./camera";
import { useNow, useSiteData } from "./data";
import { Confirm, ShelveDialog } from "./dialogs";
import { alertTitle, lang, t, type Key } from "./i18n";
import { Icon, Shape } from "./icons";
import { LangSwitch } from "./langswitch";
import { AlarmTile, FullCamera, JumpStrip, OfflineStrip, RobotUnit, type UnitInfo } from "./live";
import { MapPage } from "./mappage";
import {
  alarmState,
  canArm,
  canDispatch,
  duration,
  FELL_KINDS,
  queue,
  robotState,
  wallOrder,
  type Alert,
  type Mode,
} from "./model";
import { AlarmQueue } from "./queue";

type Page = "live" | "map" | "alarms";

function readRoute(): { page: Page; robot: string | null } {
  const h = typeof location === "undefined" ? "" : location.hash;
  const [p, q] = h.replace(/^#\/?/, "").split("?");
  const robot = new URLSearchParams(q ?? "").get("robot");
  return { page: p === "map" ? "map" : p === "alarms" ? "alarms" : "live", robot };
}

const TASK: Record<string, Key> = { patrol: "st_patrolling", goto: "st_patrolling", standby: "st_returning" };

type Pending =
  | { kind: "mode"; mode: Mode["mode"] }
  | { kind: "stop" }
  | { kind: "false"; a: Alert }
  | { kind: "shelve"; a: Alert }
  | { kind: "cam"; robot: string; cam: Cam }
  | null;

export function Console({ me, onOut }: { me: Me; onOut: () => void }) {
  const [data, refresh] = useSiteData();
  const now = useNow();
  const [route, setRoute] = useState(readRoute);
  const [qOpen, setQOpen] = useState(true);
  const [pending, setPending] = useState<Pending>(null);
  const [toast, setToast] = useState("");
  void lang.value; // 换语言整页重画

  useEffect(() => {
    const on = () => setRoute(readRoute());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
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
  const { active, shelved } = queue(alerts, now);
  const p1 = active.filter((a) => a.level === "P1");
  const p1Robots = new Set(p1.map((a) => a.robot));
  const fell = new Set(alerts.filter((a) => a.resolved_ms == null && FELL_KINDS.has(a.kind)).map((a) => a.robot));
  const units: UnitInfo[] = useMemo(() => {
    const deter = new Map((data?.deter ?? []).map((d) => [d.robot_id, d]));
    const batt = new Map((data?.summary?.robots ?? []).map((r) => [r.robot_id, r.battery_pct]));
    return (data?.robots ?? [])
      .filter((r) => r.active && !r.revoked)
      .map((r) => {
        const kind = r.status?.task?.kind ?? "";
        const state = robotState(r, deter.get(r.robot_id), kind.includes("dock") || kind.includes("charg"), fell.has(r.robot_id));
        return { robot: r, state, battery: batt.get(r.robot_id) ?? null, deter: deter.get(r.robot_id), task: kind && TASK[kind] ? t(TASK[kind]) : "" };
      });
  }, [data, lang.value]);
  const { pinned, rest } = wallOrder(units.map((u) => u.robot), p1Robots);
  const unitOf = (id: string) => units.find((u) => u.robot.robot_id === id)!;
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
  const firstP1 = p1[0];

  return (
    <div class="console">
      <TopBar me={me} page={route.page} mode={data?.mode ?? null} p1={p1.length} p2={active.length - p1.length}
        now={now} onPage={go} onMode={(m) => setPending({ kind: "mode", mode: m })}
        onStop={() => setPending({ kind: "stop" })}
        onSignOut={async () => { try { await api("POST", "/api/logout", {}); } finally { onOut(); } }} />
      {route.page !== "live" && firstP1 && (
        <div class={alarmState(firstP1, now) === "unacked" ? "p1bar flash" : "p1bar"} role="alert">
          <span class="pri"><Shape kind="P1" />P1</span>
          <span>{alertTitle(lang.value, firstP1.kind, firstP1.title)} · {firstP1.robot}</span>
          <span class="push strong">{alarmState(firstP1, now) === "unacked" ? t("unackedFor", { d: duration(now - firstP1.first_ms) }) : ""}</span>
          {alarmState(firstP1, now) === "unacked" && <button type="button" class="btn primary sm" onClick={() => ack(firstP1)}>{t("take")}</button>}
          <button type="button" class="btn onsolid sm" onClick={() => go("live")}>{t("goToVideo")}</button>
        </div>
      )}

      <main class="page" id="main">
        {route.page === "live" && (
          <>
            {units.length > 4 && (
              <JumpStrip units={[...pinned, ...rest].map((r) => unitOf(r.robot_id))} p1={p1Robots}
                watching={online.length * 2} total={units.length * 2} />
            )}
            {units.length === 0 && data && <p class="empty">{t("noRobots")}</p>}
            {pinned.map((r) => {
              const a = p1.find((x) => x.robot === r.robot_id)!;
              return (
                <AlarmTile key={a.key} a={a} u={unitOf(r.robot_id)} role={me.role} now={now}
                  onAck={() => ack(a)}
                  onSiren={() => act(() => api("POST", `/api/deterrence/${enc(a.robot)}/level`, { level: 2 }))}
                  onFalse={() => setPending({ kind: "false", a })}
                  onMap={() => go("map", a.robot)}
                  onOpen={(cam) => setPending({ kind: "cam", robot: a.robot, cam })} />
              );
            })}
            <div class="wall">
              {rest.map((r) => {
                const u = unitOf(r.robot_id);
                return u.state === "offline"
                  ? <OfflineStrip key={r.robot_id} u={u} onMap={() => go("map", r.robot_id)} />
                  : <RobotUnit key={r.robot_id} u={u} now={now} onMap={() => go("map", r.robot_id)}
                      onOpen={(cam) => setPending({ kind: "cam", robot: r.robot_id, cam })} />;
              })}
            </div>
          </>
        )}
        {route.page === "map" && (
          <MapPage units={units} p1={p1} now={now} selected={route.robot ?? firstP1?.robot ?? null}
            onSelect={(id) => go("map", id)}
            onVideo={(id) => { go("live"); window.setTimeout(() => document.getElementById(`unit-${id}`)?.scrollIntoView(), 50); }} />
        )}
      </main>

      <AlarmQueue active={active} shelved={shelved} now={now} open={route.page === "alarms" || qOpen}
        onToggle={() => setQOpen(!qOpen)} onAck={ack}
        onShelve={(a) => setPending({ kind: "shelve", a })}
        onUnshelve={(a) => act(() => api("POST", `/api/alerts/${enc(a.key)}/unshelve`, {}))}
        onGo={(a) => { go("live"); window.setTimeout(() => document.getElementById(`unit-${a.robot}`)?.scrollIntoView(), 50); }} />

      {pending?.kind === "mode" && (
        <Confirm text={t(pending.mode === "armed" ? "modeArmConfirm" : pending.mode === "home" ? "modeHomeConfirm" : "modeVisitorConfirm")}
          yes={t(pending.mode === "armed" ? "modeArmed" : pending.mode === "home" ? "modeHome" : "modeVisitor")}
          onNo={() => setPending(null)}
          onYes={() => { const m = pending.mode; setPending(null); void act(() => api("POST", "/api/mode", { mode: m })); }} />
      )}
      {pending?.kind === "stop" && (
        <Confirm text={t("stopAllConfirm", { n: online.length })} yes={t("stopAll")} danger onNo={() => setPending(null)} onYes={stopAll} />
      )}
      {pending?.kind === "false" && (
        <Confirm text={t("falseAlarmConfirm")} yes={t("falseAlarmYes")} onNo={() => setPending(null)}
          onYes={() => { const a = pending.a; setPending(null); void act(() => api("POST", `/api/alerts/${enc(a.key)}/resolve`, {})); }} />
      )}
      {pending?.kind === "shelve" && (
        <ShelveDialog now={now} onClose={() => setPending(null)}
          onShelve={(until, reason) => { const a = pending.a; setPending(null); void act(() => api("POST", `/api/alerts/${enc(a.key)}/shelve`, { until_ms: until, reason })); }} />
      )}
      {pending?.kind === "cam" && <FullCamera robot={pending.robot} cam={pending.cam} now={now} onClose={() => setPending(null)} />}
      <div class="toast" role="status" aria-live="polite">{toast}</div>
    </div>
  );
}

function TopBar(props: {
  me: Me;
  page: Page;
  mode: Mode | null;
  p1: number;
  p2: number;
  now: number;
  onPage: (p: Page) => void;
  onMode: (m: Mode["mode"]) => void;
  onStop: () => void;
  onSignOut: () => void;
}) {
  const tabs: { id: Page | null; key: Key }[] = [
    { id: "live", key: "tabLive" },
    { id: "map", key: "tabMap" },
    { id: "alarms", key: "tabAlarms" },
    { id: null, key: "tabRecordings" },
    { id: null, key: "tabReports" },
  ];
  const modes: Mode["mode"][] = ["armed", "home", "visitor"];
  const modeKey: Record<Mode["mode"], Key> = { armed: "modeArmed", home: "modeHome", visitor: "modeVisitor" };
  return (
    <header class="topbar">
      <a class="skip" href="#main">Skip to content</a>
      <span class="wordmark">D1 Max</span>
      <span class="site muted">{props.me.site_name}</span>
      <nav class="tabsnav" aria-label={t("mainNav")}>
        {tabs.map((x) => x.id ? (
          <a key={x.key} href={`#/${x.id}`} aria-current={props.page === x.id ? "page" : undefined}>{t(x.key)}</a>
        ) : (
          <span key={x.key} class="disabled" aria-disabled="true" title={t("comingSoon")}>{t(x.key)}</span>
        ))}
      </nav>
      <span class="counts" aria-live="polite">
        {props.p1 + props.p2 === 0 ? <span class="muted">{t("noActiveAlarms")}</span> : null}
        {props.p1 > 0 && <span class="pri P1"><Shape kind="P1" />{props.p1}</span>}
        {props.p2 > 0 && <span class="pri P2"><Shape kind="P2" />{props.p2}</span>}
      </span>
      {props.mode && (
        <div class="seg push" role="group" aria-label={t("modeLabel")}>
          {modes.map((m) => (
            <button key={m} type="button" aria-pressed={props.mode?.mode === m}
              disabled={!canArm(props.me.role) || props.mode?.mode === m} onClick={() => props.onMode(m)}>
              {t(modeKey[m])}
            </button>
          ))}
        </div>
      )}
      <span class={props.mode ? "clock mono" : "clock mono push"}>{new Date(props.now).toLocaleTimeString("en-GB", { hour12: false })}</span>
      <LangSwitch />
      <span class="who">{props.me.display_name || props.me.name}</span>
      <button type="button" class="btn icon" aria-label={t("signOut")} title={t("signOut")} onClick={props.onSignOut}><Icon name="logout" /></button>
      {(canDispatch(props.me.role) || props.me.role === "owner") && (
        <button type="button" class="btn stop" onClick={props.onStop}><Icon name="octagon" />{t("stopAll")}</button>
      )}
    </header>
  );
}
