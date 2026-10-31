// 报表页(W44 第二期,设计 §5.1「报表」;原来的值班日报换掉):选一段时间(这周 / 上周 / 这个月 / 自己选,最长 31 天),
// 四个大数字(巡了多少趟、出了几次事、误报几次、机器人出问题几次),按天柱状图,事件列表(点一下看录像)。
// 导出 PDF = 浏览器打印(打印样式只留标题、时间段、四个数、柱子、事件表);导出表格 = 事件的 CSV。
// 数是站点按站点时区数的,网页不凑;读到的那一份只在它就是现在选的那一段时才显示、才能导出(外审 I2 同一条规矩)。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { hm, nameOf } from "./data";
import { alertTitle, DICTS, lang, t, type Key } from "./i18n";
import { eventsCsv, fileTag, kpiDelta, matches, reportRange, type KpiKey, type Kpis, type ReportKind, type Summary, type SummaryEvent } from "./reports";

/** W60 关键指标分两组:安防运营、系统可靠性。 */
const KPI_GROUPS: { key: Key; items: { k: KpiKey; key: Key }[] }[] = [
  { key: "kpiOps", items: [{ k: "patrol_completion", key: "kpiCompletion" }, { k: "response_s", key: "kpiResponse" }, { k: "ack_rate", key: "kpiAck" }] },
  { key: "kpiSys", items: [{ k: "robot_availability", key: "kpiRobotAvail" }, { k: "camera_availability", key: "kpiCamAvail" },
    { k: "intervention_rate", key: "kpiIntervention" }] },
];

function kpiValue(k: KpiKey, v: number | null): { n: string; unit: string } {
  if (v == null) return { n: "—", unit: "" };
  if (k === "response_s") return { n: String(Math.round(v)), unit: t("kpiSec") };
  return { n: (Math.round(v * 1000) / 10).toString(), unit: "%" };
}

