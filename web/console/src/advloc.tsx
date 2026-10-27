// 高级 → 定位(W44 第四期,设计 §5.3「定位」):什么时候用 —— 机器人说找不到自己在哪。
// 每台一行:绿「知道自己在哪」/ 黄「不太确定」/ 红「迷路了」/ 灰「不知道」(离线、没报)。「帮它找位置」:在地图上把它拖到
// 实际站的地方、拖箭头指朝向(跟值班页同一套:SiteMap 的选点、Relocalize.human)。数字、历史收在「详细」里。
import { useState } from "preact/hooks";
import { nameOf } from "./data";
import { t, type Key } from "./i18n";
import type { UnitInfo } from "./live";
import { LocHistoryPage } from "./locpage";
import { SiteMap } from "./mappage";
import type { Alert, Robot } from "./model";

export type LocLevel = "ok" | "unsure" | "lost" | "unknown";
const LOST_KINDS = new Set(["loc_lost_paused", "loc_lost_idle"]);
/** 偏差超过这么多米算「不太确定」。 */
export const UNSURE_SIGMA_M = 0.3;

/** 机器人现在知不知道自己在哪:有没解决的「迷路」告警、没锚上 → 红;「变差」告警、偏差大 → 黄;报了定位 → 绿;离线、没报 → 灰。 */
export function locLevel(r: Robot, alerts: Alert[]): LocLevel {
  const open = alerts.filter((a) => a.robot === r.robot_id && a.resolved_ms == null);
  if (open.some((a) => LOST_KINDS.has(a.kind))) return "lost";
  if (!r.fresh || !r.loc) return "unknown";
  if (r.loc.anchored === false || r.loc.source === "lost") return "lost";
  if (open.some((a) => a.kind === "loc_degraded") || (r.loc.sigma_m ?? 0) > UNSURE_SIGMA_M || !!r.loc.degraded) return "unsure";
  return "ok";
}

const TEXT: Record<LocLevel, Key> = { ok: "locOk", unsure: "locUnsure", lost: "locLost", unknown: "locUnknown" };

export function LocPage(props: { units: UnitInfo[]; alerts: Alert[]; now: number; role: string }) {
  const [finding, setFinding] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(props.units[0]?.robot.robot_id ?? null);
  return (
    <section class="advloc">
      <h1>{t("tabLocalization")}</h1>
      <p class="muted lead">{t("locWhen")}</p>
      <div class="ly-tab">
        <div>
          <SiteMap units={props.units} p1={[]} now={props.now} role={props.role} episodes={false}
            selected={finding ?? selected} onSelect={setSelected} pick={!!finding}
            hint={finding ? t("dutyFindHint", { id: nameOf(finding) }) : null} />
        </div>
        <div class="ly-side">
          <ul class="plain ly-list">
            {props.units.length === 0 && <li class="muted small">{t("robotsNone")}</li>}
            {props.units.map((u) => {
              const lv = locLevel(u.robot, props.alerts);
              const id = u.robot.robot_id;
              return (
                <li key={id} class="loc-row" data-robot={id} data-level={lv}>
                  <i class={`loc-dot ${lv}`} aria-hidden="true" />
                  <b>{nameOf(id)}</b>
                  <span class="muted small">{t(TEXT[lv])}</span>
                  {lv !== "unknown" && (
                    <button type="button" class={finding === id ? "btn sm primary push" : "btn sm push"}
                      onClick={() => { setFinding(finding === id ? null : id); setSelected(id); }}>
                      {finding === id ? t("cancel") : t("dutyFindPose")}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {finding && <p class="muted small">{t("locFindHow")}</p>}
        </div>
      </div>
      <details class="loc-detail">
        <summary>{t("locDetails")}</summary>
        <LocHistoryPage robots={props.units.map((u) => u.robot.robot_id)} now={props.now} />
      </details>
    </section>
  );
}
