import { expect, test } from "@playwright/test";

test("task launcher describes a pending synchronous request and clears waiting on failure", async ({ page }) => {
  await page.route("**/api/runs/active", (route) => route.fulfill({ json: null }));
  let pendingRequest;
  let notifyReceived;
  const received = new Promise((resolve) => { notifyReceived = resolve; });
  await page.route("**/api/runs", (route) => {
    pendingRequest = route;
    notifyReceived();
  });
  await page.goto("/");
  await page.getByTestId("task-input").fill("Prepare a focused change");
  await page.getByTestId("run-button").click();
  await received;
  try {
    await expect(page.getByTestId("run-button")).toBeDisabled();
    await expect(page.getByRole("status")).toContainText("Waiting for the backend");
    await expect(page.getByRole("status")).toContainText("Live step progress is not available during creation.");
  } finally {
    await pendingRequest.fulfill({ status: 503, json: {
      error: { code: "unavailable", message: "Backend temporarily unavailable" },
    } });
  }
  await expect(page.getByRole("alert").filter({ hasText: "Backend temporarily unavailable" })).toHaveText("Backend temporarily unavailable");
  await expect(page.getByRole("status")).toHaveCount(0);
  await expect(page.getByTestId("run-button")).toBeEnabled();
});
