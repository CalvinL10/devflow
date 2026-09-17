import { defineConfig } from "@playwright/test";

const externalServers = process.env.DEVFLOW_E2E_EXTERNAL_SERVERS === "1";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.mjs",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3000",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: externalServers
    ? undefined
    : [
        {
          command: "node e2e/start-backend.mjs",
          url: "http://127.0.0.1:8000/api/health",
          reuseExistingServer: false,
          timeout: 60_000,
        },
        {
          command: "node --dns-result-order=ipv4first node_modules/next/dist/bin/next dev --hostname localhost --port 3000",
          url: "http://127.0.0.1:3000",
          reuseExistingServer: false,
          timeout: 60_000,
          env: { DEVFLOW_API_URL: "http://127.0.0.1:8000" },
        },
      ],
});
