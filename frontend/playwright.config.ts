import { defineConfig } from "@playwright/test";

// Smoke test of the built dashboard against the real API with the stub LLM (tests/e2e_server.py).
// Needs Postgres (`make up` locally, the service container in CI) and `npm run build` first.
const port = 8765;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  use: { baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: `uv run python -m tests.e2e_server ${port}`,
    cwd: "..",
    url: `http://127.0.0.1:${port}/health`,
    timeout: 60_000,
    reuseExistingServer: !process.env.CI,
  },
});
