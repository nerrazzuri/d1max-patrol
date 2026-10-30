// @vitest-environment happy-dom
// W51:所有画面走一条 WebSocket(原来每路 MJPEG 占一条 HTTP/1.1 连接,浏览器对一个站点最多 6 条)。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setSocketFactory, subscribe, subscribed, type SocketLike } from "./videomux";

class 假连接 implements SocketLike {
  binaryType = "blob";
  readyState = 0;
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  sent: unknown[] = [];
  closed = false;
  constructor(readonly url: string) {}
  send(d: string) { this.sent.push(JSON.parse(d)); }
  close() { this.closed = true; this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.({}); }
  frame(id: string, jpeg: number[]) {
    const raw = new TextEncoder().encode(id);
    const b = new Uint8Array(1 + raw.length + jpeg.length);
    b[0] = raw.length;
    b.set(raw, 1);
    b.set(jpeg, 1 + raw.length);
    this.onmessage?.({ data: b.buffer });
  }
  drop() { this.readyState = 3; this.onclose?.({}); }
}

let made: 假连接[] = [];
const A = "/api/robots/A/video/front";
const B = "/api/cameras/cam3/live";
const tick = () => new Promise((r) => queueMicrotask(() => r(null)));
const offs: (() => void)[] = [];

function 看(id: string, fps = 10) {
  const frames: string[] = [];
  const errors: [number, string][] = [];
  offs.push(subscribe(id, { fps, onFrame: (u) => frames.push(u), onError: (s, m) => errors.push([s, m]) }));
  return { frames, errors, off: offs[offs.length - 1] };
}

beforeEach(() => {
  made = [];
  setSocketFactory((u) => { const s = new 假连接(u); made.push(s); return s; });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});
afterEach(() => {
  while (offs.length) offs.pop()!();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  setSocketFactory(null);
});

describe("一条 WebSocket 装所有画面", () => {
  it("两路画面只开一条连接;连上就把要看的整份发过去,同一路几个人看只订一次、fps 取大的", async () => {
    看(A, 5);
    看(B);
    看(A, 15);
    expect(made.length).toBe(1);
    expect(made[0].url).toMatch(/\/api\/video\/ws$/);
    made[0].open();
    await tick();
    expect(made[0].sent.at(-1)).toEqual({ sub: [{ id: A, fps: 15 }, { id: B, fps: 10 }] });
  });

  it("来帧:交给看这一路的,是 blob: 地址", async () => {
    const a = 看(A);
    const b = 看(B);
    made[0].open();
    made[0].frame(A, [0xff, 0xd8, 1]);
    expect(a.frames.length).toBe(1);
    expect(a.frames[0]).toMatch(/^blob:/);
    expect(b.frames.length).toBe(0);
  });

  it("站点说这一路起不来:只告诉看这一路的,带原因", async () => {
    const a = 看(A);
    const b = 看(B);
    made[0].open();
    made[0].onmessage?.({ data: JSON.stringify({ kind: "error", id: A, status: 504, message: "等不到第一帧" }) });
    expect(a.errors).toEqual([[504, "等不到第一帧"]]);
    expect(b.errors).toEqual([]);
  });

  it("连接断了:每一路都按断了处理,1 秒后自己重连,连上再发一遍要看的", async () => {
    const a = 看(A);
    made[0].open();
    made[0].drop();
    expect(a.errors.length).toBe(1);
    expect(made.length).toBe(1);
    vi.advanceTimersByTime(1000);
    expect(made.length).toBe(2);
    made[1].open();
    await tick();
    expect(made[1].sent.at(-1)).toEqual({ sub: [{ id: A, fps: 10 }] });
  });

  it("没人看了:退订、连接收掉", async () => {
    const a = 看(A);
    made[0].open();
    a.off();
    expect(subscribed()).toEqual([]);
    expect(made[0].closed).toBe(true);
  });

  it("退订一路:剩下的整份重发", async () => {
    const a = 看(A);
    看(B);
    made[0].open();
    await tick();
    a.off();
    await tick();
    expect(made[0].sent.at(-1)).toEqual({ sub: [{ id: B, fps: 10 }] });
  });
});
