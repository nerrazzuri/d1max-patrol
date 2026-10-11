// 报表页(C-B3b):值班日报。选一天、几点到几点(结束不比开始晚 = 跨夜的班),一页看完:告警各级多少、
// 多久有人确认、谁处理的;入侵事件和出警结果;排程巡逻跑了几趟、成了几趟;运行记录。能打印、能导出 CSV。
// 钟点是 24 小时制的文字框(浏览器自带的时间框跟着系统,会变成上午下午)。数是站点数的,不在浏览器里凑;明细截了会写明。颜色只给 P1 / P2,别的数都是中性的。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import type { Me } from "./app";
import { hms } from "./data";
import { alertTitle, DICTS, lang, t, type Key } from "./i18n";
import { dateInput } from "./recordings";
import { alarmsCsv, fileTag, incidentsCsv, matches, patrolsCsv, period, secondsText, stamp, type Daily } from "./reports";

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

function Stat({ n, label, cls }: { n: string | number; label: string; cls?: string }) {
  return (
    <div class="stat">
      <span class={cls ? `stat-n ${cls}` : "stat-n"}>{n}</span>
      <span class="stat-l">{label}</span>
    </div>
  );
}

function Counts({ by }: { by: Record<string, number> }) {
  const keys = Object.keys(by).sort((a, b) => by[b] - by[a] || a.localeCompare(b));
  if (!keys.length) return <span class="muted">—</span>;
  return <span>{keys.map((k) => `${outcomeText(k)} ${by[k]}`).join(" · ")}</span>;
}

