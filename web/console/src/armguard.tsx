// 布防值守(W73 第二期,设计 §5):布防时间页里的「值守方案」—— 哪条防区路线给哪台机器人、每圈隔多久;
// 下面是预报(用户 11-04:这段布防大约跑几圈、每个防区多久被看一次;电量放第四期)、此刻谁在跑、
// 每个防区多久没看,以及最近一段的值守记录。方案单独保存(它有自己的版本号,不跟时间表一起存)。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import { robotNames } from "./data";
import { lang, type Lang } from "./i18n";
import { clock } from "./routes";

// end_standby:每圈结束回待命点(11-04 用户,默认开;老站点不带这个键 = 开)
export interface GuardEntry { id?: number; route: string; robot: string; gap_min: number; shuffle: boolean; end_standby?: boolean }
interface Window { day: number; start: string; end: string; minutes: number; laps: number | null; topups?: number | null }
interface FcEntry { id: number; route: string; robot: string; gap_min: number; lap_s: number | null; cycle_s: number | null; mode: "schedule" | "manual"; windows: Window[]; laps_per_charge?: number | null }
interface FcZone { zone: string; routes: string[]; interval_s: number | null; limit_min: number; limit_set: boolean }
export interface Forecast { entries: FcEntry[]; zones: FcZone[]; warnings: string[] }
export interface Plan { revision: number; entries: GuardEntry[]; limits: Record<string, number>; forecast: Forecast; clashes?: string[] }
interface Run { entry: number; robot_id: string; state: string; lap: number; task_id: string; next_lap_ms: number; watch_zone: string }
interface ZoneSeen { zone: string; last_ms: number | null; robot_id: string; how: string; limit_min: number; armed: boolean; overdue: boolean }
/** W73 第四期:没桩的换电进度(站点按两块电池的在位、电量判)。 */
export interface Swap { robot_id: string; step: "going" | "first" | "first_out" | "first_low" | "second" | "second_out" | "wrong_out" | "any"; first: number | null; second: number | null; packs: (number | null)[] | null; battery_pct: number | null; since_ms: number; told_ms: number | null; ok_pct: number }
export interface Status { duty: { id: number; started_ms: number } | null; runs: Run[]; waiting: Record<string, string>; zones: ZoneSeen[]; now_ms: number; swaps?: Swap[] }
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
  gEndStandby: "Return to standby point after each lap",
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
  gTopups: " ({n} top-ups)",
  gPerCharge: "about {n} laps per charge",
  gTimeline: "Timeline: on duty / topping up",
  gTlDuty: "on duty",
  gTlTopup: "topping up (zones on this route unwatched)",
  gSwap: "Battery swap — {r}",
  gSwapGoing: "Heading to the swap spot",
  gSwapFirst: "Swap pack {a} first (the lower one). Leave pack {b} in.",
  gSwapFirstOut: "Pack {a} is out — do not touch pack {b}. Insert a full pack {a}.",
  gSwapFirstLow: "The new pack {a} reads below {p}% — use a full pack.",
  gSwapSecond: "Pack {a} done. Now swap pack {b}.",
  gSwapSecondOut: "Pack {b} is out — insert a full pack {b}.",
  gSwapWrongOut: "Pack {b} was pulled — put it back first. Pack {a} is not done yet; never pull both.",
  gSwapAny: "Swap in a full battery (hot swap, the robot stays standing).",
  gPack: "Pack {n}: {v}",
  gPackOut: "out",
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
  stHeld: "saw a person: holding still and watching, waiting for the guard",
  stStopping: "stopping this lap, waiting for the robot to confirm",
  stStandDown: "standing down: stopping the robot first, then back to standby",
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
  gEndStandby: "每圈结束回待命点",
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
  gTopups: "（要补能 {n} 次）",
  gPerCharge: "补一次能约跑 {n} 圈",
  gTimeline: "时间轴：值守 / 补能",
  gTlDuty: "值守",
  gTlTopup: "补能（这条路线的防区没人看）",
  gSwap: "换电池 — {r}",
  gSwapGoing: "正在去换电位",
  gSwapFirst: "先换 {a} 号（电量低的那块），{b} 号别动。",
  gSwapFirstOut: "{a} 号已拔下 —— 不要动 {b} 号。插上满电的 {a} 号。",
  gSwapFirstLow: "新插的 {a} 号电量不到 {p}%，请换满电的。",
  gSwapSecond: "{a} 号换好了。现在换 {b} 号。",
  gSwapSecondOut: "{b} 号已拔下 —— 插上满电的 {b} 号。",
  gSwapWrongOut: "{b} 号被拔了 —— 先把它装回去。{a} 号还没换好，两块不要同时拔。",
  gSwapAny: "换上满电电池（热插拔，机器人站着不断电）。",
  gPack: "{n} 号：{v}",
  gPackOut: "未插",
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
  stHeld: "看见人：原地盯着，等保安处理",
  stStopping: "正在停下这一圈，等机器人确认",
  stStandDown: "收队中：先让机器人停下，再回待命点",
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
const clean = (es: GuardEntry[]) => es.map(({ route, robot, gap_min, shuffle, end_standby }) =>
  ({ route, robot, gap_min, shuffle, end_standby: end_standby !== false }));

