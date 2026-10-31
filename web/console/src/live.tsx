// 各页共用的机器人状态文字(StateText)、全屏看一路(FullCamera)。W44 起旧的视频墙换成值班页、实况页。
import { useState } from "preact/hooks";
import { CameraWell, camLabel, type Cam } from "./camera";
import { Modal } from "./dialogs";
import { t, type Key } from "./i18n";
import { Shape, type ShapeKind } from "./icons";
import { tone } from "./theme";
import { abnormal, type DeterSession, type Robot, type RobotState } from "./model";

export interface UnitInfo {
  robot: Robot;
  state: RobotState;
  battery: number | null;
  deter?: DeterSession;
  task: string;
  /** 现在的速度(米/秒;遥测有才有)。 */
  speed?: number | null;
  /** 数据过期了(B 阶段外审 I5):状态显示「未知」。 */
  stale?: boolean;
  /** 驱离接口这一次没拿到:说不清有没有驱离,不是「没有」。 */
  deterUnknown?: boolean;
}

const CAMS: Cam[] = ["front", "back"];

/** 狗状态旁边的形状(色盲靠它):离线有自己的形状,P1 类菱形,其它 P2 类三角;正常没有。 */
export function stateShape(s: RobotState): ShapeKind | null {
  if (s === "offline") return "offline";
  const a = abnormal(s);
  return a === "p1" ? "P1" : a === "p2" ? "P2" : null;
}

/** 定位怎么样(W60 卡片第二层):没报 = 「—」;变差、不可信 = 变差;没锚定 = 未定位。 */
export function locText(u: UnitInfo | undefined): Key | null {
  const l = u?.robot.loc;
  if (!u || u.stale || !l) return null;
  if (l.degraded || l.reason) return "locPoor";
  if (l.anchored === false || !l.pose) return "locNone";
  return "locGood";
}

export function StateText({ s, stale }: { s: RobotState; stale?: boolean }) {
  if (stale) return <span class="state unknown">{t("stUnknown")}</span>;
  const a = abnormal(s);
  const shape = stateShape(s);
  return (
    <span class={`${a ? `state ${a}` : "state"} tone-${tone(s)}`}>
      {shape && <Shape kind={shape} />}
      {t(`st_${s}` as Key)}
    </span>
  );
}

/** 全屏看一路;上面的按钮切这只狗的各路(``cams``:装了热像的多一路,W40)。 */
export function FullCamera({ robot, cam, now, onClose, cams = CAMS }: { robot: string; cam: Cam; now: number; onClose: () => void; cams?: Cam[] }) {
  const [c, setC] = useState<Cam>(cam);
  return (
    <Modal label={camLabel(robot, c)} wide onClose={onClose}>
      <div class="seg" role="group" aria-label={robot}>
        {cams.map((x) => (
          <button key={x} type="button" aria-pressed={x === c} onClick={() => setC(x)}>{t(x)}</button>
        ))}
      </div>
      <div class="full">
        <CameraWell key={c} robot={robot} cam={c} now={now} forceLive />
      </div>
    </Modal>
  );
}
