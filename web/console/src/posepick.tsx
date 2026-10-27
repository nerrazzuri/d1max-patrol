// 地图页上「点一下 → 去这里 / 在这里设位置」(2026-10-14,能力台账行 24、13)。
// 点图选一个位置,拖圆形把手定朝向;「去这里」走已有的 goto 接口(站点拒了就把原因原样给人看,比如
// 监护模式要手机的租约),「在这里设位置」走 relocalize(先弹确认,写着 x、y、朝向)。
// 发出去的就是点的那个地图坐标(米)和拖出来的朝向(弧度),不做别的换算。
//
// 外审 W39-I2、I3(2026-10-15):
// - 选的点、提示、确认框都记着「在哪个上下文里选的」(目标狗 + 底图的图号版本 + 狗加载的图);上下文一变,
//   **渲染时**就不认旧的(不等 effect,换完那一帧也点不到旧点)。目标狗加载的图跟底图不是同一张:按钮全灰。
// - 设位置带上底图的图号版本,站点核对(不一样 409,不再按狗的图补上就发)。
// - 站点 HTTP 200 不等于狗收下了:只有回执 ``accepted``(且命令号对得上)才说「已设 / 已让去」;
//   被拒、过期、重复、看不懂的都照原因给人看。上下文变了以后才回来的回包不管。
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
  /** 目标狗**加载的**图(能力里报的);跟 ``map`` 不是同一张就不许发。狗没报(null)让站点、狗去核。 */
  robotMap?: { map_id: string; version: string } | null;
  /** 狗此刻的位置(有就拿它当新点的默认朝向:从狗看向那个点)。 */
  robotAt?: { x: number; y: number } | null;
  role: string;
  /** W41:目标狗能「自动找位置」(能力 ``relocalize.global``)。 */
  canFind?: boolean;
}): PosePick {
  const ctx = pickContext(o.robotId, o.map, o.robotMap);
  const [picked, setPicked] = useState<{ pose: Pose; ctx: string } | null>(null);
  const [said, setSaid] = useState<{ text: string; ctx: string } | null>(null);
  const [askedIn, setAskedIn] = useState<string | null>(null);
  const [findIn, setFindIn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const guard = useRef(clickGuard());
  // 上下文每变一次代次加一:变之前发出去的请求,回来时代次不对就不管
  const gen = useRef({ ctx, n: 0 });
  if (gen.current.ctx !== ctx) gen.current = { ctx, n: gen.current.n + 1 };
  const can = o.role === "admin" || o.role === "engineer" || o.role === "guard";
  const meta = o.meta;
  // 只认这个上下文里选的点、说的话、弹的框
  const pose = picked && picked.ctx === ctx ? picked.pose : null;
  const msg = said && said.ctx === ctx ? said.text : "";
  const asking = askedIn === ctx;
  const wrongMap = !!(o.map && o.robotMap && !sameMap(o.map, o.robotMap));
  const setPose = (f: Pose | null | ((p: Pose | null) => Pose | null)) =>
    setPicked((cur) => {
      const prev = cur && cur.ctx === ctx ? cur.pose : null;
      const next = typeof f === "function" ? f(prev) : f;
      return next ? { pose: next, ctx } : null;
    });
  const setMsg = (text: string) => setSaid(text ? { text, ctx } : null);
  const setAsking = (on: boolean) => setAskedIn(on ? ctx : null);

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
    const mine = gen.current.n;
    const stillMine = () => gen.current.n === mine;     // 回包回来时上下文变了:不管(不说成新狗的)
    setBusy(true);
    try {
      const r = await api<CommandReply>("POST", `/api/robots/${enc(o.robotId ?? "")}/${path}`, body);
      if (!stillMine()) return;
      const why = notAccepted(r);
      setMsg(why === null ? ok : `${rt("poseNeedsSupervision")}: ${why}`);
    } catch (e) {
      if (!stillMine()) return;
      // 站点拒了:原因原样给人看(supervised 要租约、没就绪、点不可达、不是同一张图……)
      setMsg(`${rt("poseNeedsSupervision")}: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }
  const go = () => {
    if (!pose || !o.map || !o.robotId || wrongMap) return;
    void run("goto", { target: { map_id: o.map.map_id, map_version: o.map.version, frame_id: "map", x: pose.x, y: pose.y, yaw: pose.yaw } },
      rt("poseGoDone", { robot: o.robotId }));
  };
  const set = () => {
    setAsking(false);
    if (!pose || !o.map || !o.robotId || wrongMap) return;
    void run("relocalize", { x: pose.x, y: pose.y, yaw: pose.yaw, map_id: o.map.map_id, map_version: o.map.version },
      rt("poseSetDone", { robot: o.robotId }));
  };
  const find = () => {
    setFindIn(null);
    if (!o.map || !o.robotId || wrongMap) return;
    void run("relocalize", { global: true, map_id: o.map.map_id, map_version: o.map.version },
      rt("poseFindDone", { robot: o.robotId }));
  };
  const finding = findIn === ctx;
  const findable = !!(o.canFind && o.robotId && o.map && !wrongMap);
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
        {findable && !pose && (
          <div class="row">
            <button type="button" class="btn sm" data-pose-find="1" disabled={busy} onClick={() => setFindIn(ctx)}>{rt("poseFind")}</button>
          </div>
        )}
        {pose && !o.robotId && <p class="muted small">{rt("poseNoRobot")}</p>}
        {pose && o.robotId && wrongMap && <p class="small warn" data-pose-wrongmap="1">{rt("poseWrongMap", { robot: o.robotId })}</p>}
        {(pose || msg) && <div class="row">
          <button type="button" class="btn primary sm" disabled={!pose || !o.robotId || !o.map || wrongMap || busy} onClick={go}>{rt("poseGo")}</button>
          <button type="button" class="btn sm" disabled={!pose || !o.robotId || !o.map || wrongMap || busy} onClick={() => setAsking(true)}>{rt("poseSet")}</button>
          <button type="button" class="btn sm" disabled={!pose} onClick={() => { setPose(null); setMsg(""); }}>{rt("poseClear")}</button>
        </div>}
        {msg && <p class="small" role="status" data-pose-msg="1">{msg}</p>}
        {finding && findable && (
          <Confirm text={rt("poseFindConfirm", { robot: o.robotId ?? "" })} yes={rt("poseFind")}
            onNo={() => setFindIn(null)} onYes={find} />
        )}
        {asking && pose && !wrongMap && (
          <Confirm text={rt("poseSetConfirm", { robot: o.robotId ?? "", x: pose.x, y: pose.y, deg: degOf(pose.yaw) })}
            yes={rt("poseSet")} onNo={() => setAsking(false)} onYes={set} />
        )}
      </div>
    ),
  };
}

/** 站点派单接口的回包:HTTP 200 里带着狗的回执(``accepted`` / ``rejected`` / ``expired`` / ``duplicate``)。 */
export interface CommandReply {
  command_id?: string;
  ack?: { command_id?: string; result?: string; reason?: string };
}

/** 狗收下了吗:收下了是 ``null``;没收下(被拒、过期、重复、回执跟命令对不上、看不懂)回给人看的原因。 */
export function notAccepted(r: CommandReply | null | undefined): string | null {
  const ack = r?.ack;
  if (!ack || typeof ack.result !== "string") return rt("poseNoAck");
  if (r?.command_id && ack.command_id && ack.command_id !== r.command_id) return rt("poseNoAck");
  if (ack.result === "accepted") return null;
  return ack.reason ? `${ack.result}: ${ack.reason}` : ack.result;
}

export function sameMap(a: { map_id: string; version: string }, b: { map_id: string; version: string }): boolean {
  return a.map_id === b.map_id && a.version === b.version;
}

/** 选点的上下文:目标狗、底图、狗加载的图,哪个变了旧的点都作废。 */
export function pickContext(robotId: string | null, map: { map_id: string; version: string } | null,
  robotMap: { map_id: string; version: string } | null | undefined): string {
  const m = (x: { map_id: string; version: string } | null | undefined) => (x ? `${x.map_id}/${x.version}` : "-");
  return `${robotId ?? "-"}|${m(map)}|${m(robotMap)}`;
}