/** 这台此刻在干什么(一句话)。 */
export function runText(r: Run): string {
  const z = r.watch_zone;
  switch (r.state) {
    case "lap": return gt("stLap", { lap: gt("gLapN", { n: r.lap }) });
    case "watch": return gt("stWatch", { z });
    case "post": return gt("stPost", { z });
    case "wait": return gt("stWait");
    case "paused": return gt("stPaused");
    case "held": return gt("stHeld");
    case "stopping": return gt("stStopping");
    case "standdown": return gt("stStandDown");
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
            <label class="guard-shuffle"><input type="checkbox" name="end_standby" checked={e.end_standby !== false} disabled={!canEdit}
              onChange={(ev) => set(i, { end_standby: ev.currentTarget.checked })} /> {gt("gEndStandby")}</label>
            {canEdit && <button type="button" class="btn sm push" onClick={() => setEntries(entries.filter((_, k) => k !== i))}>{gt("gRemove")}</button>}
          </li>
        ))}
      </ul>
      {canEdit && routes.length > 0 && (
        <button type="button" class="btn sm guard-add" onClick={() => setEntries([...entries, { route: routes[0], robot: "", gap_min: 0, shuffle: false, end_standby: true }])}>{gt("gAdd")}</button>
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
                {e.laps_per_charge ? <span class="muted"> · {gt("gPerCharge", { n: e.laps_per_charge })}</span> : null}
                <div class="muted small">
                  {e.mode === "manual" ? gt("gManual") : e.windows.length === 0 ? gt("gNoWindow")
                    : e.windows.map((w) => (w.laps != null
                      ? gt("gWindow", { d: day(w.day), a: w.start, b: w.end, n: w.laps }) + (w.topups ? gt("gTopups", { n: w.topups }) : "")
                      : gt("gWindowUnknown", { d: day(w.day), a: w.start, b: w.end }))).join("；")}
                </div>
                {e.windows.slice(0, 1).map((w) => <Timeline key="tl" w={w} perCharge={e.laps_per_charge ?? null} />)}
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

      {(status?.swaps ?? []).map((w) => <SwapCard key={w.robot_id} w={w} who={who} />)}

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

/** W73 第四期 换电引导:一块一块换,进度跟着电池走(站点判),保安不用点「下一步」。 */
export function swapText(w: Swap): string {
  const a = w.first ?? 1, b = w.second ?? 2;
  switch (w.step) {
    case "going": return gt("gSwapGoing");
    case "first": return gt("gSwapFirst", { a, b });
    case "first_out": return gt("gSwapFirstOut", { a, b });
    case "first_low": return gt("gSwapFirstLow", { a, p: w.ok_pct });
    case "second": return gt("gSwapSecond", { a, b });
    case "second_out": return gt("gSwapSecondOut", { b });
    case "wrong_out": return gt("gSwapWrongOut", { a, b });
    default: return gt("gSwapAny");
  }
}

function SwapCard({ w, who }: { w: Swap; who: (r: string) => string }) {
  return (
    <div class="guard-swap" role="status">
      <h3>{gt("gSwap", { r: who(w.robot_id) })}</h3>
      <p class={w.step.endsWith("_out") ? "guard-swap-step hot" : "guard-swap-step"}>{swapText(w)}</p>
      {w.packs && (
        <ul class="plain guard-packs">
          {w.packs.map((v, i) => (
            <li key={i} class={i + 1 === w.first ? "first" : ""}>
              {gt("gPack", { n: i + 1, v: v == null ? gt("gPackOut") : `${Math.round(v)}%` })}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** W73 §7.6 时间轴(简版):一段布防里值守 / 补能各占多少;补能按每 N 圈一次、每次约 8 分钟画。 */
function Timeline({ w, perCharge }: { w: Window; perCharge: number | null }) {
  if (!w.laps || !perCharge || !w.topups) return null;
  const gap = 8;                                   // 分钟,跟站点 energy.SWAP_GAP_MIN 一致
  const lap = w.minutes / w.laps;
  const parts: { kind: "duty" | "topup"; min: number }[] = [];
  let left = w.minutes;
  for (let i = 0; i < w.topups && left > 0; i++) {
    const run = Math.min(left, perCharge * lap);
    parts.push({ kind: "duty", min: run });
    left -= run;
    const g = Math.min(left, gap);
    if (g > 0) { parts.push({ kind: "topup", min: g }); left -= g; }
  }
  if (left > 0) parts.push({ kind: "duty", min: left });
  return (
    <div class="guard-tl" aria-label={gt("gTimeline")}>
      {parts.map((p, i) => (
        <span key={i} class={p.kind === "topup" ? "guard-tl-topup" : "guard-tl-duty"}
          style={{ flexGrow: p.min }} title={p.kind === "topup" ? gt("gTlTopup") : gt("gTlDuty")} />
      ))}
    </div>
  );
}
