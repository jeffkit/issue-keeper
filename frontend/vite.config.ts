import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发：前端跑在 5173，/api 代理到后端 7433。
// 生产：npm run build 产出 dist/，由 FastAPI 一并托管，同源，无需代理。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7433",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
