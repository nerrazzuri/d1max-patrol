// 每只狗下面的小字:定位(来源、偏差、变差、为什么不可信)、被挡了多久、重规划了几次。
// 值班台的视频墙和地图侧栏共用。读的是站点 /api/robots 里已有的 loc 块和新加的 nav 块。
import { Shape } from "./icons";
import type { Robot } from "./model";
import { blockedText, L, locLine, locState, replanText } from "./runviz";

export function RunInfo({ robot }: { robot: Robot }) {
  const loc = robot.loc ?? null;
  const st = locState(loc);
  const nav = robot.nav ?? null;
  if (!loc && !nav) return null;
  const cls = st === "lost" || st === "unset" ? "lost" : st === "degraded" ? "warn" : "";
  return (
    <div class="runinfo" data-testid="run-info">
      {loc && (
        <span class={cls} data-loc={st}>
          {st !== "ok" && st !== "unknown" && <Shape kind="P2" size={12} />}
          {locLine(loc)}
          {/* 工单 11:定位器报了原始 ICP 匹配质量就显示它;老的只有 σ 换算的 quality */}
          {st !== "unset" && (typeof loc.icp_quality === "number"
            ? ` · ${L("match quality", "匹配质量")} ${loc.icp_quality.toFixed(2)}`
            : typeof loc.quality === "number" && ` · ${L("quality", "质量")} ${loc.quality.toFixed(2)}`)}
        </span>
      )}
      {nav?.blocked && <span class="warn" data-nav="blocked"><Shape kind="P2" size={12} />{blockedText(nav.blocked)}</span>}
      {nav?.replan && <span data-nav="replan">{replanText(nav.replan)}</span>}
    </div>
  );
}
