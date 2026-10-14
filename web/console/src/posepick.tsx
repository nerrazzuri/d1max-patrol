// 地图页上「点一下 → 去这里 / 在这里设位置」(2026-10-14,能力台账行 24、13)。
// 点图选一个位置,拖圆形把手定朝向;「去这里」走已有的 goto 接口(站点拒了就把原因原样给人看,比如
// 监护模式要手机的租约),「在这里设位置」走 relocalize(先弹确认,写着 x、y、朝向)。
// 发出去的就是点的那个地图坐标(米)和拖出来的朝向(弧度),不做别的换算。
import type { ComponentChildren } from "preact";
import { useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm } from "./dialogs";
import { clickGuard, startDrag, swallowClick } from "./dragpt";
import type { Screen } from "./mapcanvas";
import type { PreviewMeta } from "./mapview";
import { PoseMarker } from "./posemarker";
import { clientToMap, degOf, handleAt, pxToMap, yawToward } from "./routes";
import { rt } from "./routes_i18n";
import "./routes.css";

export interface Pose { x: number; y: number; yaw: number }

const HANDLE_PX = 34;

export interface PosePick {
  /** 能点图选位置吗(管理员、保安);不能就是 undefined,地图不进「点图」模式。 */
  onPick?: (px: number, py: number) => void;
  marks: (s: Screen) => ComponentChildren;
  panel: ComponentChildren;
  /** 挂在地图外面那层 div 上(拖把手要从它里面找世界层;吞掉拖完的那个 click)。 */
  wrapProps: { ref: { current: HTMLDivElement | null }; onClickCapture: (e: MouseEvent) => void };
}

export function usePosePick(o: {
  meta: PreviewMeta | null;
  map: { map_id: string; version: string } | null;
  robotId: string | null;
  /** 狗此刻的位置(有就拿它当新点的默认朝向:从狗看向那个点)。 */
  robotAt?: { x: number; y: number } | null;
  role: string;
}): PosePick {
  const [pose, setPose] = useState<Pose | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const guard = useRef(clickGuard());
  const can = o.role === "admin" || o.role === "guard";
  const meta = o.meta;

  function onPick(px: number, py: number) {
    if (!meta) return;
    const [x, y] = pxToMap(meta, px, py);
    const from = o.robotAt;
    const yaw = (from && yawToward(from.x, from.y, x, y)) ?? pose?.yaw ?? 0;
    setPose({ x, y, yaw });
    setMsg("");
  }
  function dragHandle(e: PointerEvent) {
    startDrag(e, guard.current, (cx, cy) => {
      const m = meta && clientToMap(ref.current, meta, cx, cy);
      if (!m) return;
      setPose((p) => {
        const yaw = p && yawToward(p.x, p.y, m[0], m[1]);
        return p && yaw !== null && yaw !== undefined ? { ...p, yaw } : p;
      });
    });
  }

  async function run(path: string, body: unknown, ok: string) {
    setBusy(true);
    try {
      await api("POST", `/api/robots/${enc(o.robotId ?? "")}/${path}`, body);
      setMsg(ok);
    } catch (e) {
      // 站点拒了:原因原样给人看(supervised 要租约、没就绪、点不可达……)
      setMsg(`${rt("poseNeedsSupervision")}: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }
  const go = () => {
    if (!pose || !o.map || !o.robotId) return;
    void run("goto", { target: { map_id: o.map.map_id, map_version: o.map.version, frame_id: "map", x: pose.x, y: pose.y, yaw: pose.yaw } },
      rt("poseGoDone", { robot: o.robotId }));
  };
  const set = () => {
    if (!pose || !o.robotId) return;
    setAsking(false);
    void run("relocalize", { x: pose.x, y: pose.y, yaw: pose.yaw }, rt("poseSetDone", { robot: o.robotId }));
  };
  const at = pose ? rt("poseAt", { x: pose.x, y: pose.y, deg: degOf(pose.yaw) }) : "";

  return {
    onPick: can ? onPick : undefined,
    wrapProps: { ref, onClickCapture: (e) => swallowClick(guard.current, e) },
    marks: (s) => {
      if (!pose || !meta) return null;
      const h = handleAt(meta, s, pose.x, pose.y, pose.yaw, HANDLE_PX);
      // 定位箭头(posemarker.tsx):圆底座 + 朝向三角尖,三角尖就是转朝向的把手
      return (
        <PoseMarker x={h.x} y={h.y} hx={h.hx} hy={h.hy} r={12} selected class="pose-pin"
          attrs={{ "data-pose": "1" } as Record<string, string>}
          handle={{ data: "pose", onPointerDown: dragHandle }} />
      );
    },
    panel: !can ? null : (
      <div class={!pose && !msg ? "posebar idle" : "posebar"} data-posebar="1">
        {!pose ? <p class="muted small">{rt("poseHint")}</p> : <p class="mono small" data-pose-text="1">{at}</p>}
        {pose && !o.robotId && <p class="muted small">{rt("poseNoRobot")}</p>}
        {(pose || msg) && <div class="row">
          <button type="button" class="btn primary sm" disabled={!pose || !o.robotId || !o.map || busy} onClick={go}>{rt("poseGo")}</button>
          <button type="button" class="btn sm" disabled={!pose || !o.robotId || busy} onClick={() => setAsking(true)}>{rt("poseSet")}</button>
          <button type="button" class="btn sm" disabled={!pose} onClick={() => { setPose(null); setMsg(""); }}>{rt("poseClear")}</button>
        </div>}
        {msg && <p class="small" role="status" data-pose-msg="1">{msg}</p>}
        {asking && pose && (
          <Confirm text={rt("poseSetConfirm", { robot: o.robotId ?? "", x: pose.x, y: pose.y, deg: degOf(pose.yaw) })}
            yes={rt("poseSet")} onNo={() => setAsking(false)} onYes={set} />
        )}
      </div>
    ),
  };
}
