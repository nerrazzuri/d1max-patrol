// 布防值守(W73 第二期,设计 §5):布防时间页里的「值守方案」—— 哪条防区路线给哪台机器人、每圈隔多久;
// 下面是预报(用户 11-04:这段布防大约跑几圈、每个防区多久被看一次;电量放第四期)、此刻谁在跑、
// 每个防区多久没看,以及最近一段的值守记录。方案单独保存(它有自己的版本号,不跟时间表一起存)。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import { robotNames } from "./data";
import { lang, type Lang } from "./i18n";
import { clock } from "./routes";

export interface GuardEntry { id?: number; route: string; robot: string; gap_min: number; shuffle: boolean }
interface Window { day: number; start: string; end: string; minutes: number; laps: number | null }
interface FcEntry { id: number; route: string; robot: string; gap_min: number; lap_s: number | null; cycle_s: number | null; mode: "schedule" | "manual"; windows: Window[] }
interface FcZone { zone: string; routes: string[]; interval_s: number | null; limit_min: number; limit_set: boolean }
export interface Forecast { entries: FcEntry[]; zones: FcZone[]; warnings: string[] }
export interface Plan { revision: number; entries: GuardEntry[]; limits: Record<string, number>; forecast: Forecast; clashes?: string[] }
interface Run { entry: number; robot_id: string; state: string; lap: number; task_id: string; next_lap_ms: number; watch_zone: string }
interface ZoneSeen { zone: string; last_ms: number | null; robot_id: string; how: string; limit_min: number; armed: boolean; overdue: boolean }
export interface Status { duty: { id: number; started_ms: number } | null; runs: Run[]; waiting: Record<string, string>; zones: ZoneSeen[]; now_ms: number }
interface DutyZone { zone: string; looks: number; max_gap_s: number }
interface Duty { id: number; started_ms: number; ended_ms: number | null; laps: number; interruptions: number; missed: number; failed: number; zones: DutyZone[] }
interface LogRow { at_ms: number; robot_id: string; kind: string; zone: string; point: string; detail: string }

