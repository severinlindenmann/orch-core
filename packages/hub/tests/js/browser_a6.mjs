// A6 in a real browser: the person home (needs-you left, workspaces and signing places right) and the admin console.
// Driven by tests/test_browser_a6.py (HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, optional SHOT_DIR). Run under the playwright lease.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const { chromium } = createRequire(import.meta.url)("playwright");
const { HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, SHOT_DIR } = process.env;
const assert = (c, m) => { if (!c) throw new Error("A6: " + m); };

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
const problems = [];
page.on("console", (m) => { if (/Content Security Policy|Trusted Type/i.test(m.text())) problems.push(m.text()); });
page.on("pageerror", (e) => problems.push(String(e)));
const shot = async (name) => {
  if (!SHOT_DIR) return;
  mkdirSync(SHOT_DIR, { recursive: true });
  for (const width of [1400, 390]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
    await page.screenshot({ path: `${SHOT_DIR}/a6-${name}-${width}.png`, fullPage: true });
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert(!wide, `${name} at ${width}px scrolls horizontally`);
  }
  await page.setViewportSize({ width: 1400, height: 900 });
};

try {
  await page.goto(HUB_URL + "/");
  await page.fill("#user", HUB_USER);
  await page.fill("#pw", HUB_PW);
  await page.fill("#code", CODE_SIGNIN);
  await page.getByRole("button", { name: "Sign in" }).click();

  // person home
  await page.getByRole("heading", { name: "Needs you" }).waitFor();
  await page.getByRole("heading", { name: "My workspaces" }).waitFor();
  await page.getByRole("heading", { name: "Where you can sign" }).waitFor();
  await page.getByText("CSV or API?").first().waitFor();
  assert(await page.getByText("Hub can read everything").first().isVisible(), "full workspace plain line");
  assert((await page.getByRole("button", { name: /^Approve/ }).count()) === 0, "no direct Approve on the home");
  assert((await page.getByRole("button", { name: "Review" }).count()) >= 1, "Review button on the needs-you row");
  await shot("home");

  // admin console
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("heading", { name: "Admin" }).waitFor();
  await page.getByText("Groups managed here; external sync not configured").waitFor();
  await page.getByRole("heading", { name: "People & groups" }).waitFor();
  await page.getByText(/^Adding .* to .* gives /).first().waitFor();
  await shot("admin-people");
  for (const [tab, heading] of [["Access", "Access"], ["Workspaces", "Workspaces"], ["Marketplaces", "Marketplaces"], ["Audit", "Audit"]]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.getByRole("heading", { name: heading, exact: true }).waitFor();
    await shot("admin-" + tab.toLowerCase());
  }
  // D14: the hub marketplace policy: block one add-on, the version rises
  await page.getByRole("tab", { name: "Marketplaces" }).click();
  await page.getByText("Official", { exact: true }).first().waitFor();
  await page.getByLabel("Never allow these add-ons").fill("wiki@1.0.0");
  await page.getByRole("button", { name: "Save add-on rules" }).click();
  await page.getByText("Saved").waitFor();
  await page.getByText(/Version 2\./).waitFor();
  await shot("admin-marketplaces-saved");
  // add a person, then remove them with the two-step button
  await page.getByRole("tab", { name: "People & groups" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Jonas");
  await page.getByRole("button", { name: "Add person" }).click();
  await page.getByText("Saved").waitFor();
  await page.locator("b", { hasText: "Jonas" }).first().waitFor();

  assert(problems.length === 0, "CSP/Trusted Types violations: " + problems.join(" | "));
  console.log("A6 browser: ok");
} finally {
  await browser.close();
}
