import { expect, test } from "@playwright/test";
import { existsSync, lstatSync, readdirSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CLIENT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const RUNTIME = path.join(CLIENT_ROOT, ".e2e-runtime");
const WORKSPACE = path.join(RUNTIME, "workspace", "revisions");
const DATABASE = path.join(RUNTIME, "state.sqlite");
const BACKEND = "http://127.0.0.1:8000";
const PYTHON = process.platform === "win32"
  ? path.join(CLIENT_ROOT, "..", "backend", ".venv", "Scripts", "python.exe")
  : path.join(CLIENT_ROOT, "..", "backend", ".venv", "bin", "python");

function revisionPath(revision) {
  return path.join(WORKSPACE, String(revision).padStart(8, "0"));
}

function readTree(root) {
  if (!existsSync(root)) return null;
  const output = {};
  function visit(directory, prefix = "") {
    for (const name of readdirSync(directory).sort()) {
      const absolute = path.join(directory, name);
      const relative = prefix ? `${prefix}/${name}` : name;
      const stat = lstatSync(absolute);
      if (stat.isSymbolicLink()) throw new Error(`Unexpected link in managed workspace: ${relative}`);
      if (stat.isDirectory()) visit(absolute, relative);
      else output[relative] = readFileSync(absolute, "utf8");
    }
  }
  visit(root);
  return output;
}

function databaseRevision() {
  const script = [
    "import sqlite3, sys",
    "connection = sqlite3.connect(sys.argv[1])",
    "print(connection.execute(\"SELECT current_revision FROM workspaces WHERE id='default'\").fetchone()[0])",
  ].join("; ");
  const result = spawnSync(PYTHON, ["-c", script, DATABASE], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || "Unable to read E2E database revision");
  return Number(result.stdout.trim());
}

async function runIdFromPage(page) {
  const match = new URL(page.url()).pathname.match(/^\/runs\/([^/]+)$/);
  if (!match) throw new Error(`Expected run URL, received ${page.url()}`);
  return decodeURIComponent(match[1]);
}

async function getSnapshot(request, runId) {
  const response = await request.get(`${BACKEND}/api/runs/${encodeURIComponent(runId)}`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function getPatch(request, runId) {
  const response = await request.get(`${BACKEND}/api/runs/${encodeURIComponent(runId)}/patch`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function submitTask(page, task) {
  await page.goto("/");
  await page.getByTestId("task-input").fill(task);
  await page.getByTestId("run-button").click();
  await page.waitForURL(/\/runs\/run-/);
  await expect(page.getByTestId("run-status")).toHaveText("AWAITING_APPROVAL");
  await expect(page.getByTestId("file-list").getByTestId("file-item")).toHaveCount(2);
  await expect(page.locator(".monaco-diff-editor")).toBeVisible();
  return runIdFromPage(page);
}

async function expectPublishedPatch(baseTree, patch, revision) {
  const expectedTree = { ...baseTree };
  for (const file of patch.files) {
    if (file.modified == null) delete expectedTree[file.path];
    else expectedTree[file.path] = file.modified;
  }

  const tree = readTree(revisionPath(revision));
  expect(tree).not.toBeNull();
  expect(tree).toEqual(expectedTree);
  for (const file of patch.files) {
    if (file.modified == null) expect(Object.hasOwn(tree, file.path)).toBeFalsy();
    else expect(tree[file.path]).toBe(file.modified);
  }
}

test.describe.serial("backend-authoritative workbench", () => {
  test("active run recovery finds a run when the create response is lost", async ({ page }) => {
    await page.goto("/");
    await page.route("**/api/runs", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      await route.fetch();
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "upstream_lost", message: "response lost" } }),
      });
    });

    await page.getByTestId("task-input").fill("Recover a run after a lost create response");
    await page.getByTestId("run-button").click();
    await expect(page.getByTestId("active-run-recovery")).toBeVisible();
    await expect(page.getByTestId("resume-active-run")).toHaveAttribute("href", /\/runs\/run-/);

    await page.unroute("**/api/runs");
    await page.getByTestId("resume-active-run").click();
    await page.waitForURL(/\/runs\/run-/);
    await expect(page.getByTestId("run-status")).toHaveText("AWAITING_APPROVAL");
    await page.getByTestId("cancel-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("CANCELED");
    await expect(page.getByTestId("current-node")).toHaveText("CANCELED (Finished)");
  });

  test("Reject persists after refresh and leaves the workspace unchanged", async ({ page, request }) => {
    const runId = await submitTask(page, "Prepare the reject workflow evidence");
    const before = await getSnapshot(request, runId);
    const baseTree = readTree(revisionPath(before.workspace_revision));
    const beforeDatabase = databaseRevision();

    await expect(page.getByTestId("diff-modified")).toContainText("Prepare the reject workflow evidence");
    const feedback = "Please add boundary tests <script>alert(1)</script>";
    await page.getByLabel("Decision feedback").fill(feedback);
    await page.getByTestId("reject-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("REJECTED");
    await expect(page.getByTestId("current-node")).toHaveText("REJECTED (Finished)");

    await expect(page.getByTestId("recorded-decision")).toContainText("reject");
    await expect(page.getByTestId("recorded-feedback")).toHaveText(feedback);
    await expect(page.getByTestId("recorded-feedback").locator("script")).toHaveCount(0);
    const rejected = await getSnapshot(request, runId);
    expect(rejected.last_decision.feedback).toBe(feedback);
    expect(rejected.status).toBe("REJECTED");
    expect(rejected.workspace_revision).toBe(before.workspace_revision);
    expect(databaseRevision()).toBe(beforeDatabase);
    expect(readTree(revisionPath(before.workspace_revision))).toEqual(baseTree);
    expect(existsSync(revisionPath(before.workspace_revision + 1))).toBeFalsy();

    await page.reload();
    await expect(page.getByTestId("run-status")).toHaveText("REJECTED");
    await expect(page.getByTestId("current-node")).toHaveText("REJECTED (Finished)");
    await expect(page.getByTestId("recorded-feedback")).toHaveText(feedback);
    await expect(page.getByTestId("recorded-feedback").locator("script")).toHaveCount(0);
    expect((await getSnapshot(request, runId)).status).toBe("REJECTED");
  });

  test("Approve publishes the persisted patch and restores COMPLETE after refresh", async ({ page, request }) => {
    const runId = await submitTask(page, "Prepare the approve workflow evidence");
    const before = await getSnapshot(request, runId);
    const patch = await getPatch(request, runId);
    const baseTree = readTree(revisionPath(before.workspace_revision));
    const beforeDatabase = databaseRevision();

    await page.getByTestId("approve-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("COMPLETE");
    await expect(page.getByTestId("current-node")).toHaveText("COMPLETE (Finished)");
    const complete = await getSnapshot(request, runId);
    expect(complete.status).toBe("COMPLETE");
    expect(complete.workspace_revision).toBe(before.workspace_revision + 1);
    expect(databaseRevision()).toBe(beforeDatabase + 1);
    await expectPublishedPatch(baseTree, patch, complete.workspace_revision);

    await page.reload();
    await expect(page.getByTestId("run-status")).toHaveText("COMPLETE");
    await expect(page.getByTestId("current-node")).toHaveText("COMPLETE (Finished)");
    await expect(page.getByTestId("workspace-revision")).toHaveText(`r${complete.workspace_revision}`);
    expect((await getSnapshot(request, runId)).status).toBe("COMPLETE");
  });

  test("Cancel persists after refresh and does not publish a revision", async ({ page, request }) => {
    const runId = await submitTask(page, "Prepare the cancel workflow evidence");
    const before = await getSnapshot(request, runId);
    const baseTree = readTree(revisionPath(before.workspace_revision));
    const beforeDatabase = databaseRevision();

    await page.getByTestId("cancel-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("CANCELED");
    await expect(page.getByTestId("current-node")).toHaveText("CANCELED (Finished)");
    const canceled = await getSnapshot(request, runId);
    expect(canceled.status).toBe("CANCELED");
    expect(canceled.workspace_revision).toBe(before.workspace_revision);
    expect(databaseRevision()).toBe(beforeDatabase);
    expect(readTree(revisionPath(before.workspace_revision))).toEqual(baseTree);
    expect(existsSync(revisionPath(before.workspace_revision + 1))).toBeFalsy();

    await page.reload();
    await expect(page.getByTestId("run-status")).toHaveText("CANCELED");
    await expect(page.getByTestId("current-node")).toHaveText("CANCELED (Finished)");
  });

  test("stale revision errors remain non-terminal and retries reuse one decision id", async ({ page, request }) => {
    const runId = await submitTask(page, "Exercise the stale revision response");
    const before = await getSnapshot(request, runId);
    const baseTree = readTree(revisionPath(before.workspace_revision));
    const beforeDatabase = databaseRevision();
    let intercepted = 0;

    await page.route("**/api/runs/*/approve", async (route) => {
      const original = JSON.parse(route.request().postData() || "{}");
      const headers = { ...route.request().headers() };
      delete headers["content-length"];
      intercepted += 1;
      await route.continue({
        headers,
        postData: JSON.stringify({ ...original, patch_revision: original.patch_revision + 1 }),
      });
    });

    await page.getByLabel("Decision feedback").fill("first feedback");
    await page.getByTestId("approve-button").click();
    await expect(page.getByTestId("decision-error")).toContainText("Stale revision");
    await expect(page.getByTestId("run-status")).toHaveText("AWAITING_APPROVAL");
    const firstDecision = await page.evaluate((id) => {
      const key = Object.keys(localStorage).find((item) => item.startsWith(`devflow:decision:${id}:approve:`));
      return key ? JSON.parse(localStorage.getItem(key)).decision_id : null;
    }, runId);

    await page.getByTestId("approve-button").click();
    await expect.poll(() => intercepted).toBe(2);
    const secondDecision = await page.evaluate((id) => {
      const key = Object.keys(localStorage).find((item) => item.startsWith(`devflow:decision:${id}:approve:`));
      return key ? JSON.parse(localStorage.getItem(key)).decision_id : null;
    }, runId);
    expect(secondDecision).toBe(firstDecision);

    await page.getByLabel("Decision feedback").fill("updated feedback");
    await page.getByTestId("approve-button").click();
    await expect.poll(() => intercepted).toBe(3);
    const changedDecision = await page.evaluate((id) => {
      const key = Object.keys(localStorage).find((item) => item.startsWith(`devflow:decision:${id}:approve:`));
      return key ? JSON.parse(localStorage.getItem(key)) : null;
    }, runId);
    expect(changedDecision.decision_id).not.toBe(firstDecision);
    expect(changedDecision.feedback).toBe("updated feedback");
    expect((await getSnapshot(request, runId)).status).toBe("AWAITING_APPROVAL");
    expect(databaseRevision()).toBe(beforeDatabase);
    expect(readTree(revisionPath(before.workspace_revision))).toEqual(baseTree);

    await page.unroute("**/api/runs/*/approve");
    await page.getByTestId("reject-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("REJECTED");
    await expect(page.getByTestId("current-node")).toHaveText("REJECTED (Finished)");
  });

  test("SSE interruption reconnects from the cursor without gaps or status rollback", async ({ page, request }) => {
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window);
      window.__devflowSseCursors = [];
      window.__devflowCutFirstStream = false;
      window.fetch = async (input, init) => {
        const request = new Request(input, init);
        if (!new URL(request.url).pathname.endsWith("/events")) return originalFetch(input, init);
        window.__devflowSseCursors.push(request.headers.get("Last-Event-ID"));
        const response = await originalFetch(input, init);
        if (window.__devflowCutFirstStream || !response.body) return response;
        window.__devflowCutFirstStream = true;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          async start(controller) {
            let buffered = "";
            let businessEvents = 0;
            try {
              while (businessEvents < 3) {
                const { value, done } = await reader.read();
                if (done) break;
                buffered += decoder.decode(value, { stream: true });
                let boundary = buffered.indexOf("\n\n");
                while (boundary >= 0) {
                  const frame = buffered.slice(0, boundary + 2);
                  buffered = buffered.slice(boundary + 2);
                  controller.enqueue(encoder.encode(frame));
                  if (/^id:/m.test(frame)) businessEvents += 1;
                  if (businessEvents === 3) {
                    await reader.cancel();
                    controller.close();
                    return;
                  }
                  boundary = buffered.indexOf("\n\n");
                }
              }
              controller.close();
            } catch (error) {
              controller.error(error);
            }
          },
          cancel() { return reader.cancel(); },
        });
        return new Response(stream, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      };
    });

    const runId = await submitTask(page, "Exercise browser SSE reconnection");
    const snapshot = await getSnapshot(request, runId);
    await expect.poll(() => page.evaluate(() => window.__devflowSseCursors)).toEqual(["0", "3"]);
    await expect(page.getByTestId("connection-history")).toContainText("connected,reconnecting,connected");
    const eventItems = page.locator('[data-testid^="event-"]');
    await expect(eventItems).toHaveCount(snapshot.latest_seq);
    const sequences = await eventItems.evaluateAll((items) => (
      items.map((item) => Number(item.dataset.seq))
    ));
    expect(sequences).toEqual(Array.from({ length: snapshot.latest_seq }, (_, index) => index + 1));
    const statuses = (await page.getByTestId("status-history").textContent()).split(",").filter(Boolean);
    expect(statuses).not.toContain("RUNNING");
    expect(statuses.at(-1)).toBe("AWAITING_APPROVAL");

    await page.getByTestId("cancel-button").click();
    await expect(page.getByTestId("run-status")).toHaveText("CANCELED");
    await expect(page.getByTestId("current-node")).toHaveText("CANCELED (Finished)");
  });
});