const en = {
  gTitle: "Guard duty",
  gLead: "While armed, robots run these zone routes lap after lap. Between laps they stand at a zone's watch post. Disarmed zones are skipped.",
  gNoRoutes: "No zone routes yet. Make one on Site layout → Zone routes (it can be generated from your zones).",
  gEmpty: "No guard duty set up: robots do not patrol on their own while armed.",
  gRoute: "Zone route",
  gRobot: "Robot",
  gAnyRobot: "Any available robot",
  gGap: "One lap every",
  gGapNone: "Back to back",
  gMin: "{n} min",
  gShuffle: "Random start and direction",
  gAdd: "Add a route",
  gRemove: "Remove",
  gSave: "Save guard duty",
  gSaved: "Guard duty saved",
  gDirty: "Guard duty has unsaved changes.",
  gFc: "Forecast",
  gLap: "One lap ≈ {t}",
  gLapUnknown: "Lap time unknown",
  gManual: "Not on a schedule: runs whenever the site is armed",
  gNoWindow: "The schedule never arms these zones",
  gWindow: "{d} {a}–{b}: about {n} laps",
  gWindowUnknown: "{d} {a}–{b}",
  gZoneEvery: "checked about every {t}",
  gZoneLimit: "alarm after {n} min unchecked",
  gLimitLabel: "Alarm after (min)",
  gLimitAuto: "auto",
  gWarn: "Notes",
  gClash: "Clashes with patrol schedules (fix one side)",
  gNow: "Right now",
  gOff: "Not on duty (nothing armed that a plan covers).",
  gOn: "On duty since {t}",
  gLapN: "lap {n}",
  stLap: "running {lap}",
  stWatch: "going to the {z} watch post",
  stPost: "watching {z} from its post",
  stWait: "waiting for the next lap",
  stResume: "interrupted, will carry on",
  stPaused: "stopped by a person, resting",
  gWaiting: "Waiting: {why}",
  gSeenAgo: "checked {t} ago",
  gSeenNever: "not checked yet",
  gSeenPost: "(from the watch post)",
  gOverdue: "overdue",
  gDisarmed: "disarmed",
  gRecord: "Last duty record",
  gRecordNone: "No duty records yet.",
  gRecordSpan: "{a} – {b}",
  gRecordRunning: "{a} – now",
  gRecordSum: "{laps} laps · {int} interruptions · {miss} missed points · {fail} failed laps",
  gRecordZone: "{z}: looked {n} times, longest gap {t}",
  gShowLog: "Show details",
  gHideLog: "Hide details",
  lgPoint: "reached",
  lgMissed: "missed",
  days: "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
};
type GKey = keyof typeof en;
const zh: Record<GKey, string> = {
  gTitle: "布防值守",
  gLead: "布防着的时候，机器人按这里配的防区路线一圈圈跑；两圈之间站到防区的值守位上盯着。撤防的防区跳过。",
  gNoRoutes: "还没有防区路线。在「场地布置 → 防区路线」里建一条（可以按防区自动生成）。",
  gEmpty: "还没配值守：布防时机器人不会自己出去巡。",
  gRoute: "防区路线",
  gRobot: "机器人",
  gAnyRobot: "任意一台空着的",
  gGap: "每圈间隔",
  gGapNone: "跑完马上下一圈",
  gMin: "{n} 分钟",
  gShuffle: "随机起点和方向",
  gAdd: "加一条路线",
  gRemove: "删除",
  gSave: "保存值守方案",
  gSaved: "值守方案已保存",
  gDirty: "值守方案有改动还没保存。",
  gFc: "预报",
  gLap: "一圈约 {t}",
  gLapUnknown: "一圈多久算不出",
  gManual: "没按时间表：布防着就一直跑",
  gNoWindow: "时间表里这些防区从不布防",
  gWindow: "{d} {a}–{b}：约 {n} 圈",
  gWindowUnknown: "{d} {a}–{b}",
  gZoneEvery: "约每 {t} 看一次",
  gZoneLimit: "超过 {n} 分钟没看报警",
  gLimitLabel: "超时报警（分钟）",
  gLimitAuto: "自动",
  gWarn: "提示",
  gClash: "跟巡逻排程撞了（改一边）",
  gNow: "此刻",
  gOff: "没在值守（有方案的防区都没布防）。",
  gOn: "从 {t} 起值守中",
  gLapN: "第 {n} 圈",
  stLap: "在跑{lap}",
  stWatch: "去{z}的值守位",
  stPost: "在值守位上盯着{z}",
  stWait: "等下一圈",
  stResume: "被叫走了，回来接着跑",
  stPaused: "被人中止，歇着",
  gWaiting: "在等：{why}",
  gSeenAgo: "{t} 前看过",
  gSeenNever: "还没看过",
  gSeenPost: "（值守位上）",
  gOverdue: "超时",
  gDisarmed: "撤防",
  gRecord: "最近一段值守记录",
  gRecordNone: "还没有值守记录。",
  gRecordSpan: "{a} – {b}",
  gRecordRunning: "{a} – 现在",
  gRecordSum: "跑完 {laps} 圈 · 被打断 {int} 次 · 没到的点 {miss} 个 · 跑不成 {fail} 圈",
  gRecordZone: "{z}：看了 {n} 次，最长隔 {t}",
  gShowLog: "看明细",
  gHideLog: "收起明细",
  lgPoint: "到了",
  lgMissed: "没到",
  days: "周一,周二,周三,周四,周五,周六,周日",
};
export const GDICTS: Record<Lang, Record<GKey, string>> = { en, zh };
export function gt(key: GKey, vars?: Record<string, string | number>): string {
  let s = GDICTS[lang.value][key] ?? en[key];
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

const GAPS = [0, 10, 15, 20, 30, 45, 60, 90, 120];
const day = (d: number) => gt("days").split(",")[d] ?? String(d);
const hhmm = (ms: number) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const stamp = (ms: number) => { const d = new Date(ms); return `${d.getMonth() + 1}/${d.getDate()} ${hhmm(ms)}`; };
const who = (rid: string) => robotNames.value[rid] || rid;
function errText(e: unknown): string { return e instanceof ApiError ? e.message : String(e); }
const clean = (es: GuardEntry[]) => es.map(({ route, robot, gap_min, shuffle }) => ({ route, robot, gap_min, shuffle }));

/** 这台此刻在干什么(一句话)。 */
export function runText(r: Run): string {
  const z = r.watch_zone;
  switch (r.state) {
    case "lap": return gt("stLap", { lap: gt("gLapN", { n: r.lap }) });
    case "watch": return gt("stWatch", { z });
    case "post": return gt("stPost", { z });
    case "wait": return gt("stWait");
    case "paused": return gt("stPaused");
    default: return gt("stResume");
  }
}

export function GuardPanel({ say, now, canEdit }: { say: (s: string) => void; now: number; canEdit: boolean }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [entries, setEntries] = useState<GuardEntry[]>([]);
  const [limits, setLimits] = useState<Record<string, number>>({});
  const [routes, setRoutes] = useState<string[]>([]);
  const [robots, setRobots] = useState<string[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [duty, setDuty] = useState<(Duty & { log?: LogRow[] }) | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [busy, setBusy] = useState(false);

  const take = (p: Plan) => { setPlan(p); setEntries(p.entries); setLimits(p.limits ?? {}); };
  const loadStatus = () => {
    api<Status>("GET", "/api/guard/status").then(setStatus).catch(() => setStatus(null));
    api<{ duties: Duty[] }>("GET", "/api/guard/duties").then((d) => setDuty(d.duties[0] ?? null)).catch(() => setDuty(null));
  };
  useEffect(() => {
    api<Plan>("GET", "/api/guard/plan").then(take).catch((e) => say(errText(e)));
    api<{ routes: { id: string }[] }>("GET", "/api/routes?kind=guard").then((d) => setRoutes(d.routes.map((r) => r.id))).catch(() => setRoutes([]));
    api<{ robots: { robot_id: string }[] }>("GET", "/api/robots").then((d) => setRobots(d.robots.map((r) => r.robot_id))).catch(() => setRobots([]));
    loadStatus();
    const t = setInterval(loadStatus, 15_000);
    return () => clearInterval(t);
  }, []);

  if (!plan) return null;
  const dirty = JSON.stringify(clean(entries)) !== JSON.stringify(clean(plan.entries))
    || JSON.stringify(limits) !== JSON.stringify(plan.limits ?? {});
  const set = (i: number, patch: Partial<GuardEntry>) => setEntries(entries.map((e, k) => (k === i ? { ...e, ...patch } : e)));

  async function save() {
    setBusy(true);
    try {
      take(await api<Plan>("POST", "/api/guard/plan", { entries: clean(entries), limits, base_revision: plan!.revision }));
      say(gt("gSaved"));
      loadStatus();
    } catch (e) {
      say(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const fc = plan.forecast;
  const seen = new Map((status?.zones ?? []).map((z) => [z.zone, z]));
  return (
    <section class="panel arm-guard">
      <div class="panel-head">
        <h2>{gt("gTitle")}</h2>
        {canEdit && <button type="button" class="btn primary push guard-save" disabled={busy || !dirty} onClick={() => void save()}>{gt("gSave")}</button>}
      </div>
      <p class="muted small">{gt("gLead")}</p>
      {dirty && <p class="arm-dirty" role="status">{gt("gDirty")}</p>}
      {routes.length === 0 && <p class="muted">{gt("gNoRoutes")}</p>}
      {entries.length === 0 && routes.length > 0 && <p class="muted">{gt("gEmpty")}</p>}
      <ul class="plain guard-entries">
        {entries.map((e, i) => (
          <li key={i} class="row wrap guard-entry">
            <label class="field">{gt("gRoute")}
              <select name="route" value={e.route} disabled={!canEdit} onChange={(ev) => set(i, { route: ev.currentTarget.value })}>
                {[...new Set([e.route, ...routes])].filter(Boolean).map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label class="field">{gt("gRobot")}
              <select name="robot" value={e.robot} disabled={!canEdit} onChange={(ev) => set(i, { robot: ev.currentTarget.value })}>
                <option value="">{gt("gAnyRobot")}</option>
                {[...new Set([...(e.robot ? [e.robot] : []), ...robots])].map((r) => <option key={r} value={r}>{who(r)}</option>)}
              </select>
            </label>
            <label class="field">{gt("gGap")}
              <select name="gap" value={String(e.gap_min)} disabled={!canEdit} onChange={(ev) => set(i, { gap_min: Number(ev.currentTarget.value) })}>
                {[...new Set([e.gap_min, ...GAPS])].sort((a, b) => a - b).map((g) => (
                  <option key={g} value={String(g)}>{g === 0 ? gt("gGapNone") : gt("gMin", { n: g })}</option>
                ))}
              </select>
            </label>
            <label class="guard-shuffle"><input type="checkbox" name="shuffle" checked={e.shuffle} disabled={!canEdit}
              onChange={(ev) => set(i, { shuffle: ev.currentTarget.checked })} /> {gt("gShuffle")}</label>
            {canEdit && <button type="button" class="btn sm push" onClick={() => setEntries(entries.filter((_, k) => k !== i))}>{gt("gRemove")}</button>}
          </li>
        ))}
      </ul>
      {canEdit && routes.length > 0 && (
        <button type="button" class="btn sm guard-add" onClick={() => setEntries([...entries, { route: routes[0], robot: "", gap_min: 0, shuffle: false }])}>{gt("gAdd")}</button>
      )}

      {(plan.clashes ?? []).length > 0 && (
        <div class="guard-warn guard-clash" role="alert">
          <h3>{gt("gClash")}</h3>
          <ul>{plan.clashes!.map((w) => <li key={w}>{w}</li>)}</ul>
        </div>
      )}
      {fc.entries.length > 0 && (
        <div class="guard-fc">
          <h3>{gt("gFc")}</h3>
          <ul class="plain">
            {fc.entries.map((e) => (
              <li key={e.id} class="guard-fc-entry">
                <b>{e.route}</b> · {e.robot ? who(e.robot) : gt("gAnyRobot")} · {e.lap_s ? gt("gLap", { t: clock(e.lap_s) }) : gt("gLapUnknown")}
                <div class="muted small">
                  {e.mode === "manual" ? gt("gManual") : e.windows.length === 0 ? gt("gNoWindow")
                    : e.windows.map((w) => (w.laps != null
                      ? gt("gWindow", { d: day(w.day), a: w.start, b: w.end, n: w.laps })
                      : gt("gWindowUnknown", { d: day(w.day), a: w.start, b: w.end }))).join("；")}
                </div>
              </li>
            ))}
          </ul>
          <table class="guard-zones">
            <tbody>
              {fc.zones.map((z) => {
                const s = seen.get(z.zone);
                return (
                  <tr key={z.zone} class={s?.overdue ? "overdue" : ""}>
                    <th>{z.zone}</th>
                    <td>{z.interval_s ? gt("gZoneEvery", { t: clock(z.interval_s) }) : "—"}</td>
                    <td>{gt("gZoneLimit", { n: z.limit_min })}</td>
                    <td>
                      {canEdit && (
                        <label class="guard-limit">{gt("gLimitLabel")}
                          <input type="number" name={`limit-${z.zone}`} min={1} max={1440} placeholder={gt("gLimitAuto")}
                            value={limits[z.zone] ?? ""}
                            onInput={(ev) => {
                              const v = ev.currentTarget.value;
                              const n = { ...limits };
                              if (v === "") delete n[z.zone]; else n[z.zone] = Math.max(1, Math.round(Number(v)));
                              setLimits(n);
                            }} />
                        </label>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {fc.warnings.length > 0 && (
            <div class="guard-warn">
              <h3>{gt("gWarn")}</h3>
              <ul>{fc.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {status && plan.entries.length > 0 && (
        <div class="guard-now">
          <h3>{gt("gNow")}</h3>
          <p>{status.duty ? gt("gOn", { t: hhmm(status.duty.started_ms) }) : gt("gOff")}</p>
          <ul class="plain">
            {status.runs.map((r) => <li key={r.entry}><b>{who(r.robot_id)}</b> {runText(r)}</li>)}
            {Object.entries(status.waiting).map(([k, why]) => <li key={`w${k}`} class="muted">{gt("gWaiting", { why })}</li>)}
          </ul>
          <ul class="plain guard-seen">
            {status.zones.map((z) => (
              <li key={z.zone} class={z.overdue ? "overdue" : ""}>
                <b>{z.zone}</b>{" "}
                {z.last_ms ? gt("gSeenAgo", { t: clock((now - z.last_ms) / 1000) }) : gt("gSeenNever")}
                {z.how === "post" && <span class="muted"> {gt("gSeenPost")}</span>}
                {!z.armed && <span class="muted"> · {gt("gDisarmed")}</span>}
                {z.overdue && <span class="tag bad"> {gt("gOverdue")}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {plan.entries.length > 0 && (
        <div class="guard-record">
          <h3>{gt("gRecord")}</h3>
          {!duty ? <p class="muted">{gt("gRecordNone")}</p> : (
            <>
              <p>{duty.ended_ms ? gt("gRecordSpan", { a: stamp(duty.started_ms), b: stamp(duty.ended_ms) }) : gt("gRecordRunning", { a: stamp(duty.started_ms) })}</p>
              <p class="muted small">{gt("gRecordSum", { laps: duty.laps, int: duty.interruptions, miss: duty.missed, fail: duty.failed })}</p>
              <ul class="plain small">
                {duty.zones.map((z) => <li key={z.zone}>{gt("gRecordZone", { z: z.zone, n: z.looks, t: clock(z.max_gap_s) })}</li>)}
              </ul>
              <button type="button" class="btn sm" onClick={() => {
                if (!showLog && !duty.log) {
                  api<Duty & { log: LogRow[] }>("GET", `/api/guard/duties/${duty.id}`).then(setDuty).catch((e) => say(errText(e)));
                }
                setShowLog(!showLog);
              }}>{showLog ? gt("gHideLog") : gt("gShowLog")}</button>
              {showLog && duty.log && (
                <ol class="plain small guard-log">
                  {duty.log.slice(-200).map((l, i) => (
                    <li key={i}><span class="mono">{hhmm(l.at_ms)}</span> {l.robot_id && who(l.robot_id)} {l.kind === "point" ? gt("lgPoint") : l.kind === "missed" ? gt("lgMissed") : ""} {l.zone} {l.point} {l.detail}</li>
                  ))}
                </ol>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
