import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Simple dev server config. The backend URL/WS URL are read from
// VITE_BACKEND_HTTP_URL / VITE_BACKEND_WS_URL at runtime (see src/config.js),
// so no proxy is required, but one is left here, commented, in case you'd
// rather avoid CORS entirely during development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // proxy: {
    //   "/languages": "http://localhost:8000",
    //   "/ws": { target: "ws://localhost:8000", ws: true },
    // },
  },
});
