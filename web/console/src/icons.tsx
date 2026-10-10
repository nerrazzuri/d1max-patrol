// 图标(B0 专业版):优先级三重编码的形状(P1 实心菱形、P2 实心三角、P3 空心圆)、急停八边形,
// 外加几个线条图标(Lucide 风格,ISC 许可的样子手画)。形状永远跟文字一起出现,不单独表意。
import type { JSX } from "preact";

export function Shape({ kind, size = 12 }: { kind: "P1" | "P2" | "P3"; size?: number }): JSX.Element {
  return (
    <svg class={`shape ${kind}`} width={size} height={size} viewBox="0 0 12 12" aria-hidden="true">
      {kind === "P1" && <path d="M6 0l6 6-6 6-6-6z" />}
      {kind === "P2" && <path d="M6 1l6 10H0z" />}
      {kind === "P3" && <circle cx="6" cy="6" r="4.5" fill="none" stroke-width="1.5" />}
    </svg>
  );
}

const P: Record<string, string[]> = {
  octagon: ["M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z"],
  x: ["M18 6L6 18", "M6 6l12 12"],
  expand: ["M15 3h6v6", "M9 21H3v-6", "M21 3l-7 7", "M3 21l7-7"],
  map: ["M9 4l-6 2v14l6-2 6 2 6-2V4l-6 2z", "M9 4v14", "M15 6v14"],
  logout: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"],
};

export function Icon({ name, size = 18 }: { name: keyof typeof P; size?: number }): JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      {P[name].map((d) => <path key={d} d={d} />)}
    </svg>
  );
}
