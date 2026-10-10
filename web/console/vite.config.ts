// 网页值班台(商业化 B1)。构建产物直接放进站点包里,站点安装时不用装 Node、离线也能用。
// 开发时 `npm run dev`,/api 转给本机站点(D1MAX_SITE=https://127.0.0.1:8443 可改)。
import preact from "@preact/preset-vite";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const site = process.env.D1MAX_SITE ?? "https://127.0.0.1:8443";

export default defineConfig({
  plugins: [preact()],
  base: "/",
  build: {
    outDir: fileURLToPath(new URL("../../packages/site-node/src/d1max_site/web", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "assets",
    // 站点的 CSP 不许内联脚本、内联样式:字体、图片都走文件
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  server: {
    proxy: { "/api": { target: site, secure: false, changeOrigin: true } },
    fs: { allow: [fileURLToPath(new URL("../..", import.meta.url))] },
  },
  test: { include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
});
