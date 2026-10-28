// Run-11 #12/#13 in a real browser: the Services page and the Workspaces page. Driven by tests/test_browser_r11.py.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const { chromium } = createRequire(import.meta.url)("playwright");
const { HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, SHOT_DIR } = process.env;
const assert = (c, m) => { if (!c) throw new Error("R11: " + m); };

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: HUB_URL }).catch(() => {});
const page = await ctx.newPage();
const problems = [];
page.on("console", (m) => { if (/Content Security Policy|Trusted Type/i.test(m.text())) problems.push(m.text()); });
page.on("pageerror", (e) => { console.log("PAGEERROR", String(e)); problems.push(String(e)); });
const shot = async (name) => {
  if (!SHOT_DIR) return;
  mkdirSync(SHOT_DIR, { recursive: true });
  for (const width of [1400, 390]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
    await page.screenshot({ path: `${SHOT_DIR}/r11-${name}-${width}.png`, fullPage: true });
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)), `${name} at ${width}px scrolls horizontally`);
  }
  await page.setViewportSize({ width: 1400, height: 900 });
};

try {
  await page.goto(HUB_URL + "/");
  await page.fill("#user", HUB_USER);
  await page.fill("#pw", HUB_PW);
  await page.fill("#code", CODE_SIGNIN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("heading", { name: "Needs you" }).waitFor();

  await page.locator(".side").getByRole("link", { name: "Services" }).click();
  await page.getByRole("heading", { name: "Services", exact: true }).waitFor();
  await page.locator("#services-summary").waitFor();
  for (const n of ["hub", "relay", "publish"]) await page.locator("#svc-" + n).waitFor();
  assert((await page.locator("#svc-hub").innerText()).includes("Running"), "hub row says Running");
  assert((await page.getByRole("button", { name: /^Restart/ }).count()) === 0, "no fake restart button");
  await page.locator("#how-relay summary").click();
  const cmd = await page.locator("#how-relay code").innerText();
  assert(cmd === "orch install relay", "exact command shown: " + cmd);
  await page.locator("#how-relay").getByRole("button", { name: "Copy" }).click();
  await page.getByRole("button", { name: "Copied" }).or(page.getByRole("button", { name: "Select and copy" })).waitFor();
  await page.locator("#svc-relay").getByRole("button", { name: "Check now" }).click();
  await page.locator("#services-summary").waitFor();
  await shot("services");

  await page.locator("#services-ws").waitFor();
  await page.getByRole("button", { name: "See workspaces and hand-offs" }).click();
  await page.getByRole("heading", { name: "Connected workspaces" }).waitFor();
  await page.getByRole("heading", { name: "Hand-offs between workspaces" }).waitFor();
  await page.locator("#handoffs-empty").waitFor();
  await shot("workspaces");
  await page.getByRole("button", { name: "Connect a workspace" }).click();
  await page.getByText("30 minutes").first().waitFor();
  await page.getByRole("button", { name: "Everything" }).first().click();
  await page.getByText("Suggested: Everything").waitFor();
  await shot("workspaces-connect");
  await page.getByRole("button", { name: "Details" }).first().click();
  await page.getByText("To change what this workspace shares").waitFor();
  await shot("workspaces-details");
  assert(problems.length === 0, "CSP or script problems: " + problems.join("; "));
  console.log("R11 browser: ok");
} finally {
  await browser.close();
}
