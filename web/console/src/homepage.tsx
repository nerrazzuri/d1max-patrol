// 家里的状况(W44 第二期,设计 §5.0;打开「管理」的第一页,换掉第一期的「提醒」页)。一页看完,不用点进去:
// 1. 要你处理的事 —— 给业主的技术提醒,说人话、写该做什么、带一个按钮;没有就整块不显示。
// 2. 今天的安排 —— 今天几点布防(按防区)、今天的授权、访客模式开到几点;右边切模式(设计 §11 第 14 条),写现在谁说了算。
// 3. 机器人 —— 每台一张小卡片,点了去机器人页。
// 4. 这周 —— 出了几次事、几次误报,跟上周比。
import { useEffect, useState } from "preact/hooks";
import { advice } from "./advice";
import { api } from "./api";
import { modeLabel, whoText } from "./armingpage";
import type { Authz } from "./accesspage";
import { hm } from "./data";
import { alertTitle, lang, t, type Key } from "./i18n";
import { Icon } from "./icons";
import { forGuard, type Alert, type Mode } from "./model";
import { reportRange, type Summary } from "./reports";
import type { RobotCard } from "./robotspage";

interface Today { zone: string; windows: [string, string][] }

export function HomePage(props: {
  alerts: Alert[];
  mode: Mode | null;
  now: number;
  onAck: (a: Alert) => void;
  onDone: (a: Alert) => void;
  onMode: (m: Mode["mode"]) => void;
}) {
  const [today, setToday] = useState<{ enabled: boolean; today: Today[] } | null>(null);
  const [authz, setAuthz] = useState<Authz[]>([]);
  const [robots, setRobots] = useState<RobotCard[] | null>(null);
  const [weeks, setWeeks] = useState<[Summary | null, Summary | null]>([null, null]);
  void lang.value;

  useEffect(() => {
    api<{ enabled: boolean; today: Today[] }>("GET", "/api/arming/schedule").then(setToday).catch(() => setToday(null));
    api<{ authorizations: Authz[] }>("GET", "/api/lists/authorizations").then((d) => setAuthz(d.authorizations ?? [])).catch(() => setAuthz([]));
    api<{ robots: RobotCard[] }>("GET", "/api/robots/overview").then((d) => setRobots((d.robots ?? []).filter((r) => !r.revoked))).catch(() => setRobots(null));
    const w = reportRange("week", props.now)!;
    const l = reportRange("lastweek", props.now)!;
    Promise.all([w, l].map((p) => api<Summary>("GET", `/api/reports/summary?since=${p.since}&until=${p.until}`).catch(() => null)))
      .then(([a, b]) => setWeeks([a, b]));
  }, []);

  const todo = props.alerts.filter((a) => a.resolved_ms == null && !forGuard(a))
    .sort((a, b) => (a.level === b.level ? b.last_ms - a.last_ms : a.level < b.level ? -1 : 1));
  const todayAuthz = authz.filter((a) => a.on_today);
  const m = props.mode;
  const modes: { m: Mode["mode"]; key: Key }[] = [
    { m: "armed", key: "modeArmed" }, { m: "home", key: "modeHome" }, { m: "visitor", key: "modeVisitor" },
  ];

  return (
    <section class="home">
      <h1>{t("tabHome")}</h1>

      {todo.length > 0 && (
        <section class="panel home-todo">
          <h2>{t("homeTodo")}</h2>
          <ul class="plain">
            {todo.map((a) => {
              const adv = advice(a.kind);
              return (
                <li key={a.key} class={`todo ${a.level}`}>
                  <div class="todo-body">
                    <b>{adv ? t(adv.say) : alertTitle(lang.value, a.kind, a.title)}</b>
                    {a.detail && <span class="muted small">{a.detail}</span>}
                    <span>{t(adv ? adv.todo : "advCallEngineer")}</span>
                  </div>
                  <div class="todo-acts">
                    {adv?.go ? <a class="btn sm" href={`#/${adv.go}`}>{t("homeGo")}</a>
                      : a.acked_ms == null && <button type="button" class="btn sm" onClick={() => props.onAck(a)}>{t("noticeAck")}</button>}
                    <button type="button" class="btn sm primary" onClick={() => props.onDone(a)}>{t("noticeDone")}</button>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section class="panel home-today">
        <div class="panel-head"><h2>{t("homeToday")}</h2></div>
        <div class="home-today-grid">
          <div>
            {today && today.enabled ? (
              <ul class="plain">
                {today.today.map((z) => (
                  <li key={z.zone}>
                    <b>{z.zone || t("homeAllZones")}</b>{" "}
                    {z.windows.length === 0 ? t("homeDisarmedAllDay") : t("homeArmedAt", { w: z.windows.map(([a, b]) => `${a}–${b}`).join("、") })}
                  </li>
                ))}
              </ul>
            ) : (
              <p class="muted">{t("homeNoSchedule")} <a href="#/admin/arming">{t("homeSetSchedule")}</a></p>
            )}
            <h3>{t("homeAuthzToday")}</h3>
            {todayAuthz.length === 0 ? <p class="muted small">{t("homeAuthzNone")}</p> : (
              <ul class="plain">
                {todayAuthz.map((a) => (
                  <li key={a.id}>
                    <b>{a.name}</b> {a.zones.length ? a.zones.join("、") : t("wholeSite")} <span class="mono">{a.start}–{a.end}</span>
                    {a.active_now && <span class="tag">{t("homeAuthzNow")}</span>}
                  </li>
                ))}
              </ul>
            )}
            {m?.mode === "visitor" && m.until_ms != null && <p><b>{t("homeVisitorUntil", { t: hm(m.until_ms) })}</b></p>}
          </div>
          {m && (
            <div class="home-mode">
              <p class="arm-who">{whoText(m as Parameters<typeof whoText>[0], today?.enabled ?? false)}</p>
              <div class="seg" role="group" aria-label={t("homeSwitch")}>
                {modes.map((x) => (
                  <button key={x.m} type="button" aria-pressed={m.source !== "schedule" && m.mode === x.m}
                    onClick={() => props.onMode(x.m)}>{t(x.key)}</button>
                ))}
              </div>
              <p class="muted small">{modeLabel(m)}</p>
            </div>
          )}
        </div>
      </section>

      <section class="panel home-robots">
        <h2>{t("homeRobots")}</h2>
        {robots == null ? <p class="muted">…</p> : robots.length === 0 ? <p class="muted">{t("robotsNone")}</p> : (
          <div class="mini-cards">
            {robots.map((r) => (
              <a key={r.robot_id} class="mini-card" href="#/admin/robots">
                <Icon name="robot" />
                <b>{r.name || r.robot_id}</b>
                <span class={r.online ? "tone-success" : "muted"}>{r.online ? t("online") : t("st_offline")}</span>
                <span>{r.battery_pct == null ? "—" : `${r.battery_pct}%`} · {t("robotPatrolsToday", { n: r.patrols_today })}</span>
                <span class={r.problems.length ? "warn-text" : "tone-success"}>{r.problems.length ? t("robotProblems", { n: r.problems.length }) : t("robotOk")}</span>
              </a>
            ))}
          </div>
        )}
      </section>

      <section class="panel home-week">
        <h2>{t("homeWeek")}</h2>
        {weeks[0] == null ? <p class="muted">…</p> : (
          <ul class="plain">
            <li><b>{t("homeIncidents", { n: weeks[0].totals.incidents })}</b> <span class="muted">{compare(weeks[0].totals.incidents, weeks[1]?.totals.incidents)}</span></li>
            <li><b>{t("homeFalse", { n: weeks[0].totals.false_alarms })}</b> <span class="muted">{compare(weeks[0].totals.false_alarms, weeks[1]?.totals.false_alarms)}</span></li>
          </ul>
        )}
      </section>
    </section>
  );
}

function compare(a: number, b: number | undefined): string {
  if (b == null) return "";
  if (a > b) return t("homeMore", { n: a - b });
  if (a < b) return t("homeLess", { n: b - a });
  return t("homeSame");
}
