// 今天的巡逻(W53):日程的每一轮和临时巡逻按时间排,一行一趟。值班页左下角。站点 GET /api/patrols/today。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { hm } from "./data";
import { t, type Key } from "./i18n";

export interface TodayItem {
  at_ms: number;
  kind: "schedule" | "adhoc";
  label: string;
  robot: string;
  by: string;
  state: string;
  reason: string;
  end_ms: number | null;
  adhoc_id: string;
}

const STATE: Record<string, [Key, string]> = {
  done: ["tdDone", "ok"], failed: ["tdFailed", "bad"], aborted: ["tdAborted", ""], running: ["tdRunning", "info"],
  waiting: ["tdWaiting", "warn"], missed: ["tdMissed", "bad"], next: ["tdNext", ""], later: ["tdLater", ""],
  queued: ["tdQueued", "warn"], cancelled: ["tdCancelled", ""], expired: ["tdExpired", ""],
};

export function stateOf(s: string): [string, string] {
  const x = STATE[s];
  return x ? [t(x[0]), x[1]] : [s, ""];
}

export function TodayPatrols() {
  const [items, setItems] = useState<TodayItem[] | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => api<{ items: TodayItem[] }>("GET", "/api/patrols/today")
      .then((d) => live && setItems(d.items ?? []))
      .catch(() => live && setItems((x) => x ?? []));
    void load();
    const timer = window.setInterval(load, 30_000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  return (
    <section class="today" aria-label={t("tdTitle")}>
      <h3 class="today-h">{t("tdTitle")}</h3>
      {items != null && items.length === 0 && <p class="muted small">{t("tdNone")}</p>}
      <ul class="today-list">
        {(items ?? []).map((x, i) => {
          const [label, tone] = stateOf(x.state);
          return (
            <li key={i} class="today-row" title={x.reason || undefined}>
              <span class="mono">{hm(x.at_ms)}</span>
              <span class="today-what">
                {[x.kind === "adhoc" ? t("tdAdhoc") : "", x.label, x.robot, x.kind === "adhoc" ? x.by : ""].filter(Boolean).join(" · ")}
                {x.reason && <span class="muted small"> · {x.reason}</span>}
              </span>
              <span class={`tdchip ${tone}`}>{label}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
