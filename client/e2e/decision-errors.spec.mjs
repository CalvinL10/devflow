import { expect, test } from "@playwright/test";

for (const scenario of ["conflict", "own-lost-response", "other-lost-response", "own-conflict", "own-failed"]) {
  test(`decision error reconciliation: ${scenario}`, async ({ page }) => {
    const runId = "run-decision-error";
    let current = {
      run_id: runId, task: "Reconcile decision errors", status: "AWAITING_APPROVAL",
      latest_seq: 0, workspace_revision: 0, base_workspace_revision: 0,
      patch_revision: 1, pending_decision: null, last_decision: null,
    };
    await page.route(`**/api/runs/${runId}`, (route) => route.fulfill({ json: current }));
    await page.route(`**/api/runs/${runId}/events`, (route) => route.fulfill({
      contentType: "text/event-stream", body: ": heartbeat\n\n",
    }));
    await page.route(`**/api/runs/${runId}/patch`, (route) => route.fulfill({ json: {
      run_id: runId, patch_revision: 1,
      files: [{ path: "example.py", original: null, modified: "value = 1\n" }],
    } }));
    await page.route(`**/api/runs/${runId}/approve`, async (route) => {
      const submitted = route.request().postDataJSON();
      current = { ...current, status: scenario === "own-failed" ? "FAILED" : "COMPLETE", workspace_revision: 1,
        last_decision: { ...submitted, kind: "approve",
          decision_id: scenario.startsWith("own-") ? submitted.decision_id : "another-reviewer",
        },
      };
      await route.fulfill({ status: scenario.includes("conflict") ? 409 : 502, json: {
        error: { code: scenario.includes("conflict") ? "revision_conflict" : "upstream_lost",
          message: "response lost",
        },
      } });
    });
    await page.goto(`/runs/${runId}`);
    await expect(page.getByTestId("approve-button")).toBeEnabled();
    await page.getByTestId("approve-button").click();
    await expect(page.getByTestId("run-status")).toHaveText(scenario === "own-failed" ? "FAILED" : "COMPLETE");
    await expect(page.getByTestId("approve-button")).toHaveText("Approve");
    if (scenario === "own-lost-response") {
      await expect(page.getByTestId("decision-error")).toHaveCount(0);
    } else {
      await expect(page.getByTestId("decision-error")).toContainText(
        scenario.includes("conflict") ? "Stale revision" : "response lost",
      );
    }
  });
}
