// 定位历史页:每台狗定位丢了 / 变差的每一段(站点 /api/loc-episodes)—— 什么时候、多久、在哪、为什么、
// 在跑哪个任务。按狗、种类、时间段过滤。地图页上同一批记录画成点。不是匹配分数的热力图
// (错解也会打高分,把危险的地方画成绿的)。
import { useState } from "preact/hooks";
import { Shape } from "./icons";
import { L, durText, endText, kindText, useEpisodes, type Episode } from "./runviz";

const PERIODS: { id: string; ms: number; en: string; zh: string }[] = [
  { id: "1d", ms: 86_400_000, en: "Last 24 hours", zh: "最近 24 小时" },
  { id: "7d", ms: 7 * 86_400_000, en: "Last 7 days", zh: "最近 7 天" },
  { id: "30d", ms: 30 * 86_400_000, en: "Last 30 days", zh: "最近 30 天" },
  { id: "all", ms: 0, en: "All kept", zh: "全部" },
];

function stamp(ms: number): string {
  const d = new Date(ms);
  const two = (v: number) => String(v).padStart(2, "0");
  return `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

function where(e: Episode): string {
  return e.x == null || e.y == null ? "—" : `(${e.x.toFixed(1)}, ${e.y.toFixed(1)}) m`;
}

export function LocHistoryPage({ robots, now }: { robots: string[]; now: number }) {
  const [robot, setRobot] = useState("");
  const [kind, setKind] = useState("");
  const [period, setPeriod] = useState("7d");
  const ms = PERIODS.find((p) => p.id === period)?.ms ?? 0;
  // 取整到分钟:每秒重画不要每秒重新请求
  const since = ms ? Math.floor((now - ms) / 60_000) * 60_000 : undefined;
  const { rows, error, reload } = useEpisodes({ robot, kind, since_ms: since, limit: 500 });
  return (
    <section class="locpage" aria-label={L("Localization history", "定位历史")}>
      <div class="panel-head">
        <h1>{L("Localization history", "定位历史")}</h1>
        <span class="muted small push">{L("Each stretch where a robot said it was not localized or degraded", "狗自己说过「定位丢了 / 变差」的每一段")}</span>
      </div>
      <div class="filters">
        <label>{L("Robot", "狗")}
          <select value={robot} onChange={(e) => setRobot(e.currentTarget.value)}>
            <option value="">{L("All", "全部")}</option>
            {robots.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>{L("Kind", "种类")}
          <select value={kind} onChange={(e) => setKind(e.currentTarget.value)}>
            <option value="">{L("All", "全部")}</option>
            <option value="lost">{kindText("lost")}</option>
            <option value="degraded">{kindText("degraded")}</option>
          </select>
        </label>
        <label>{L("Period", "时间")}
          <select value={period} onChange={(e) => setPeriod(e.currentTarget.value)}>
            {PERIODS.map((p) => <option key={p.id} value={p.id}>{L(p.en, p.zh)}</option>)}
          </select>
        </label>
        <button type="button" class="btn sm" onClick={reload}>{L("Refresh", "刷新")}</button>
      </div>
      {error && <p class="state p2" role="alert"><Shape kind="P2" />{L("Could not load: {e}", "加载失败:{e}", { e: error })}</p>}
      <table class="grid">
        <thead>
          <tr>
            <th>{L("Started", "开始")}</th><th>{L("Duration", "时长")}</th><th>{L("Robot", "狗")}</th>
            <th>{L("Kind", "种类")}</th><th>{L("Where", "地点")}</th><th>{L("Why (robot's words)", "原因(狗的原话)")}</th>
            <th>{L("Task", "任务")}</th><th>{L("Ended", "怎么结束的")}</th><th />
          </tr>
        </thead>
        <tbody>
          {rows && rows.length === 0 && !error && <tr><td colSpan={9} class="muted">{L("No localization problems in this period.", "这段时间没有定位问题。")}</td></tr>}
          {(rows ?? []).map((e) => (
            <tr key={e.id} data-kind={e.kind}>
              <td class="mono">{stamp(e.start_ms)}</td>
              <td class="mono">{durText(e.duration_s)}</td>
              <td class="mono strong">{e.robot_id}</td>
              <td><span class={e.kind === "lost" ? "state p2" : "state"}>{e.kind === "lost" && <Shape kind="P2" />}{kindText(e.kind)}</span></td>
              <td class="mono">{where(e)}{e.map_id ? <span class="muted"> {e.map_id}:{e.map_version}</span> : null}</td>
              <td class="cause">{e.cause || "—"}</td>
              <td class="mono">{e.task_id || "—"}</td>
              <td class={e.ongoing ? "warncell" : "muted small"}>{endText(e.end_how, e.ongoing)}</td>
              <td>{e.x != null && <a class="link" href={`#/map?robot=${encodeURIComponent(e.robot_id)}`}>{L("Map", "地图")}</a>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
