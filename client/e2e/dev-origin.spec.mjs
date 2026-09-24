import { expect, test } from "@playwright/test";

test("documented loopback origins can load dev assets but unrelated origins cannot", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByTestId("task-input")).toBeVisible();
  const asset = await page.locator('script[src^="/_next/"]').first().getAttribute("src");
  expect(asset).toBeTruthy();
  for (const origin of ["http://127.0.0.1:3000", "http://localhost:3000"]) {
    const response = await request.get(asset, { headers: { Origin: origin, Host: new URL(origin).host } });
    expect(response.status(), origin).toBe(200);
  }
  const denied = await request.get(asset, { headers: { Origin: "https://untrusted.example" } });
  expect(denied.status()).toBe(403);
});

test("frontend rejects DNS-rebinding hosts before rewrite and enforces same-origin mutations", async ({ request }) => {
  for (const path of ["/", "/api/health", "/api/settings/provider", "/api/runs/nonexistent/patch/download"]) {
    const denied = await request.get(path, { headers: { Host: "attacker.example:3000", "X-Forwarded-Host": "127.0.0.1:3000" } });
    expect(denied.status()).toBe(403);
    expect((await denied.json()).error.code).toBe("host_denied");
  }
  for (const origin of ["http://attacker.example", "http://127.0.0.1:3001", "null"]) {
    const denied = await request.post("/api/runs", { headers: { Origin: origin, "X-DevFlow-Request": "1" }, data: {} });
    expect(denied.status()).toBe(403);
    expect((await denied.json()).error.code).toBe("origin_denied");
  }
  const unmarked = await request.post("/api/runs", { data: {} });
  expect(unmarked.status()).toBe(403);
  expect((await unmarked.json()).error.code).toBe("csrf_denied");
  const sameOrigin = await request.get("/api/health", { headers: { Origin: "http://127.0.0.1:3000" } });
  expect(sameOrigin.status()).toBe(200);
});
