import { expect, test } from "@playwright/test";

async function setup(page, provider = "chat_completions") {
  await page.route("**/api/health", (route) => route.fulfill({ json: { provider } }));
  await page.route("**/api/runs/active", (route) => route.fulfill({ json: null }));
  await page.route("**/api/runs?*", (route) => route.fulfill({ json: { runs: [], next_offset: null } }));
  await page.route("**/api/settings/provider", (route) => route.fulfill({ json: {
    base_url: "https://provider.example/v1", model: "test-model", key_configured: true, allow_local_http: false,
  } }));
  await page.route("**/api/project/preview", (route) => route.fulfill({ json: {
    commit: "commit-one", files: ["src/main.py", "pyproject.toml"], excluded: [".env"], errors: [], dependency_sources: ["pyproject.toml"],
    dependency_details: [{ source: "pyproject.toml", requirements: ["requests>=2"], extras: ["dev", "test"], errors: [] }], warnings: [],
  } }));
}

test("provider secrets are write-only, omitted when unchanged, tested without code, and cleared", async ({ page }) => {
  await setup(page);
  let configured = false;
  const writes = [];
  let tests = 0;
  await page.route("**/api/settings/provider", async (route) => {
    const request = route.request();
    if (request.method() !== "GET") {
      expect(request.headers()["x-devflow-request"]).toBe("1");
      if (request.method() === "PUT") { writes.push(request.postDataJSON()); configured = true; }
      if (request.method() === "DELETE") configured = false;
    }
    await route.fulfill({ json: { base_url: "https://provider.example/v1", model: "test-model", key_configured: configured, allow_local_http: false } });
  });
  await page.route("**/api/settings/provider/test", (route) => {
    expect(route.request().postDataJSON()).toEqual({});
    expect(route.request().headers()["x-devflow-request"]).toBe("1");
    tests += 1;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/");
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("test-model");
  await page.getByLabel("API key", { exact: false }).fill("secret-not-for-storage");
  await page.getByRole("button", { name: "Save provider", exact: true }).click();
  await expect(page.getByLabel("API key", { exact: false })).toHaveValue("");
  expect(writes[0].api_key).toBe("secret-not-for-storage");
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("secret-not-for-storage");
  await page.getByRole("button", { name: "Save provider", exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).not.toHaveProperty("api_key");
  await page.getByRole("button", { name: "Test saved provider (may charge)", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "No project code was sent" })).toBeVisible();
  expect(tests).toBe(1);
  await page.getByRole("button", { name: "Clear provider", exact: true }).click();
  await expect(page.getByLabel("API key (not configured)", { exact: true })).toBeVisible();
});

test("import requires explicit consent and sends exact commit, dependency source, and extras", async ({ page }) => {
  await setup(page);
  const imports = [];
  const launches = [];
  await page.route("**/api/imports", (route) => {
    expect(route.request().headers()["x-devflow-request"]).toBe("1");
    imports.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: { import_id: "import-one", commit: "commit-one" } });
  });
  await page.route("**/api/runs", (route) => {
    launches.push(route.request().postDataJSON());
    return route.fulfill({ status: 503, json: { error: { code: "provider_error", message: "Provider unavailable" } } });
  });
  await page.goto("/?task=Implement%20change");
  await expect(page.getByRole("button", { name: "Import selected source" })).toBeDisabled();
  await expect(page.getByTestId("run-button")).toBeDisabled();
  await page.getByLabel("Dependency source", { exact: true }).selectOption("pyproject.toml");
  await page.getByLabel("Dependency extras", { exact: false }).fill("dev, test");
  await page.getByRole("checkbox", { name: /I consent/ }).check();
  await page.getByRole("button", { name: "Import selected source" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Imported import-one" })).toBeVisible();
  expect(imports).toEqual([{ commit: "commit-one", dependency_source: "pyproject.toml", extras: ["dev", "test"] }]);
  await page.getByTestId("run-button").click();
  await expect(page.getByRole("alert").filter({ hasText: "Provider unavailable" })).toBeVisible();
  expect(launches[0].import_id).toBe("import-one");
  await expect(page.getByLabel("Provider mode")).not.toContainText("Demo mode");
  await page.getByLabel("Dependency extras", { exact: false }).fill("test");
  await expect(page.getByRole("checkbox", { name: /I consent/ })).not.toBeChecked();
  await expect(page.getByTestId("run-button")).toBeDisabled();
});

test("preview errors block import even after consent", async ({ page }) => {
  await setup(page);
  await page.route("**/api/project/preview", (route) => route.fulfill({ json: {
    commit: "commit-one", files: [], excluded: [], errors: ["Working tree must be clean"], dependency_sources: [],
  } }));
  await page.goto("/");
  await page.getByRole("checkbox", { name: /I consent/ }).check();
  await expect(page.getByLabel("Project import").getByRole("alert")).toContainText("Working tree must be clean");
  await expect(page.getByRole("button", { name: "Import selected source" })).toBeDisabled();
});

test("dependency warnings and selected details are visible; source errors block import and switching resets extras", async ({ page }) => {
  await setup(page);
  const imports = [];
  await page.route("**/api/project/preview", (route) => route.fulfill({ json: {
    commit: "commit-one", files: ["pyproject.toml", "requirements.txt", "setup.py", "package.json"], excluded: [], errors: [],
    warnings: ["setup.py is not supported", "package.json is not supported"],
    dependency_sources: ["pyproject.toml", "requirements.txt"],
    dependency_details: [
      { source: "pyproject.toml", requirements: ["requests>=2"], extras: ["dev", "test"], errors: [] },
      { source: "requirements.txt", requirements: [], extras: [], errors: ["Unsupported editable dependency"] },
    ],
  } }));
  await page.route("**/api/imports", (route) => {
    imports.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: { import_id: "import-one", commit: "commit-one" } });
  });
  await page.goto("/");
  await expect(page.getByLabel("Preview warnings")).toContainText("setup.py is not supported");
  await expect(page.getByLabel("Preview warnings")).toContainText("package.json is not supported");
  const source = page.getByLabel("Dependency source", { exact: true });
  await expect(source.locator("option")).toHaveText(["None", "pyproject.toml", "requirements.txt"]);
  await source.selectOption("pyproject.toml");
  const details = page.getByLabel("Selected dependency details");
  await expect(details).toContainText("requests>=2");
  await expect(details.getByRole("listitem")).toHaveText(["requests>=2", "dev", "test"]);
  await page.getByLabel("Dependency extras", { exact: false }).fill("dev");
  await page.getByRole("checkbox", { name: /I consent/ }).check();
  await expect(page.getByRole("button", { name: "Import selected source" })).toBeEnabled();
  await source.selectOption("requirements.txt");
  await expect(page.getByLabel("Dependency extras", { exact: false })).toHaveValue("");
  await expect(page.getByRole("checkbox", { name: /I consent/ })).not.toBeChecked();
  await expect(details).not.toContainText("requests>=2");
  await expect(details).toContainText("No requirements listed.");
  await expect(details).toContainText("No extras available.");
  await expect(details.getByRole("alert")).toContainText("Unsupported editable dependency");
  await page.getByRole("checkbox", { name: /I consent/ }).check();
  await expect(page.getByRole("button", { name: "Import selected source" })).toBeDisabled();
  expect(imports).toEqual([]);
  await source.selectOption("");
  await expect(details).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /I consent/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /I consent/ }).check();
  await page.getByRole("button", { name: "Import selected source" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Imported import-one" })).toBeVisible();
  expect(imports).toEqual([{ commit: "commit-one", dependency_source: null, extras: [] }]);
});

