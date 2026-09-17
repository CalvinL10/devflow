import { expect, test } from "@playwright/test";

test("documented loopback origins can load dev assets but unrelated origins cannot", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByTestId("task-input")).toBeVisible();
  const asset = await page.locator('script[src^="/_next/"]').first().getAttribute("src");
  expect(asset).toBeTruthy();
  for (const origin of ["http://127.0.0.1:3000", "http://localhost:3000"]) {
    const response = await request.get(asset, { headers: { Origin: origin } });
    expect(response.status(), origin).toBe(200);
  }
  const denied = await request.get(asset, { headers: { Origin: "https://untrusted.example" } });
  expect(denied.status()).toBe(403);
});
