// 实况页(W44 第一期,设计 §4.2):所有机器人的画面一直在播。2×2 卡片,每张一台:前、后两路左右并排,
// 顶上同样的状态栏;按值班页的紧急程度排,超过 4 台往下翻。装了热像的卡片能把后摄像头那一半换成热像,
// 右上角「全部看热像」夜里一键全换(设计 §2.4)。
import { useState } from "preact/hooks";
import { CameraWell, type Cam } from "./camera";
import { rankUnits } from "./dutyrank";
import { t } from "./i18n";
import type { UnitInfo } from "./live";
import { robotCams, type Alert } from "./model";
import { SlotBar, SlotFoot } from "./slot";
import { nameOf } from "./data";

export function LiveWall(props: { units: UnitInfo[]; alerts: Alert[]; now: number; onZoom: (robot: string) => void }) {
  const ranked = rankUnits(props.units, props.alerts);
  const [all, setAll] = useState(false);
  const [one, setOne] = useState<Set<string>>(new Set());
  const anyThermal = ranked.some((u) => robotCams(u.robot).includes("thermal"));
  const flip = (id: string) => setOne((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    return n;
  });
  return (
    <div class="livewall">
      {anyThermal && (
        <div class="lw-tools">
          <button type="button" class="btn sm lw-allthermal" aria-pressed={all} onClick={() => { setAll(!all); setOne(new Set()); }}>
            {t("lwAllThermal")}
          </button>
        </div>
      )}
      {ranked.length === 0 && <p class="empty">{t("dutyNoRobots")}</p>}
      <div class="lw-grid">
        {ranked.map((u) => {
          const id = u.robot.robot_id;
          const has = robotCams(u.robot).includes("thermal");
          const thermal = has && (all !== one.has(id));
          const second: Cam = thermal ? "thermal" : "back";
          const off = u.state === "offline";
          return (
            <section key={id} class="lw-card slot" data-robot={id} aria-label={nameOf(id)}>
              {off ? <div class="slot-off">{t("st_offline")}</div> : (
                <div class="lw-pair">
                  <CameraWell key={`${id}-front`} robot={id} cam="front" now={props.now} variant="main" />
                  <CameraWell key={`${id}-${second}`} robot={id} cam={second} now={props.now} variant="main" />
                </div>
              )}
              <SlotBar u={u} onZoom={() => props.onZoom(id)} thermal={has && !off ? { on: thermal, toggle: () => flip(id) } : null} />
              <SlotFoot u={u} />
            </section>
          );
        })}
      </div>
    </div>
  );
}
