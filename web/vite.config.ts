import { defineConfig } from "vite";

// 构建产物直接吐到 Go 包旁边的 web/ 目录，这样 FastAPI 用 Path(__file__).parent / "web"
// 就能找到，装成包也不会丢。产物会一起提交，所以 clone 下来不装 npm 也能看界面。
export default defineConfig({
  base: "./",
  build: {
    outDir: "../src/minicode/service/web",
    emptyOutDir: true,
  },
  server: {
    // 本地开发时把接口请求转给后端起的那份
    proxy: {
      "/runs": "http://127.0.0.1:8765",
      "/healthz": "http://127.0.0.1:8765",
    },
  },
});
