// 报警队列(B0 专业版,ISA-18.2):底部常驻,Active / Shelved / History 三页。P1、P2 进队列,P3 不进。
// 未确认的在前;只有未确认的 P1 闪(跟报警画面同步);P2 可以搁置(要原因、到点回来),P1 不行。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { hm, hms } from "./data";
import { alertTitle, lang, t, type Key } from "./i18n";
import { Shape } from "./icons";
import { alarmState, type Alert, type AlarmState } from "./model";

const STATE_KEY: Record<AlarmState, Key> = {
  unacked: "stUnacked",
  new: "stNew",
  acked: "stAcked",
  shelved: "stShelved",
  resolved: "stResolved",
};

type Tab = "active" | "shelved" | "history";

export function AlarmQueue(props: {
  active: Alert[];
  shelved: Alert[];
  now: number;
  open: boolean;
  onToggle: () => void;
  onAck: (a: Alert) => void;
  onShelve: (a: Alert) => void;
  onUnshelve: (a: Alert) => void;
  onGo: (a: Alert) => void;
}) {
  const [tab, setTab] = useState<Tab>("active");
  const [history, setHistory] = useState<Alert[]>([]);
  useEffect(() => {
    if (tab !== "history" || !props.open) return;
    api<{ alerts: Alert[] }>("GET", "/api/alerts?all=1&limit=200")
      .then((d) => setHistory(d.alerts.filter((a) => a.level !== "P3")))
      .catch(() => setHistory([]));
  }, [tab, props.open]);
  const rows = tab === "active" ? props.active : tab === "shelved" ? props.shelved : history;
  const top = props.active[0];
  return (
    <section class={props.open ? "queue open" : "queue"} aria-label={t("tabAlarms")}>
      <div class="queue-head">
        <h2>{props.active.length ? t("alarmsActive", { n: props.active.length }) : t("noActiveAlarms")}</h2>
        {props.open ? (
          <div class="tabs" role="tablist">
            {(["active", "shelved", "history"] as Tab[]).map((x) => (
              <button key={x} type="button" role="tab" aria-selected={tab === x} onClick={() => setTab(x)}>
                {x === "active" ? t("qActive") : x === "shelved" ? t("qShelved", { n: props.shelved.length }) : t("qHistory")}
              </button>
            ))}
          </div>
        ) : (
          top && <span class="queue-peek"><Pri a={top} now={props.now} /> {hms(top.first_ms)} · {top.robot} · {alertTitle(lang.value, top.kind, top.title)}</span>
        )}
        <button type="button" class="link push" aria-expanded={props.open} onClick={props.onToggle}>
          {props.open ? t("collapse") : t("expand")}
        </button>
      </div>
      {props.open && (
        <div class="queue-body">
          <table>
            <thead>
              <tr>
                <th>{t("colPriority")}</th><th>{t("colState")}</th><th>{t("colTime")}</th><th>{t("colSource")}</th>
                <th>{t("colEvent")}</th><th>{t("colLocation")}</th><th>{t("colHandledBy")}</th><th>{t("colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const st = alarmState(a, props.now);
                return (
                  <tr key={a.key} class={`row-${a.level} st-${st}`}>
                    <td><Pri a={a} now={props.now} /></td>
                    <td>{t(STATE_KEY[st])}{st === "shelved" && a.shelved_until_ms ? ` · ${hm(a.shelved_until_ms)}` : ""}</td>
                    <td class="mono">{hms(a.first_ms)}</td>
                    <td>{a.robot}</td>
                    <td>{alertTitle(lang.value, a.kind, a.title)}{st === "shelved" && a.shelved_reason ? <span class="muted"> — {a.shelved_reason}</span> : null}</td>
                    <td>{a.context?.zone ?? "—"}</td>
                    <td>{a.acked_by || a.shelved_by || "—"}</td>
                    <td class="actions">
                      {st === "unacked" && <button type="button" class="btn primary sm" onClick={() => props.onAck(a)}>{t("take")}</button>}
                      {st === "new" && <button type="button" class="btn sm" onClick={() => props.onAck(a)}>{t("acknowledge")}</button>}
                      {a.level === "P2" && (st === "new" || st === "acked") && (
                        <button type="button" class="btn sm" onClick={() => props.onShelve(a)}>{t("shelve")}</button>
                      )}
                      {st === "shelved" && <button type="button" class="btn sm" onClick={() => props.onUnshelve(a)}>{t("unshelve")}</button>}
                      {st !== "resolved" && <button type="button" class="link" onClick={() => props.onGo(a)}>{t("goTo")}</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Pri({ a, now }: { a: Alert; now: number }) {
  const flash = a.level === "P1" && alarmState(a, now) === "unacked";
  return (
    <span class={`pri ${a.level}${flash ? " flash" : ""}`}><Shape kind={a.level} />{a.level}</span>
  );
}