export function ReportsPage({ me, now }: { me: Me; now: number }) {
  const [date, setDate] = useState(dateInput(now));
  const [from, setFrom] = useState("00:00");
  const [to, setTo] = useState("00:00");
  const [got, setD] = useState<Daily | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "failed" | "bad">("loading");
  const [madeAt, setMadeAt] = useState(now);
  const [nonce, setNonce] = useState(0);
  const p = period(date, from, to);
  void lang.value;
  // 读到的那一份只在它就是现在选的时间段时才算数(外审 I2):换了日期、新的还没回来,旧的不显示、
  // 不能打印、不能导出。文件名按读到的那一份的头尾写,不按筛选框。
  const d = matches(got, p) ? got : null;

  useEffect(() => {
    if (!p) {
      setState("bad");
      setD(null);
      return;
    }
    let live = true;
    setState("loading");
    api<Daily>("GET", `/api/reports/daily?since=${p.since}&until=${p.until}`)
      .then((x) => {
        if (!live) return;
        setD(x);
        setMadeAt(Date.now());
        setState("ok");
      })
      .catch(() => {
        if (!live) return;
        setD(null);
        setState("failed");
      });
    return () => { live = false; };
  }, [p?.since, p?.until, nonce]);

  const tag = d ? fileTag(d.since_ms, d.until_ms) : "";
  const a = d?.alarms;
  return (
    <div class="report">
      <div class="recbar noprint">
        <label>{t("recDate")}
          <input type="date" value={date} max={dateInput(now)} onChange={(e) => setDate((e.target as HTMLInputElement).value)} />
        </label>
        <label>{t("repFrom")}
          <input type="text" class="mono hhmm" inputMode="numeric" maxLength={5} placeholder="HH:MM" value={from} onInput={(e) => setFrom((e.target as HTMLInputElement).value)} />
        </label>
        <label>{t("repTo")}
          <input type="text" class="mono hhmm" inputMode="numeric" maxLength={5} placeholder="HH:MM" value={to} onInput={(e) => setTo((e.target as HTMLInputElement).value)} />
        </label>
        <button type="button" class="btn sm" onClick={() => { setFrom("00:00"); setTo("00:00"); }}>{t("repWholeDay")}</button>
        <button type="button" class="btn sm" onClick={() => setNonce((n) => n + 1)}>{t("repRefresh")}</button>
        <span class="push row">
          <button type="button" class="btn sm" disabled={!d} onClick={() => window.print()}>{t("repPrint")}</button>
          <button type="button" class="btn sm" disabled={!a?.rows.length} onClick={() => a && save(`alarms_${tag}.csv`, alarmsCsv(a.rows, (r) => alertTitle(lang.value, r.kind, r.title)))}>{t("repCsvAlarms")}</button>
          <button type="button" class="btn sm" disabled={!d?.incidents.rows.length} onClick={() => d && save(`incidents_${tag}.csv`, incidentsCsv(d.incidents.rows))}>{t("repCsvIncidents")}</button>
          <button type="button" class="btn sm" disabled={!d?.patrols.rows.length} onClick={() => d && save(`patrols_${tag}.csv`, patrolsCsv(d.patrols.rows))}>{t("repCsvPatrols")}</button>
        </span>
      </div>

      {state === "bad" && <p class="empty">{t("repBadPeriod")}</p>}
      {state === "failed" && <p class="empty">{t("repFailed")}</p>}
      {state !== "bad" && state !== "failed" && !d && <p class="empty">{t("loading")}</p>}

      {d && a && p && (
        <article class="sheet">
          <header class="sheet-head">
            <h1>{t("repTitle")} · {me.site_name}</h1>
            <p class="mono">{stamp(d.since_ms)} → {stamp(d.until_ms)}</p>
            <p class="muted small">{t("repMadeBy", { who: me.display_name || me.name, t: stamp(madeAt) })}</p>
          </header>

          <section>
            <h2>{t("repAlarms")}</h2>
            <div class="stats">
              <Stat n={a.by_level.P1} label="P1" cls={a.by_level.P1 > 0 ? "pri P1" : undefined} />
              <Stat n={a.by_level.P2} label="P2" cls={a.by_level.P2 > 0 ? "pri P2" : undefined} />
              <Stat n={a.by_level.P3} label="P3" />
              <Stat n={a.waiting} label={t("repWaiting")} cls={a.waiting > 0 ? "pri P2" : undefined} />
              <Stat n={a.closed_unacked} label={t("repClosedUnacked")} />
              <Stat n={secondsText(a.ack.P1.median_s)} label={t("repAckMedian", { n: a.ack.P1.n })} />
              <Stat n={secondsText(a.ack.P1.max_s)} label={t("repAckMax")} />
            </div>
            {a.handlers.length > 0 && (
              <table class="reptable narrow">
                <caption>{t("repHandlers")}</caption>
                <thead><tr><th>{t("repWho")}</th><th class="num">{t("repAcked")}</th><th class="num">{t("repClosed")}</th></tr></thead>
                <tbody>
                  {a.handlers.map((h) => (
                    <tr key={h.name}><td>{h.name || t("repAuto")}</td><td class="num">{h.acked}</td><td class="num">{h.resolved}</td></tr>
                  ))}
                </tbody>
              </table>
            )}
            {a.by_kind.length > 0 && (
              <table class="reptable narrow">
                <caption>{t("repByKind")}</caption>
                <tbody>
                  {a.by_kind.map((k) => (
                    <tr key={`${k.level}/${k.kind}`}><td><span class={`pri ${k.level}`}>{k.level}</span></td><td>{alertTitle(lang.value, k.kind, k.kind)}</td><td class="num">{k.n}</td></tr>
                  ))}
                </tbody>
              </table>
            )}
            <table class="reptable">
              <caption>{t("repAlarmList", { n: a.total })}{a.truncated ? ` — ${t("repTruncated", { n: a.rows.length })}` : ""}</caption>
              <thead><tr><th>{t("colTime")}</th><th>{t("colPriority")}</th><th>{t("colEvent")}</th><th>{t("colSource")}</th><th>{t("colLocation")}</th><th>{t("repAckedCol")}</th><th>{t("repClosedCol")}</th></tr></thead>
              <tbody>
                {a.rows.length === 0 && <tr><td colSpan={7} class="muted">{t("repNone")}</td></tr>}
                {a.rows.map((r) => (
                  <tr key={r.key}>
                    <td class="mono">{hms(r.first_ms)}</td>
                    <td><span class={`pri ${r.level}`}>{r.level}</span></td>
                    <td>{alertTitle(lang.value, r.kind, r.title)}{r.count > 1 ? ` ×${r.count}` : ""}</td>
                    <td>{r.robot}</td>
                    <td>{r.zone ?? "—"}</td>
                    <td>{r.acked_ms != null ? `${r.acked_by} · ${secondsText(Math.max(0, (r.acked_ms - r.first_ms) / 1000))}` : "—"}</td>
                    <td>{r.resolved_ms != null ? `${hms(r.resolved_ms)}${r.resolved_by ? ` · ${r.resolved_by}` : ""}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section>
            <h2>{t("repIncidents")}</h2>
            <p>{t("repTotal", { n: d.incidents.total })} <Counts by={d.incidents.by_outcome} /></p>
            {d.incidents.rows.length > 0 && (
              <table class="reptable">
                <caption>{d.incidents.truncated ? t("repTruncated", { n: d.incidents.rows.length }) : ""}</caption>
                <thead><tr><th>{t("colTime")}</th><th>{t("colLocation")}</th><th>{t("repSource")}</th><th>{t("repOutcome")}</th><th>{t("recRobot")}</th><th>{t("repResult")}</th></tr></thead>
                <tbody>
                  {d.incidents.rows.map((r) => (
                    <tr key={r.id}>
                      <td class="mono">{hms(r.received_at)}</td><td>{r.zone}{r.intercept ? ` → ${r.intercept}` : ""}</td><td>{r.source}</td>
                      <td>{outcomeText(r.outcome)}</td><td>{r.robot_id ?? "—"}</td><td>{outcomeText(r.result)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section>
            <h2>{t("repPatrols")}</h2>
            <p>{t("repPatrolTotal", { n: d.patrols.total })} <Counts by={d.patrols.by_outcome} /></p>
            {d.patrols.started > 0 && <p>{t("repStartedResults", { n: d.patrols.started })} <Counts by={d.patrols.by_result} /></p>}
            {d.patrols.rows.length > 0 && (
              <table class="reptable">
                <caption>{d.patrols.truncated ? t("repTruncated", { n: d.patrols.rows.length }) : ""}</caption>
                <thead><tr><th>{t("colTime")}</th><th>{t("repSchedule")}</th><th>{t("repOutcome")}</th><th>{t("recRobot")}</th><th>{t("repResult")}</th><th>{t("repNote")}</th></tr></thead>
                <tbody>
                  {d.patrols.rows.map((r) => (
                    <tr key={r.id}>
                      <td class="mono">{hms(r.scheduled_ms)}</td><td>{r.entry_id}</td><td>{outcomeText(r.outcome)}</td>
                      <td>{r.robot_id ?? "—"}</td><td>{r.outcome === "started" ? outcomeText(r.result ?? "running") : "—"}</td><td>{r.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section>
            <h2>{t("repEvidence")}</h2>
            <p>{t("repRuns", { n: d.runs.total, f: d.runs.finished, p: d.runs.photos })}</p>
          </section>

          <footer class="muted small">{t("repNotIncluded")}</footer>
        </article>
      )}
    </div>
  );
}
