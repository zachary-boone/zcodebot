import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Electron 渲染进程用 Vite 构建。
// 主进程（electron/main.ts）用 tsc 单独编译到 dist-electron/。
export default defineConfig({
  plugins: [react()],
  base: "./", // Electron file:// 加载需要相对路径
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
