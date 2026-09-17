import { expect, test } from "@playwright/test";

const RUN_ID = "run-timing";

function snapshot(status, latestSeq, pendingDecision = null) {
  return {
    run_id: RUN_ID, task: "Controlled recovery timing", status,
    latest_seq: latestSeq, patch_revision: 1, base_workspace_revision: 0,
    workspace_revision: 0, pending_decision: pendingDecision,
  };
}

const patch = {
  run_id: RUN_ID, patch_revision: 1,
  files: [{ path: "example.py", original: null, modified: "value = 1\n" }],
};

for (const [kind, status] of [["reject", "REJECTED"], ["cancel", "CANCELED"]]) {
  test(`${kind} keeps SSE until its recorded decision is finalized`, async ({ page }) => {
    let current = snapshot(status, 1, { decision_id: "timing-d", kind });
    await installEventTransport(page);
    await page.route(`**/api/runs/${RUN_ID}`, (route) => route.fulfill({ json: current }));
    await page.route(`**/api/runs/${RUN_ID}/patch`, (route) => route.fulfill({ json: patch }));
    await page.goto(`/runs/${RUN_ID}`);
    await expect(page.getByTestId("pending-decision")).toBeVisible();
    await page.waitForFunction(() => typeof window.__emitRunEvent === "function");
    await page.evaluate(() => window.__emitRunEvent(1));
    await expect(page.getByTestId("event-1")).toBeVisible();
    // Drain the UI update and any queued abort microtask before checking it.
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await expect(page.getByTestId("connection-status")).toHaveText("Connected", { timeout: 3000 });

    current = snapshot(status, 2);
    await page.evaluate(() => window.__emitRunEvent(2));
    await expect(page.getByTestId("pending-decision")).toHaveCount(0);
    await expect(page.getByTestId("event-2")).toBeVisible();
    await expect(page.getByTestId("connection-status")).toHaveText("Synced / closed");
    await expect(page.getByTestId("run-status")).toHaveText(status);
  });
}

async function installEventTransport(page) {
  // Keep the real workbench and SSE parser; control only the transport timing.
  await page.addInitScript((runId) => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const request = new Request(input, init);
      if (!new URL(request.url).pathname.endsWith(`/runs/${runId}/events`)) {
        return originalFetch(input, init);
      }
      const encoder = new TextEncoder();
      const body = new ReadableStream({
        start(controller) {
          window.__emitRunEvent = (seq) => {
            const event = { run_id: runId, seq, type: "decision.progress", node: "await_approval" };
            controller.enqueue(encoder.encode(`id: ${seq}\nevent: run.event\ndata: ${JSON.stringify(event)}\n\n`));
          };
          request.signal.addEventListener("abort", () => controller.close(), { once: true });
        },
      });
      return new Response(body, { headers: { "content-type": "text/event-stream" } });
    };
  }, RUN_ID);
}

test("patch not ready at revision 1 is loaded when that same revision reaches approval", async ({ page }) => {
  let current = snapshot("RUNNING", 0);
  let patchGets = 0;
  await installEventTransport(page);
  await page.route(`**/api/runs/${RUN_ID}`, (route) => route.fulfill({ json: current }));
  await page.route(`**/api/runs/${RUN_ID}/patch`, (route) => {
    patchGets += 1;
    return current.status === "RUNNING"
      ? route.fulfill({ status: 409, json: { error: { code: "patch_not_ready", message: "not ready" } } })
      : route.fulfill({ json: patch });
  });
  await page.goto(`/runs/${RUN_ID}`);
  await expect(page.getByTestId("run-status")).toHaveText("RUNNING");
  await expect.poll(() => patchGets).toBeGreaterThan(0);
  await expect(page.getByTestId("approve-button")).toBeDisabled();
  await page.waitForFunction(() => typeof window.__emitRunEvent === "function");

  current = snapshot("AWAITING_APPROVAL", 1);
  await page.evaluate(() => window.__emitRunEvent(1));

  await expect(page.getByTestId("run-status")).toHaveText("AWAITING_APPROVAL");
  await expect(page.getByTestId("file-item")).toHaveCount(1, { timeout: 5000 });
  await expect(page.getByTestId("approve-button")).toBeEnabled();
  await expect(page.getByTestId("reject-button")).toBeEnabled();
  await expect(page.getByTestId("cancel-button")).toBeEnabled();
});
