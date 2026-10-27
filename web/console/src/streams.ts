// 一页的长连画面路数(演示站点实测:四台机器人 8 路 MJPEG 长连把浏览器对一个站点的 6 条连接(HTTP/1.1)占满,
// 页面自己的数据、报警都进不来,顶上写「与站点的连接中断」)。
// 最多 MAX_STREAMS 路长连,留至少两条给页面自己;超出的画面位隔 2 秒取一张(短请求,取完就还连接)。
// 谁先拿:一直拉的(报警现场、放大的)> 大画面 > 小窗;同一档先来先得。有人让出来,排在前面的取一张的升回长连。
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";

export const MAX_STREAMS = 3;
export const STILL_MS = 2000;

interface Claim { id: number; rank: number }
const claims = signal<Claim[]>([]);
let next = 1;

/** 越大越先拿到长连。 */
export function slotRank(p: { forceLive?: boolean; variant?: "main" | "pip" }): number {
  return (p.forceLive ? 2 : 0) + (p.variant === "pip" ? 0 : 1);
}

/** 这一路想拉流(``want``)时排队;回「拿到长连了没有」。没拿到的取一张。 */
export function useStreamSlot(want: boolean, rank: number): boolean {
  const id = useRef(0);
  if (!id.current) id.current = next++;
  useEffect(() => {
    if (!want) return;
    const me = id.current;
    claims.value = [...claims.value.filter((c) => c.id !== me), { id: me, rank }];
    return () => { claims.value = claims.value.filter((c) => c.id !== me); };
  }, [want, rank]);
  if (!want) return false;
  const order = [...claims.value].sort((a, b) => b.rank - a.rank || a.id - b.id);
  const at = order.findIndex((c) => c.id === id.current);
  return at >= 0 && at < MAX_STREAMS;
}