function KpiCards({ cur, prev }: { cur: Kpis; prev?: Kpis }) {
  return (
    <div class="kpi-groups">
      {KPI_GROUPS.map((g) => (
        <section key={g.key} class="kpi-group" aria-label={t(g.key)}>
          <h2 class="kpi-group-h">{t(g.key)}</h2>
          <div class="kpi-row">
            {g.items.map((x) => {
              const v = kpiValue(x.k, cur[x.k]);
              const d = kpiDelta(x.k, cur, prev);
              return (
                <div key={x.k} class="kpi" data-kpi={x.k}>
                  <span class="kpi-l">{t(x.key)}</span>
                  <span class="kpi-v">{v.n}<small>{v.unit}</small></span>
                  <span class={`kpi-d ${d ? d.tone : "flat"}`}>
                    {d == null ? t("kpiNoPrev") : d.d === 0 ? t("kpiFlat")
                      : t(x.k === "response_s" ? "kpiDeltaSec" : "kpiDeltaPts", { a: d.d > 0 ? "↑" : "↓", n: Math.abs(d.d) })}
                  </span>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

/** 入侵事件、排程的结果:认得的翻成人话,不认得的原样摆(站点以后可能多出新的)。 */
export function outcomeText(v: string | null): string {
  if (!v) return "—";
  const k = `out_${v}`;
  return k in DICTS.en ? t(k as Key) : v;
}

function save(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function verdictText(v: string): string {
  return v === "intruder" ? t("vIntruder") : v === "false_alarm" ? t("vFalse") : v === "handled" ? t("vHandled") : "";
}

const KINDS: { v: ReportKind; key: Key }[] = [
  { v: "week", key: "rsWeek" }, { v: "lastweek", key: "rsLastWeek" }, { v: "month", key: "rsMonth" }, { v: "custom", key: "rsCustom" },
];

function ymd(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function ReportsPage({ now }: { now: number }) {
  const [kind, setKind] = useState<ReportKind>("week");
  const [from, setFrom] = useState(ymd(now - 6 * 86_400_000));
  const [to, setTo] = useState(ymd(now));
  const [got, setGot] = useState<Summary | null>(null);
  const [failed, setFailed] = useState(false);
  void lang.value;
  const p = reportRange(kind, now, from, to);
  const key = p ? `${p.since}-${p.until}` : "";
  const d = matches(got, p) ? got : null;

  useEffect(() => {
    if (!p) return;
    let live = true;
    setFailed(false);
    api<Summary>("GET", `/api/reports/summary?since=${p.since}&until=${p.until}`)
      .then((x) => { if (live) setGot(x); })
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [key]);

  const max = d ? Math.max(1, ...d.days.map((x) => x.patrols + x.incidents + x.robot_problems)) : 1;
  const title = (k: string, tt: string) => alertTitle(lang.value, k, tt);
  // 摄像头、站点自己报的事(robot 是 "site"):写哪个防区,不写 site(演示站点实测查出)
  const who = (e: SummaryEvent) => (e.robot === "site" ? (e.zone ? t("rsCamZone", { z: e.zone }) : "—") : nameOf(e.robot));
  return (
    <section class="reports">
      <div class="panel-head noprint">
        <h1>{t("tabReports")}</h1>
        <button type="button" class="btn push" disabled={!d} onClick={() => d && save(`report-${fileTag(d.since_ms, d.until_ms)}.csv`,
          eventsCsv(d.events, (r) => title(r.kind, r.title), (v) => verdictText(v) || v, who))}>{t("rsCsv")}</button>
        <button type="button" class="btn primary" disabled={!d} onClick={() => window.print()}>{t("rsPdf")}</button>
      </div>
      <h1 class="only-print">{t("tabReports")}</h1>
      <div class="row wrap noprint">
        <div class="seg" role="group" aria-label={t("rsPeriod")}>
          {KINDS.map((k) => <button key={k.v} type="button" aria-pressed={kind === k.v} onClick={() => setKind(k.v)}>{t(k.key)}</button>)}
        </div>
        {kind === "custom" && (
          <>
            <label class="field">{t("from")}<input name="from" type="date" value={from} onInput={(e) => setFrom(e.currentTarget.value)} /></label>
            <label class="field">{t("to")}<input name="to" type="date" value={to} onInput={(e) => setTo(e.currentTarget.value)} /></label>
          </>
        )}
      </div>
      {!p ? <p class="error">{t("rsTooLong")}</p> : (
        <p class="muted">{t("rsRange", { a: ymd(p.since), b: ymd(p.until - 1) })}</p>
      )}
      {p && failed && <p class="error">{t("repFailed")}</p>}
      {p && !d && !failed && <p class="muted">…</p>}
      {p && d && (
        <>
          {d.kpis && <KpiCards cur={d.kpis} prev={d.kpis_prev} />}
          <div class="rep-stats">
            <div class="stat"><span class="stat-n">{d.totals.patrols}</span><span class="stat-l">{t("rsPatrols")}</span></div>
            <div class="stat"><span class="stat-n">{d.totals.incidents}</span><span class="stat-l">{t("rsIncidents")}</span></div>
            <div class="stat"><span class="stat-n">{d.totals.false_alarms}</span><span class="stat-l">{t("rsFalse")}</span></div>
            <div class="stat"><span class="stat-n">{d.totals.robot_problems}</span><span class="stat-l">{t("rsProblems")}</span></div>
          </div>
          <div class="rep-chart" role="img" aria-label={t("rsChart")}>
            {d.days.map((x) => {
              const all = x.patrols + x.incidents + x.robot_problems;
              return (
                <div key={x.day} class="rep-day" data-day={x.day} title={`${x.day}: ${t("rsPatrols")} ${x.patrols} · ${t("rsIncidents")} ${x.incidents} · ${t("rsProblems")} ${x.robot_problems}`}>
                  <div class="rep-col">
                    <div class="rep-bar" style={{ height: `${(all / max) * 100}%` }}>
                      {x.incidents > 0 && <i class="seg-inc" style={{ flex: x.incidents }} />}
                      {x.robot_problems > 0 && <i class="seg-help" style={{ flex: x.robot_problems }} />}
                      {x.patrols > 0 && <i class="seg-pat" style={{ flex: x.patrols }} />}
                    </div>
                  </div>
                  <span class="rep-lbl">{x.day.slice(8)}</span>
                </div>
              );
            })}
          </div>
          <p class="rep-legend small muted">
            <span><i class="seg-pat" /> {t("rsPatrols")}</span> <span><i class="seg-inc" /> {t("rsIncidents")}</span> <span><i class="seg-help" /> {t("rsProblems")}</span>
          </p>
          <h2>{t("rsEvents")}</h2>
          {d.events.length === 0 ? <p class="muted">{t("rsNoEvents")}</p> : (
            <table class="grid rep-events">
              <thead><tr><th>{t("colTime")}</th><th>{t("robotsShort")}</th><th>{t("rsWhat")}</th><th>{t("rsVerdict")}</th></tr></thead>
              <tbody>
                {d.events.map((e) => (
                  <tr key={e.key} data-key={e.key} class={e.robot === "site" ? undefined : "clickable"} tabIndex={e.robot === "site" ? undefined : 0}
                    onClick={e.robot === "site" ? undefined : () => { location.hash = `#/recordings?robot=${encodeURIComponent(e.robot)}&t=${e.first_ms}`; }}>
                    <td class="mono">{ymd(e.first_ms).slice(5)} {hm(e.first_ms)}</td>
                    <td>{who(e)}</td>
                    <td>{title(e.kind, e.title)}</td>
                    <td>{e.verdict ? verdictText(e.verdict) : "—"}{e.resolved_by ? ` · ${e.resolved_by}` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {d.truncated && <p class="muted small">{t("rsTruncated", { n: d.events.length })}</p>}
        </>
      )}
    </section>
  );
}
