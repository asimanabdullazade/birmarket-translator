import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Simple dev server config. The backend URL/WS URL are read from
// VITE_BACKEND_HTTP_URL / VITE_BACKEND_WS_URL at runtime (see src/config.js),
// so no proxy is required, but one is left here, commented, in case you'd
// rather avoid CORS entirely during development.
//
// Phase 11 (meeting broadcast mode): two extra pages need their own build
// entries alongside the existing index.html --
//   - listener.html (companion page meeting attendees open -- see
//     src/MeetingListener.jsx)
//   - broadcast.html (dev-only mic broadcaster used to test the pipeline
//     without a WAV file -- see src/MeetingBroadcast.jsx)
// `npm run dev` serves all three automatically with no extra config (any
// .html file at the project root is a dev-server entry by default);
// `vite build` needs to be told about each one explicitly via
// rollupOptions.input, or it would only ever emit index.html.
// Phase 13 (Teams side panel): set VITE_VIA_TUNNEL=1 when serving this
// dev server through an ngrok tunnel. It only affects HMR -- see below --
// and is off by default so plain `npm run dev` on localhost is unchanged.
const viaTunnel = process.env.VITE_VIA_TUNNEL === "1";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Phase 13: Teams renders the side panel in an iframe served over
    // HTTPS, which in dev means an ngrok tunnel. Those requests arrive
    // with a Host header Vite doesn't recognise, and Vite (5.4.12+)
    // answers "Blocked request. This host is not allowed." rather than
    // serving the app. Note ngrok now issues .ngrok-free.dev hostnames;
    // the older .app/.io suffixes are kept for existing tunnels.
    allowedHosts: [".ngrok-free.dev", ".ngrok-free.app", ".ngrok.app", ".ngrok.io"],
    // Through a tunnel, HMR's own WebSocket must go back out over
    // 443/wss rather than to localhost:5173, or the dev client can never
    // connect and you lose hot reload (the app still loads, so this
    // presents as "HMR just stopped working", not as an error).
    hmr: viaTunnel ? { clientPort: 443, protocol: "wss" } : undefined,
    // Phase 13: the proxy is now ACTIVE rather than commented out.
    //
    // ngrok's free plan hands out a single hostname, so the frontend and
    // the backend cannot each get their own tunnel. Proxying the
    // backend's routes through the Vite dev server means one tunnel
    // serves both: the browser only ever talks to the frontend origin,
    // and Vite forwards /languages and /ws to localhost:8000 on the
    // server side. Same-origin also means CORS stops applying at all,
    // and wss:// works without a second certificate/hostname.
    //
    // ws: true is required -- without it the proxy would handle the HTTP
    // request but not the Upgrade handshake, and every WebSocket would
    // fail to connect. This covers /ws/translate, /ws/meeting/{id}/ingest
    // and /ws/meeting/{id}/listen (see backend/main.py).
    proxy: {
      "/languages": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        listener: fileURLToPath(new URL("./listener.html", import.meta.url)),
        broadcast: fileURLToPath(new URL("./broadcast.html", import.meta.url)),
      },
    },
  },
});
