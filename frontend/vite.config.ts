/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the API to the Compose app (APP_HOST_PORT, default 8001).
const api = process.env.OPSPILOT_API ?? "http://127.0.0.1:8001";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/investigate": api, "/investigations": api, "/health": api } },
  test: { environment: "jsdom", setupFiles: ["./src/setup-tests.ts"], include: ["src/**/*.test.ts?(x)"] },
});
