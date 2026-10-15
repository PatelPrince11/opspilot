// README screenshots of the live dashboard: idle, running, done, citation (1440x900, light theme).
//   read -rs OPSPILOT_API_KEY && export OPSPILOT_API_KEY
//   node scripts/screenshot_live.mjs <host> [out_dir]      (out_dir defaults to docs/img/live)
// One investigation (the demo question), so one POST /investigate. The key is read from the
// environment, typed into the dashboard's password field and never printed or written; error
// messages have it masked. A 401 or 429 stops the script: no retry.
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("@playwright/test");

const [host, outArg] = process.argv.slice(2);
const key = process.env.OPSPILOT_API_KEY;
if (!host) {
  console.error("usage: node scripts/screenshot_live.mjs <host> [out_dir]");
  process.exit(2);
}
if (!key) {
  console.error("set OPSPILOT_API_KEY first (read -rs OPSPILOT_API_KEY && export OPSPILOT_API_KEY)");
  process.exit(2);
}
const out = resolve(outArg ?? new URL("../docs/img/live", import.meta.url).pathname);
mkdirSync(out, { recursive: true });
const mask = (s) => String(s).split(key).join("***");

const browser = await chromium.launch();
try {
  // Reduced motion: rows are captured at rest, not mid fade-in.
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  const shot = async (name, fullPage = true) => {
    await page.screenshot({ path: `${out}/live-${name}.png`, fullPage });
    console.log(`wrote ${out}/live-${name}.png`);
  };

  await page.goto(`http://${host}/`);
  await page.getByRole("button", { name: "Load demo incident" }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.getByRole("button", { name: "Load demo incident" }).click();
  await shot("idle"); // before the key is typed: the field is empty

  await page.getByLabel("API key").fill(key);
  const posted = page.waitForResponse((r) => r.url().endsWith("/investigate") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Investigate" }).click();
  const status = (await posted).status();
  if (status === 401) throw new Error("401: the key does not match the server's API_KEYS");
  if (status === 429) throw new Error("429: rate limited (5 per minute per key); wait a minute, then run again");
  if (status !== 200) throw new Error(`POST /investigate returned HTTP ${status}`);

  // Running: a tool call on screen whose result has not arrived (its row shows a spinner).
  const stream = page.getByRole("region", { name: /Live tool calls/ });
  await stream.locator("ol").getByRole("status").first().waitFor({ timeout: 120_000 });
  await shot("running");

  // Done: the run is over (Investigate is back) and the diagnosis cites evidence.
  await page.getByRole("button", { name: "Investigate" }).waitFor({ timeout: 300_000 });
  if (await page.getByRole("alert").count()) throw new Error(`error banner: ${await page.getByRole("alert").innerText()}`);
  const cite = page.getByRole("button", { name: /^Open evidence / }).first();
  await cite.waitFor();
  await shot("done");

  await cite.click();
  await page.getByRole("dialog").locator("mark").first().waitFor();
  await shot("citation", false);
} catch (e) {
  console.error(`screenshot_live: ${mask(e.message)}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
