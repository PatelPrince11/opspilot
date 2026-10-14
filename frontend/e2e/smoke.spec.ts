import { expect, test } from "@playwright/test";

test("a full investigation streams, finishes and opens a cited quote", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("API key").fill("e2e-key"); // tests/e2e_server.py sets API_KEYS
  await page.getByRole("button", { name: "Load demo incident" }).click();
  await page.getByRole("button", { name: "Investigate" }).click();

  const stream = page.getByRole("region", { name: /Live tool calls/ });
  await expect(stream.getByText("get_service_status")).toBeVisible(); // first look, live incident
  await expect(page.getByText("Defenses on: wrap, flag")).toBeVisible();

  await expect(page.getByText("payments-api logged connection errors")).toBeVisible({ timeout: 15_000 });
  await expect(stream.getByText("search_runbooks")).toBeVisible();
  await expect(page.getByRole("button", { name: "Investigate" })).toBeEnabled();
  await expect(page.getByLabel("Run stats").getByText("3", { exact: true })).toBeVisible(); // tool calls

  await page.getByRole("button", { name: "Open evidence log:1_0" }).click();
  await expect(page.getByRole("dialog").locator("mark")).toHaveText("ERROR baz xxxxxxxxxx");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();

  // Theme toggle: pins the tokens' dark side whatever the OS preference.
  await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: "dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(11, 14, 18)"); // --canvas, dark
});
