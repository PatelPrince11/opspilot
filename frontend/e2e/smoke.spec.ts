import AxeBuilder from "@axe-core/playwright";
import { type Page, expect, test } from "@playwright/test";

// axe's color-contrast rule over the whole page: element, measured ratio and required ratio.
async function contrast(page: Page, label: string) {
  const { violations, incomplete, passes } = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
  const found = violations.flatMap((v) =>
    v.nodes.map((n) => {
      const d = n.any[0]?.data as { contrastRatio?: number; expectedContrastRatio?: string } | undefined;
      return `${n.target.join(" ")}: ${d?.contrastRatio} < ${d?.expectedContrastRatio}`;
    }),
  );
  // incomplete = nodes axe could not measure. In the open <dialog> (top layer) axe cannot resolve
  // the background of the evidence <pre> and its <mark>, so those are measured here instead: the
  // computed text colour against the nearest opaque background, with the WCAG formula.
  const unsure = incomplete.flatMap((v) => v.nodes.map((n) => n.target.join(" ")));
  for (const sel of unsure) {
    const ratio = await page.locator(sel).first().evaluate((el) => {
      const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
      const lum = (c: number[]) => {
        const [r, g, b] = c.slice(0, 3).map((v) => (v / 255 <= 0.04045 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4));
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      let bg: Element | null = el;
      while (bg && rgb(getComputedStyle(bg).backgroundColor)[3] === 0) bg = bg.parentElement;
      const [a, b] = [lum(rgb(getComputedStyle(el).color)), lum(rgb(getComputedStyle(bg!).backgroundColor))].sort((x, y) => y - x);
      return Math.round(((a + 0.05) / (b + 0.05)) * 100) / 100;
    });
    if (ratio < 4.5) found.push(`${sel}: ${ratio} < 4.5:1 (measured directly)`);
    unsure[unsure.indexOf(sel)] = `${sel} ${ratio}:1 measured directly`;
  }
  const passed = passes.reduce((k, v) => k + v.nodes.length, 0);
  console.log(
    `axe color-contrast ${label}: ${passed} passed, ${found.length ? found.join("; ") : "0 violations"}` +
      `${unsure.length ? `; incomplete: ${unsure.join("; ")}` : ", 0 incomplete"}`,
  );
  expect(found, label).toEqual([]);
}

for (const theme of ["dark", "light"] as const) {
  test.describe(() => {
    // Content at rest: the rise animation fades new rows in, and axe measures opacity mid-fade.
    test.use({ reducedMotion: "reduce" });
    test(`WCAG AA contrast, ${theme} theme: idle, error, running, done, citation open`, async ({ page }) => {
      await page.goto("/");
      await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: theme }).click();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect.poll(() => page.evaluate(() => document.fonts.check("16px Inter"))).toBe(true); // measured in Inter
      await contrast(page, `${theme} idle`);

      // Error: a wrong API key gets the real API's 401 (no model call).
      await page.getByLabel("API key").fill("wrong-key");
      await page.getByRole("button", { name: "Load demo incident" }).click();
      await page.getByRole("button", { name: "Investigate" }).click();
      await expect(page.getByRole("alert")).toContainText("401");
      await contrast(page, `${theme} error`);

      await page.getByLabel("API key").fill("e2e-key");
      await page.getByRole("button", { name: "Investigate" }).click();

      await expect(page.getByRole("region", { name: /Live tool calls/ }).getByText("query_logs")).toBeVisible();
      await expect(page.getByRole("button", { name: "Stop" })).toBeVisible(); // still running
      await contrast(page, `${theme} running`);

      await expect(page.getByText("payments-api logged connection errors")).toBeVisible({ timeout: 15_000 });
      await contrast(page, `${theme} done`);

      await page.getByRole("button", { name: "Open evidence log:1_0" }).click();
      await expect(page.getByRole("dialog").locator("mark")).toBeVisible();
      await contrast(page, `${theme} citation`);
    });
  });
}

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

  // Light by default whatever the OS preference; the toggle switches the tokens to dark.
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(242, 242, 242)"); // --canvas, light
  await expect(page.locator("body")).toHaveCSS("font-family", /^Inter/); // self-hosted face
  await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: "dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(15, 20, 38)"); // --canvas, dark
});