test("history paginates and copies a rejected task through URL search params", async ({ page }) => {
  await setup(page);
  const task = "Fix <script> & review + résumé?";
  await page.route("**/api/runs?*", (route) => {
    const offset = new URL(route.request().url()).searchParams.get("offset");
    return route.fulfill({ json: { runs: [{ run_id: `run-${offset}`, task: offset === "0" ? "First task" : task,
      status: offset === "0" ? "FAILED" : "REJECTED", provider: "chat_completions", source_commit: "commit-one" }], next_offset: offset === "0" ? 20 : null } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Load more runs" }).click();
  const row = page.getByLabel("Run history").locator("li").filter({ hasText: task });
  await row.getByRole("link", { name: "Copy to new task" }).click();
  await expect(page.getByTestId("task-input")).toHaveValue(task);
  expect(new URL(page.url()).searchParams.get("task")).toBe(task);
});

function runSnapshot(overrides = {}) {
  return { run_id: "run-beta", task: "Prepare beta", status: "RUNNING", latest_seq: 0, patch_revision: 0,
    workspace_revision: 0, base_workspace_revision: 0, provider: "chat_completions", source_commit: "commit-one",
    import_id: "import-one", stop_requested: false, error: null, ...overrides };
}

async function workbench(page, current) {
  await page.route("**/api/health", (route) => route.fulfill({ json: { provider: "chat_completions" } }));
  await page.route("**/api/runs/run-beta", (route) => route.fulfill({ json: current() }));
  await page.route("**/api/runs/run-beta/events", (route) => route.fulfill({ contentType: "text/event-stream", body: ": heartbeat\n\n" }));
}

test("stop uses its own endpoint and stays pending until backend confirms terminal state", async ({ page }) => {
  let current = runSnapshot();
  await workbench(page, () => current);
  await page.route("**/api/runs/run-beta/stop", (route) => {
    expect(route.request().headers()["x-devflow-request"]).toBe("1");
    current = { ...current, stop_requested: true };
    return route.fulfill({ json: current });
  });
  await page.goto("/runs/run-beta");
  await expect(page.getByTestId("cancel-button")).toBeDisabled();
  await page.getByTestId("stop-button").click();
  await expect(page.getByTestId("stop-button")).toHaveText("Stop requested");
  await expect(page.getByTestId("stop-button")).toBeDisabled();
  await expect(page.getByTestId("run-status")).toHaveText("RUNNING");
  current = { ...current, status: "CANCELED" };
  await expect(page.getByTestId("run-status")).toHaveText("CANCELED");
  await expect(page.getByTestId("stop-button")).toHaveCount(0);
});

test("failed run displays structured error and can be copied without automatic retry", async ({ page }) => {
  await setup(page);
  await workbench(page, () => runSnapshot({ status: "FAILED", error: { code: "provider_timeout", phase: "plan", message: "Provider timed out" } }));
  await page.goto("/runs/run-beta");
  await expect(page.locator(".run-context").getByRole("alert")).toContainText("provider_timeout · plan: Provider timed out");
  await page.getByRole("link", { name: "Copy to new task" }).click();
  await expect(page.getByTestId("task-input")).toHaveValue("Prepare beta");
  await expect(page.getByTestId("run-button")).toBeDisabled();
});

for (const imported of [true, false]) {
  test(`approved patch download requires imported source: ${imported}`, async ({ page }) => {
    await workbench(page, () => runSnapshot({ status: "COMPLETE", import_id: imported ? "import-one" : null, last_decision: { kind: "approve" } }));
    await page.route("**/api/runs/run-beta/patch/download", (route) => {
      expect(route.request().method()).toBe("GET");
      return route.fulfill({ headers: { "Content-Disposition": "attachment; filename=devflow.patch" }, contentType: "text/x-diff", body: "diff --git a/a b/a\n" });
    });
    await page.goto("/runs/run-beta");
    await expect(page.getByTestId("run-status")).toHaveText("Approved — patch ready");
    if (!imported) { await expect(page.getByTestId("download-patch")).toHaveCount(0); return; }
    await expect(page.locator(".run-context")).toContainText("git apply --check devflow.patch");
    const downloadPromise = page.waitForEvent("download");
    await page.getByTestId("download-patch").click();
    expect((await downloadPromise).suggestedFilename()).toBe("devflow.patch");
  });
}
