import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// This is a development proxy setting, never a VITE_* value embedded into browser code.
const target = process.env.TA_API_ORIGIN || "http://127.0.0.1:8000";
const origin = new URL(target);
if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password
  || origin.pathname !== "/" || origin.search || origin.hash) {
  throw new Error("TA_API_ORIGIN must be an HTTP(S) origin without credentials");
}
const proxy = {
  "/v1": { target, changeOrigin: false },
  "/health": { target, changeOrigin: false },
};

export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
  build: {
    // The antd vendor chunk is intentionally isolated and long-lived in the browser cache.
    chunkSizeWarningLimit: 800,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            { name: "antd", test: /node_modules[\\/](antd|@ant-design|@rc-component|rc-[^\\/]+)[\\/]/ },
          ],
        },
      },
    },
  },
});
