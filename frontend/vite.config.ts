import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = process.env.BACKEND_PROXY_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
});
