// 视觉数值的唯一来源是 design/tokens.json(B0)。这里把颜色、圆角挂成 CSS 变量:--c-<名字>、--r-<名字>。
// 用 CSSOM 设(不是内联 style 属性),站点的 CSP(style-src 'self')允许。
import tokens from "../../../design/tokens.json";

export const color = tokens.color as Record<string, string>;

export function applyTokens(root: HTMLElement = document.documentElement): void {
  for (const [k, v] of Object.entries(tokens.color)) root.style.setProperty(`--c-${k}`, v);
  for (const [k, v] of Object.entries(tokens.radius)) root.style.setProperty(`--r-${k}`, `${v}px`);
  root.style.setProperty("--osd-plate", tokens.overlay.osdPlate);
  root.style.setProperty("--paused-dim", String(tokens.overlay.pausedDim));
  root.style.setProperty("--font-sans", tokens.font.sans);
  root.style.setProperty("--font-mono", tokens.font.mono);
}
