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
  await page.getByRole("heading", { name: "Connect your first workspace" }).waitFor(); // a new hub: Home shows only the next step (P11)

  await page.locator(".side").getByRole("link", { name: "Approvals" }).click();
  await page.getByRole("heading", { name: "Approvals" }).waitFor();
  await shot("approvals");
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
  await page.getByRole("heading", { name: "How it connects" }).waitFor();
  await shot("services");

  await page.locator("#services-ws").waitFor();
  await page.getByRole("button", { name: "See workspaces and hand-offs" }).click();
  await page.getByRole("heading", { name: "Connected workspaces" }).waitFor();
  await shot("workspaces");
  await page.getByRole("button", { name: "Connect a workspace" }).click();
  await page.getByText("30 minutes").first().waitFor();
  // P10: one link box in the connect card, no mode buttons
  await page.getByRole("region", { name: "Connect a workspace" }).waitFor();
  assert((await page.getByRole("button", { name: "Everything" }).count()) === 0, "no mode buttons in the connect card");
  await shot("workspaces-connect");
  // P07: a workspace joins while the page is open (the test joins one now); no click, no navigation.
  assert((await page.getByText("No workspaces yet").count()) === 1, "empty before the join");
  console.log("JOIN_NOW");
  await new Promise((resolve) => process.stdin.once("data", resolve)); process.stdin.pause();
  await page.locator(".ws-joined").waitFor({ timeout: 8000 });
  assert((await page.locator(".ws-joined").innerText()).includes("joined"), "joined message");
  assert((await page.getByText("No workspaces yet").count()) === 0, "empty state gone");
  assert((await page.locator("tr[data-workspace]").count()) === 1, "the new row appeared");
  assert((await page.locator(".rail").innerText()).length > 0, "rail present");
  await shot("workspaces-joined");
  await page.getByRole("button", { name: "Details" }).first().click();
  await page.getByText("To change what this workspace shares").waitFor();
  await shot("workspaces-details");
  // hand-offs live on their own page now (#1418): the empty state, then the page's own heading
  await page.keyboard.press("Escape").catch(() => {});
  await page.locator(".side").getByRole("link", { name: "Hand-offs" }).click();
  await page.getByRole("heading", { name: "Hand-offs", exact: true }).waitFor();
  await shot("handoffs");
  // every page of the M2 batch opens and draws its heading (no script or CSP errors on any of them)
  for (const [tab, heading] of [["Account", "Account"], ["Phones", "Phones & browsers"], ["Activity", "Activity"], ["Connections", "Connections"], ["Admin", "Admin"]]) {
    await page.locator(".side").getByRole("link", { name: tab, exact: true }).click();
    await page.getByRole("heading", { name: heading, exact: true }).first().waitFor();
    await shot("page-" + tab.toLowerCase());
  }
  assert(problems.length === 0, "CSP or script problems: " + problems.join("; "));
  console.log("R11 browser: ok");
} finally {
  await browser.close();
}
