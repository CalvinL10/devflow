import { expect, test } from "@playwright/test";

export async function mockSetup(page, provider = "mock") {
  await page.route("**/api/health", (route) => route.fulfill({ json: { provider } }));
  await page.route("**/api/settings/provider", (route) => route.fulfill({ json: {
    base_url: "https://provider.example/v1", model: "test-model", key_configured: true, allow_local_http: false,
  } }));
  await page.route("**/api/project/preview", (route) => route.fulfill({ status: 409, json: { detail: "No project mounted" } }));
  await page.route("**/api/runs/active", (route) => route.fulfill({ json: null }));
  await page.route("**/api/runs?*", (route) => route.fulfill({ json: { runs: [], next_offset: null } }));
}

test("task creation shows immediate progress and retries the same request after a lost response", async ({ page }) => {
  await mockSetup(page);
  let pendingRequest;
  const payloads = [];
  await page.route("**/api/runs", (route) => {
    payloads.push(route.request().postDataJSON());
    expect(route.request().headers()["x-devflow-request"]).toBe("1");
    pendingRequest = route;
  });
  await page.goto("/");
  await expect(page.getByLabel("Provider mode")).toContainText("Demo mode — mock provider");
  await page.getByTestId("task-input").fill("Prepare a focused change");
  await page.getByTestId("run-button").click();
  await expect.poll(() => payloads.length).toBe(1);
  await expect(page.getByTestId("run-button")).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "Starting run." })).toBeVisible();
  await pendingRequest.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Backend temporarily unavailable" } } });
  await expect(page.getByRole("alert").filter({ hasText: "Backend temporarily unavailable" })).toBeVisible();
  await expect(page.getByTestId("run-button")).toBeEnabled();
  await page.getByTestId("run-button").click();
  await expect.poll(() => payloads.length).toBe(2);
  expect(payloads[1]).toEqual(payloads[0]);
  expect(payloads[0]).toEqual({ task: "Prepare a focused change", import_id: null, request_id: expect.any(String) });
  const run = { run_id: "run-async", task: payloads[0].task, status: "RUNNING", latest_seq: 0,
    workspace_revision: 0, base_workspace_revision: 0, patch_revision: 0, provider: "mock" };
  await page.route("**/api/runs/run-async", (route) => route.fulfill({ json: run }));
  await page.route("**/api/runs/run-async/events", (route) => route.fulfill({ contentType: "text/event-stream", body: ": heartbeat\n\n" }));
  await pendingRequest.fulfill({ status: 202, json: run });
  await expect(page).toHaveURL(/\/runs\/run-async$/);
  await expect(page.getByTestId("run-status")).toHaveText("RUNNING");
  await expect(page.getByTestId("stop-button")).toBeEnabled();
});

test("unknown or real provider never falls back to demo when preview is unavailable", async ({ page }) => {
  await mockSetup(page, "chat_completions");
  await page.goto("/?task=Investigate%20failure");
  await expect(page.getByTestId("task-input")).toHaveValue("Investigate failure");
  await expect(page.getByTestId("run-button")).toBeDisabled();
  await expect(page.getByLabel("Provider mode")).not.toContainText("Demo mode");
  await expect(page.getByRole("alert").filter({ hasText: "Project preview unavailable" })).toBeVisible();
  await page.route("**/api/health", (route) => route.fulfill({ status: 503, json: {} }));
  await page.getByRole("button", { name: "Check provider mode" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Cannot verify provider mode" })).toBeVisible();
  await expect(page.getByTestId("run-button")).toBeDisabled();
});
